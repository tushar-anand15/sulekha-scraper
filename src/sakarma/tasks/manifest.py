"""Per-LB manifest stage: KPI snapshots + meeting manifest enumeration.

For a single local body and a single scrape run this module:

1. Loads the LBWise dashboard and sets the District + LB Type cascade.
2. Enumerates LB-specific Main Group ddl values and persists them.
3. Walks every (year × main_group) combination, captures KPI cards, then
   drills into each of the 4 meeting categories (approved, ongoing,
   incomplete, cancelled) and upserts a row into ``meeting_manifest`` for
   every discovered meeting.

This is **not** a Celery task — it is a plain function that the Unit-12
orchestrator wraps in a task so it can be retried, rate-limited, etc.

Cascade-reset strategy
-----------------------
After every ``click_button`` drill the portal replaces the dashboard with
the ``GridMeetingDEtails`` grid view.  The cleanest and most reliable way
to restore the dashboard is to call ``client.load_page`` and re-apply
the full five-dropdown cascade (District → LBType → LBName → Year →
MainGroup).  This is expensive (~5 HTTP calls per drill) but avoids any
fragile assumptions about the portal's postback behaviour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from sakarma.config import settings
from sakarma.db.models import (
    CATEGORY_APPROVED,
    CATEGORY_CANCELLED,
    CATEGORY_CANCELLED_OTHER,
    CATEGORY_CANCELLED_PUBLIC_HOLIDAY,
    CATEGORY_CANCELLED_QUORUM,
    CATEGORY_INCOMPLETE,
    CATEGORY_INCOMPLETE_GENERAL,
    CATEGORY_INCOMPLETE_NOT_STARTED,
    CATEGORY_ONGOING,
)
from sakarma.scraper.client import PaginationDetectedError, SakarmaClient
from sakarma.scraper.parsers import (
    ParserError,
    parse_cancellation_subcounts,
    parse_dropdown_options,
    parse_incomplete_subcounts,
    parse_kpi_cards,
    parse_meeting_grid,
)
from sakarma.scraper.protocol import (
    BTN_APPV_MEETINGS,
    BTN_BEFORE_MEETINGS,
    BTN_CANCEL_DETAILS,
    BTN_CNL_OTHER,
    BTN_CNL_PUBLIC_HOLIDAY,
    BTN_CNL_QUORUM,
    BTN_INCOMP_GENERAL,
    BTN_INCOMP_MEETINGS,
    BTN_INCOMP_NOT_STARTED,
    DDL_DISTRICT,
    DDL_LB_NAME,
    DDL_LB_TYPE,
    DDL_MAIN_GROUP,
    DDL_YEAR,
    FormState,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Drill-down button → category constant pairs (in preferred processing order)
# ---------------------------------------------------------------------------
_DRILL_SEQUENCE = [
    (BTN_APPV_MEETINGS, CATEGORY_APPROVED),
    (BTN_BEFORE_MEETINGS, CATEGORY_ONGOING),
    (BTN_INCOMP_MEETINGS, CATEGORY_INCOMPLETE),
    (BTN_CANCEL_DETAILS, CATEGORY_CANCELLED),
]

# ---------------------------------------------------------------------------
# Repos bag type alias (kept lightweight — no dataclass needed here)
# ---------------------------------------------------------------------------
# The caller passes an object (or SimpleNamespace) with the following attrs:
#   .lb_repo               : LBRepository
#   .main_group_value_repo : MainGroupValueRepository
#   .meeting_manifest_repo : MeetingManifestRepository
#   .dashboard_kpi_snapshot_repo : DashboardKPISnapshotRepository
#   .lb_progress_repo      : LBProgressRepository


# ---------------------------------------------------------------------------
# Cascade helpers
# ---------------------------------------------------------------------------

def _set_cascade(
    client: SakarmaClient,
    district_id: int,
    lb_type_id: int,
    lb_id: int,
) -> FormState:
    """Load the dashboard and set District → LBType → LBName.

    Returns the FormState after selecting the LB Name.  The caller then
    applies Year and MainGroup on top.
    """
    state = client.load_page(settings.scraper_lbwise_path)
    state = client.select_dropdown(state, DDL_DISTRICT, str(district_id))
    state = client.select_dropdown(state, DDL_LB_TYPE, str(lb_type_id))
    state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))
    return state


def _set_year_and_group(
    client: SakarmaClient,
    base_state: FormState,
    year_id: int,
    mg_ddl_value: int,
    lb_id: int | None = None,
) -> FormState:
    """Apply Year then MainGroup on top of a cascade base state.

    Both ddlYear AND ddlMainGroup are parents of ddlLBName in the source
    cascade — every change resets the LB to 0. Re-bind ``lb_id`` after
    each reset, otherwise KPI counters read 0 (which silently dropped
    every Standing Committee record on prior runs).
    """
    state = client.select_dropdown(base_state, DDL_YEAR, str(year_id))
    if lb_id is not None:
        state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))
    state = client.select_dropdown(state, DDL_MAIN_GROUP, str(mg_ddl_value))
    if lb_id is not None:
        state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))
    return state


# ---------------------------------------------------------------------------
# Per-category drill helpers
# ---------------------------------------------------------------------------

def _build_manifest_dict(
    row,
    *,
    lb_id: int,
    year_id: int,
    mg_pk: int,
    category: int,
    scrape_run_id: int,
    log,
) -> dict | None:
    """Translate a parsed ``ManifestRow`` into the upsert-row dict.

    Returns ``None`` for rows whose date can't be parsed (logs a warning).
    """
    parsed_date = _parse_date(row.meeting_date)
    if parsed_date is None:
        log.warning(
            "skipping_row_bad_date",
            meeting_no_label=row.meeting_no_label,
            raw_date=row.meeting_date,
        )
        return None
    return {
        "lb_id": lb_id,
        "year_id": year_id,
        "main_group_value_id": mg_pk,
        "category": category,
        "dashboard_grid_select_index": row.dashboard_grid_select_index,
        "dr_postback_target": row.dr_postback_target,
        "meeting_no_label": row.meeting_no_label,
        "meeting_date": parsed_date,
        "meeting_type": row.meeting_type,
        "meeting_nature": row.meeting_nature,
        "meeting_venue": row.meeting_venue,
        "scrape_run_id": scrape_run_id,
    }


def _drill_approved_sync(
    *, client, pre_drill_state, lb_id, year_id, mg_pk, scrape_run_id, repos, log,
    error_writer,
) -> int:
    """Drill the Approved category synchronously and upsert manifest rows."""
    try:
        state_drill = client.click_button(pre_drill_state, BTN_APPV_MEETINGS)
    except PaginationDetectedError as exc:
        msg = str(exc)
        log.error(
            "pagination_detected",
            year_id=year_id,
            category=CATEGORY_APPROVED,
            error=msg,
        )
        error_writer(msg)
        raise

    try:
        parsed_rows = parse_meeting_grid(state_drill.raw_html, CATEGORY_APPROVED)
    except ParserError as exc:
        log.error(
            "approved_grid_missing",
            year_id=year_id,
            error=str(exc),
        )
        raise

    rows_to_insert: list[dict] = []
    for row in parsed_rows:
        d = _build_manifest_dict(
            row,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            category=CATEGORY_APPROVED,
            scrape_run_id=scrape_run_id,
            log=log,
        )
        if d is not None:
            rows_to_insert.append(d)
    inserted = repos.meeting_manifest_repo.upsert_many(rows_to_insert)
    log.debug(
        "approved_drill_done",
        year_id=year_id,
        rows_parsed=len(parsed_rows),
        rows_inserted=inserted,
    )
    return inserted


def _drill_async_category(
    *, client, pre_drill_state, button_target: str, category: int,
    lb_id, year_id, mg_pk, scrape_run_id, repos, log,
) -> int:
    """Drill an async-postback-only category (Ongoing / Incomplete) and upsert.

    The AJAX delta refreshes ``UpdatePanelDEstimate`` with a
    ``GridMeetingDEtails1`` table. Returns the number of manifest rows
    upserted. Empty grids return 0 (no exception).
    """
    async_resp = client.async_postback(pre_drill_state, button_target)
    panel_html = async_resp.panel_html()
    if not panel_html.strip():
        log.debug(
            "async_drill_empty_delta",
            year_id=year_id,
            category=category,
            button=button_target,
        )
        return 0
    try:
        parsed_rows = parse_meeting_grid(panel_html, category)
    except ParserError:
        # The async drill returned a panel without the canonical grid id —
        # log the table ids that ARE present so we can extend the parser
        # if we discover yet another category-specific table id.
        import os as _os
        import re as _re
        seen_ids = _re.findall(rb'<table[^>]*id="([^"]+)"', panel_html)
        log.warning(
            "async_drill_no_grid",
            year_id=year_id,
            category=category,
            button=button_target,
            panel_size=len(panel_html),
            table_ids_found=[i.decode("utf-8", errors="replace") for i in seen_ids],
        )
        # One-shot dump of the first failure so we can examine structure.
        dump_path = "/tmp/sakarma_no_grid_sample.html"
        if not _os.path.exists(dump_path):
            try:
                with open(dump_path, "wb") as _f:
                    _f.write(panel_html)
                log.warning(
                    "dumped_no_grid_sample",
                    path=dump_path,
                    button=button_target,
                    lb_id=lb_id,
                    year_id=year_id,
                )
            except Exception:
                pass
        return 0

    rows_to_insert: list[dict] = []
    for row in parsed_rows:
        d = _build_manifest_dict(
            row,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            category=category,
            scrape_run_id=scrape_run_id,
            log=log,
        )
        if d is not None:
            rows_to_insert.append(d)
    inserted = repos.meeting_manifest_repo.upsert_many(rows_to_insert)
    log.debug(
        "async_drill_done",
        year_id=year_id,
        category=category,
        rows_parsed=len(parsed_rows),
        rows_inserted=inserted,
    )
    return inserted


def _drill_incomplete_subbuckets(
    *, client, pre_drill_state, lb_id, year_id, mg_pk, scrape_run_id, repos, log,
) -> int:
    """Drill the Incomplete category — async postback opens a sub-panel
    revealing two sub-buttons (general / not_started); each non-zero one
    is then async-clicked to render its meeting list.

    Verified live: ``btnInComp_Meetings`` returns a panel containing
    ``lblTotalGntc`` and ``lblTotalPenGntc`` counters plus the matching
    sub-buttons. Each sub-button's async delta carries a
    ``GridMeetingDEtails1`` table with the actual rows.
    """
    log.info("incomplete_drill_start", year_id=year_id, lb_id=lb_id)
    panel_resp = client.async_postback(pre_drill_state, BTN_INCOMP_MEETINGS)
    panel_html = panel_resp.panel_html()
    if not panel_html.strip():
        log.warning(
            "incomplete_panel_empty",
            year_id=year_id,
            lb_id=lb_id,
        )
        return 0

    subcounts = parse_incomplete_subcounts(panel_html)
    log.info(
        "incomplete_subcounts",
        year_id=year_id,
        general=subcounts.general,
        not_started=subcounts.not_started,
    )

    sub_buttons = [
        (BTN_INCOMP_GENERAL, CATEGORY_INCOMPLETE_GENERAL, subcounts.general),
        (
            BTN_INCOMP_NOT_STARTED,
            CATEGORY_INCOMPLETE_NOT_STARTED,
            subcounts.not_started,
        ),
    ]
    total = 0
    # Use the panel's post-async state for follow-up clicks (it carries the
    # right VIEWSTATE/EVENTVALIDATION for the now-expanded panel).
    panel_state = panel_resp.to_form_state(pre_drill_state)
    for button, sub_category, count in sub_buttons:
        if count == 0:
            continue
        total += _drill_async_category(
            client=client,
            pre_drill_state=panel_state,
            button_target=button,
            category=sub_category,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            scrape_run_id=scrape_run_id,
            repos=repos,
            log=log,
        )
    return total


def _drill_cancellation_subbuckets(
    *, client, pre_drill_state, lb_id, year_id, mg_pk, scrape_run_id, repos, log,
) -> int:
    """Drill the Cancelled category — opens pnlCancel, then drills into each
    non-zero sub-bucket via async postback.

    Each cancelled meeting goes into exactly one of three buckets keyed by
    reason. They become separate manifest rows under the
    ``CATEGORY_CANCELLED_*`` sub-codes (sum of which equals the dashboard's
    "cancelled" counter).
    """
    log.info("cancellation_drill_start", year_id=year_id, lb_id=lb_id)
    state_panel = client.click_button(pre_drill_state, BTN_CANCEL_DETAILS)
    try:
        subcounts = parse_cancellation_subcounts(state_panel.raw_html)
    except ParserError as exc:
        log.warning(
            "cancellation_panel_missing",
            year_id=year_id,
            error=str(exc),
        )
        return 0

    log.info(
        "cancellation_subcounts",
        year_id=year_id,
        quorum=subcounts.quorum,
        public_holiday=subcounts.public_holiday,
        other=subcounts.other,
    )

    sub_buttons = [
        (BTN_CNL_QUORUM, CATEGORY_CANCELLED_QUORUM, subcounts.quorum),
        (
            BTN_CNL_PUBLIC_HOLIDAY,
            CATEGORY_CANCELLED_PUBLIC_HOLIDAY,
            subcounts.public_holiday,
        ),
        (BTN_CNL_OTHER, CATEGORY_CANCELLED_OTHER, subcounts.other),
    ]
    total = 0
    for button, sub_category, count in sub_buttons:
        if count == 0:
            continue
        total += _drill_async_category(
            client=client,
            pre_drill_state=state_panel,
            button_target=button,
            category=sub_category,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            scrape_run_id=scrape_run_id,
            repos=repos,
            log=log,
        )
    return total


def _drill_all_categories(
    *, client, lb_id, year_id, mg_pk, kpi, scrape_run_id, repos, log,
    error_writer, pre_drill_state: FormState,
) -> dict:
    """Process all four KPI categories for a single (lb × year × group) cell.

    Returns ``{"manifest_rows_inserted": int, "categories_processed": int}``.
    Skips any category whose KPI counter is 0 (verified: the portal renders
    no grid in that case).
    """
    rows_inserted = 0
    cats_processed = 0

    # Approved (synchronous, has DR/Minutes links — only artifact-bearing cat)
    if kpi.minutes_complete > 0:
        rows_inserted += _drill_approved_sync(
            client=client,
            pre_drill_state=pre_drill_state,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            scrape_run_id=scrape_run_id,
            repos=repos,
            log=log,
            error_writer=error_writer,
        )
    cats_processed += 1

    # Ongoing (async-only)
    if kpi.ongoing > 0:
        rows_inserted += _drill_async_category(
            client=client,
            pre_drill_state=pre_drill_state,
            button_target=BTN_BEFORE_MEETINGS,
            category=CATEGORY_ONGOING,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            scrape_run_id=scrape_run_id,
            repos=repos,
            log=log,
        )
    cats_processed += 1

    # Incomplete — clicking btnInComp_Meetings opens its own sub-panel
    # (general + not-started buckets); each non-zero sub-bucket has its
    # own grid behind a follow-up async click.
    if kpi.minutes_incomplete > 0:
        rows_inserted += _drill_incomplete_subbuckets(
            client=client,
            pre_drill_state=pre_drill_state,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            scrape_run_id=scrape_run_id,
            repos=repos,
            log=log,
        )
    cats_processed += 1

    # Cancelled (sync drill into panel, then async per sub-bucket)
    if kpi.cancelled > 0:
        rows_inserted += _drill_cancellation_subbuckets(
            client=client,
            pre_drill_state=pre_drill_state,
            lb_id=lb_id,
            year_id=year_id,
            mg_pk=mg_pk,
            scrape_run_id=scrape_run_id,
            repos=repos,
            log=log,
        )
    cats_processed += 1

    return {
        "manifest_rows_inserted": rows_inserted,
        "categories_processed": cats_processed,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_for_lb(
    client: SakarmaClient,
    repos: Any,
    lb_id: int,
    scrape_run_id: int,
) -> dict:
    """Run the manifest stage for a single local body.

    Args:
        client: Authenticated :class:`SakarmaClient` instance.
        repos: A namespace/object exposing:
            - ``lb_repo`` (LBRepository)
            - ``main_group_value_repo`` (MainGroupValueRepository)
            - ``meeting_manifest_repo`` (MeetingManifestRepository)
            - ``dashboard_kpi_snapshot_repo`` (DashboardKPISnapshotRepository)
            - ``lb_progress_repo`` (LBProgressRepository)
        lb_id: PK of the LB row in ``sakarma.lb``.
        scrape_run_id: PK of the enclosing scrape run.

    Returns:
        Summary dict with keys:
        ``kpi_snapshots``, ``manifest_rows_inserted``,
        ``categories_processed``, ``years_processed``,
        ``main_groups_processed``.

    Raises:
        :class:`~sakarma.scraper.client.PaginationDetectedError`:
            When a drill response contains pager controls.  The error message
            is written to ``lb_progress.error_message`` before re-raising so
            the operator can inspect it.
    """
    lb = repos.lb_repo.get(lb_id)
    if lb is None:
        raise ValueError(f"LB {lb_id} not found in database")

    district_id: int = lb.district_id
    lb_type_id: int = lb.lb_type_id

    log = logger.bind(lb_id=lb_id, lb_name=lb.name_ml, scrape_run_id=scrape_run_id)
    log.info("manifest.start")

    # ------------------------------------------------------------------
    # Step 1: Load page and discover years
    # ------------------------------------------------------------------
    state = client.load_page(settings.scraper_lbwise_path)

    # We need District set to get the full year list; the year DDL is
    # independent of LB, but setting district is needed for the cascade.
    state = client.select_dropdown(state, DDL_DISTRICT, str(district_id))
    state = client.select_dropdown(state, DDL_LB_TYPE, str(lb_type_id))
    state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))

    raw_years: list[tuple[int, str]] = parse_dropdown_options(state.raw_html, DDL_YEAR)
    # Sort descending by year_int (most recent first).  The text may be
    # "2024" or "2024-25"; we sort on the first 4-digit run.
    def _year_sort_key(pair: tuple[int, str]) -> int:
        import re as _re
        m = _re.search(r"\d{4}", pair[1])
        return int(m.group()) if m else pair[0]

    years: list[tuple[int, str]] = sorted(raw_years, key=_year_sort_key, reverse=True)
    log.info("years_discovered", count=len(years))

    # ------------------------------------------------------------------
    # Step 2: Discover Main Group values for this LB
    # ------------------------------------------------------------------
    mg_options: list[tuple[int, str]] = parse_dropdown_options(state.raw_html, DDL_MAIN_GROUP)
    main_group_map: dict[int, int] = {}  # ddl_value -> main_group_value.id (PK)

    for ddl_value, name_ml in mg_options:
        mg_row = repos.main_group_value_repo.upsert(
            lb_id=lb_id,
            ddl_value=ddl_value,
            name_ml=name_ml,
        )
        main_group_map[ddl_value] = mg_row.id

    log.info("main_groups_discovered", count=len(main_group_map))

    if not main_group_map:
        log.warning("no_main_groups_found_for_lb")
        return {
            "kpi_snapshots": 0,
            "manifest_rows_inserted": 0,
            "categories_processed": 0,
            "years_processed": 0,
            "main_groups_processed": 0,
        }

    # ------------------------------------------------------------------
    # Step 3: Walk years × main_groups × 4 categories
    # ------------------------------------------------------------------
    kpi_snapshots_written = 0
    total_manifest_rows = 0
    categories_processed = 0
    years_processed = 0

    for year_id, year_text in years:
        log.info("processing_year", year_id=year_id, year_text=year_text)
        years_processed += 1

        for mg_ddl_value, mg_pk in main_group_map.items():
            log.debug("processing_group", year_id=year_id, mg_ddl_value=mg_ddl_value)

            # ---- (re-)establish full cascade for this year+group ----
            try:
                base_state = _set_cascade(client, district_id, lb_type_id, lb_id)
                state_yg = _set_year_and_group(
                    client, base_state, year_id, mg_ddl_value, lb_id=lb_id
                )
            except Exception as exc:
                log.error(
                    "cascade_setup_failed",
                    year_id=year_id,
                    mg_ddl_value=mg_ddl_value,
                    error=str(exc),
                )
                raise

            # ---- KPI snapshot ----
            kpi = parse_kpi_cards(state_yg.raw_html)
            repos.dashboard_kpi_snapshot_repo.upsert(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mg_pk,
                scrape_run_id=scrape_run_id,
                total_meetings=kpi.total,
                ongoing=kpi.ongoing,
                minutes_complete=kpi.minutes_complete,
                minutes_incomplete=kpi.minutes_incomplete,
                cancelled=kpi.cancelled,
            )
            kpi_snapshots_written += 1

            # ---- Per-category drill-downs ----
            # The portal uses two different rendering paths depending on
            # the category:
            #   * Approved (btnAppv_Meetings): synchronous postback returns
            #     a full page with `GridMeetingDEtails`. Has DR + Minutes
            #     links per row (only category with artifacts).
            #   * Ongoing / Incomplete (btnBefore / btnInComp): MS-AJAX
            #     async postback. Delta refreshes UpdatePanelDEstimate
            #     with `GridMeetingDEtails1` table. Same row schema for
            #     cols 0-5; no DR/Minutes link columns.
            #   * Cancelled (btnCancelDetails): synchronous postback opens
            #     pnlCancel showing 3 sub-counts. Each non-zero sub-count
            #     drives an async postback into one of three buttons:
            #       - btncnlquarum    (cancelled — quorum not met)
            #       - btnPublicH      (cancelled — public holiday)
            #       - btnOthersH      (cancelled — other reason)
            #     Each sub-button's async delta also renders
            #     `GridMeetingDEtails1` with the per-reason rows.
            #
            # KPI-counter zero-skip: when the dashboard counter for a
            # category is 0 we skip the drill entirely — the AJAX delta
            # would render no grid (verified live).
            inserted_for_cell = _drill_all_categories(
                client=client,
                lb_id=lb_id,
                year_id=year_id,
                mg_pk=mg_pk,
                kpi=kpi,
                scrape_run_id=scrape_run_id,
                repos=repos,
                log=log,
                error_writer=lambda msg: _try_write_progress_error(
                    repos, scrape_run_id, lb_id, msg
                ),
                pre_drill_state=state_yg,
            )
            total_manifest_rows += inserted_for_cell["manifest_rows_inserted"]
            categories_processed += inserted_for_cell["categories_processed"]

    summary = {
        "kpi_snapshots": kpi_snapshots_written,
        "manifest_rows_inserted": total_manifest_rows,
        "categories_processed": categories_processed,
        "years_processed": years_processed,
        "main_groups_processed": len(main_group_map),
    }
    log.info("manifest.done", **summary)
    return summary


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str):
    """Parse a meeting date string → :class:`datetime.date`.

    Source uses both ``DD/MM/YYYY`` (recent grids) and ``DD.MM.YYYY``
    (older grids, observed for 2020-and-earlier rows). Try both before
    giving up.
    """
    if not raw:
        return None
    s = raw.strip()
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _try_write_progress_error(
    repos: Any, scrape_run_id: int, lb_id: int, error_message: str
) -> None:
    """Best-effort write of error_message to the lb_progress row.

    Uses ``lb_progress_repo.list_for_run`` to locate the progress row for
    this (scrape_run_id, lb_id).  Swallows any exception so the caller's
    re-raise of ``PaginationDetectedError`` is the authoritative signal.
    """
    try:
        progress_rows = repos.lb_progress_repo.list_for_run(scrape_run_id)
        for pr in progress_rows:
            if pr.lb_id == lb_id:
                repos.lb_progress_repo.mark_error(pr.id, error_message)
                return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed_to_write_progress_error",
            scrape_run_id=scrape_run_id,
            lb_id=lb_id,
            inner_error=str(exc),
        )
