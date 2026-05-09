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
    CATEGORY_INCOMPLETE,
    CATEGORY_ONGOING,
)
from sakarma.scraper.client import PaginationDetectedError, SakarmaClient
from sakarma.scraper.parsers import parse_dropdown_options, parse_kpi_cards, parse_meeting_grid
from sakarma.scraper.protocol import (
    BTN_APPV_MEETINGS,
    BTN_BEFORE_MEETINGS,
    BTN_CANCEL_DETAILS,
    BTN_INCOMP_MEETINGS,
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
) -> FormState:
    """Apply Year then MainGroup on top of a cascade base state."""
    state = client.select_dropdown(base_state, DDL_YEAR, str(year_id))
    state = client.select_dropdown(state, DDL_MAIN_GROUP, str(mg_ddl_value))
    return state


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
                state_yg = _set_year_and_group(client, base_state, year_id, mg_ddl_value)
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

            # ---- 4 drill-down categories ----
            for button, category in _DRILL_SEQUENCE:
                # Re-establish cascade before each drill (portal replaces
                # the page after each click_button).
                try:
                    pre_drill_state = _set_cascade(
                        client, district_id, lb_type_id, lb_id
                    )
                    pre_drill_state = _set_year_and_group(
                        client, pre_drill_state, year_id, mg_ddl_value
                    )
                except Exception as exc:
                    log.error(
                        "pre_drill_cascade_failed",
                        year_id=year_id,
                        mg_ddl_value=mg_ddl_value,
                        category=category,
                        error=str(exc),
                    )
                    raise

                try:
                    state_drill = client.click_button(pre_drill_state, button)
                except PaginationDetectedError as exc:
                    err_msg = str(exc)
                    log.error(
                        "pagination_detected",
                        year_id=year_id,
                        mg_ddl_value=mg_ddl_value,
                        category=category,
                        error=err_msg,
                    )
                    # Write error to lb_progress; caller must locate the progress row.
                    # We surface this via a well-known attribute that the orchestrator
                    # can look up by (scrape_run_id, lb_id).
                    _try_write_progress_error(repos, scrape_run_id, lb_id, err_msg)
                    raise

                # Parse and accumulate rows.
                parsed_rows = parse_meeting_grid(state_drill.raw_html, category)

                manifest_dicts: list[dict] = []
                for row in parsed_rows:
                    parsed_date = _parse_date(row.meeting_date)
                    if parsed_date is None:
                        log.warning(
                            "skipping_row_bad_date",
                            meeting_no_label=row.meeting_no_label,
                            raw_date=row.meeting_date,
                        )
                        continue
                    manifest_dicts.append(
                        {
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
                    )

                inserted = repos.meeting_manifest_repo.upsert_many(manifest_dicts)
                total_manifest_rows += inserted
                categories_processed += 1

                log.debug(
                    "category_done",
                    year_id=year_id,
                    mg_ddl_value=mg_ddl_value,
                    category=category,
                    rows=inserted,
                )

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
    """Parse ``DD/MM/YYYY`` → :class:`datetime.date`.  Returns ``None`` on failure."""
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
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
