"""Per-LB artifact stage: Minutes HTML + DR HTML + attachment PDFs.

For a single local body and a single scrape run this module:

1. Queries all Approved-category manifest rows for this (lb_id, scrape_run_id).
2. Groups them by (year_id, main_group_value_id) — one portal "cell".
3. For each cell, re-primes the LBWise dashboard cascade and clicks the
   Approved-meetings button to restore the grid view.
4. For each manifest row in the cell:
   - Fetches Minutes HTML (if not already captured).
   - Fetches DR HTML (if not already captured).
   - Parses attachment links from the DR HTML and downloads each PDF.
5. Persists each artifact via MeetingArtifactRepository.get_or_create
   (content-hash dedup ensures idempotency across retries).

This is **not** a Celery task — it is a pure function called by the Unit-12
orchestrator within an existing requests.Session.

Session-recovery strategy
--------------------------
When a ``SessionExpiredError`` is raised mid-row, we re-prime the full
dashboard cascade and continue from the next pending row.  At most
``_MAX_SESSION_RECOVERIES`` (3) recoveries are attempted per LB call; if
the limit is exceeded the error propagates to the orchestrator.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import groupby
from typing import Any

import structlog

from sakarma.config import settings
from sakarma.db.models import CATEGORY_APPROVED
from sakarma.scraper.client import (
    SakarmaClient,
    ServerSideUnavailableError,
    SessionExpiredError,
)
from sakarma.scraper.parsers import parse_attachment_links, parse_window_open_url
from sakarma.scraper.protocol import (
    BTN_APPV_MEETINGS,
    DDL_DISTRICT,
    DDL_LB_NAME,
    DDL_LB_TYPE,
    DDL_MAIN_GROUP,
    DDL_YEAR,
    FormState,
    parse_form_state,
)
from sakarma.storage.gcs import (
    ARTIFACT_ATTACHMENT_PDF,
    ARTIFACT_DR_HTML,
    ARTIFACT_MINUTES_HTML,
    build_meeting_path,
    upload_document,
)

logger = structlog.get_logger(__name__)

# Maximum number of session-expiry recoveries per LB call.
_MAX_SESSION_RECOVERIES: int = 3


# ---------------------------------------------------------------------------
# Repos bag
# ---------------------------------------------------------------------------


@dataclass
class ArtifactsRepos:
    """Lightweight container for the repository instances needed by this stage."""

    lb_repo: Any
    year_repo: Any
    main_group_value_repo: Any
    meeting_manifest_repo: Any
    meeting_artifact_repo: Any
    lb_progress_repo: Any


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_for_lb(
    client: SakarmaClient,
    storage: Any,  # BaseStorage instance
    repos: ArtifactsRepos,
    lb_id: int,
    scrape_run_id: int,
) -> dict:
    """Fetch Minutes HTML + DR HTML + attachment PDFs for every Approved
    manifest row not yet fully captured.

    Args:
        client: Authenticated :class:`~sakarma.scraper.client.SakarmaClient`
            sharing the caller's ``requests.Session``.
        storage: :class:`~sulekha.storage.gcs.BaseStorage` instance for
            SAKARMA artifacts.
        repos: :class:`ArtifactsRepos` holding the required repository instances.
        lb_id: PK of the LB row in ``sakarma.lb``.
        scrape_run_id: PK of the enclosing scrape run.

    Returns:
        Summary dict with keys:
        ``minutes_uploaded``, ``dr_uploaded``, ``attachments_uploaded``,
        ``rows_processed``, ``rows_skipped``.
    """
    lb = repos.lb_repo.get(lb_id)
    if lb is None:
        raise ValueError(f"LB {lb_id} not found in database")

    district_id: int = lb.district_id
    lb_type_id: int = lb.lb_type_id

    log = logger.bind(lb_id=lb_id, lb_name=lb.name_ml, scrape_run_id=scrape_run_id)
    log.info("artifacts.start")

    # ------------------------------------------------------------------
    # Pre-load dimension caches
    # ------------------------------------------------------------------
    # year_id -> year_int (calendar year integer e.g. 2025)
    # We only need years referenced by manifest rows; build lazily via a
    # defaultdict-like helper.
    _year_cache: dict[int, int] = {}

    def _year_int(year_id: int) -> int:
        if year_id not in _year_cache:
            yr = repos.year_repo.get(year_id)
            if yr is None:
                raise ValueError(f"Year id={year_id} not found in database")
            _year_cache[year_id] = yr.year_int
        return _year_cache[year_id]

    # main_group_value_id -> ddl_value (for select_dropdown calls)
    _mgv_ddl_cache: dict[int, int] = {}

    def _mgv_ddl(main_group_value_id: int) -> int:
        if main_group_value_id not in _mgv_ddl_cache:
            mgv = repos.main_group_value_repo.get(main_group_value_id)
            if mgv is None:
                raise ValueError(
                    f"MainGroupValue id={main_group_value_id} not found"
                )
            _mgv_ddl_cache[main_group_value_id] = mgv.ddl_value
        return _mgv_ddl_cache[main_group_value_id]

    # ------------------------------------------------------------------
    # Fetch all Approved manifest rows for this LB + scrape_run
    # ------------------------------------------------------------------
    all_rows = repos.meeting_manifest_repo.list_approved_for_lb_run(
        lb_id, scrape_run_id
    )
    log.info("artifacts.rows_fetched", count=len(all_rows))

    if not all_rows:
        log.info("artifacts.no_rows_to_process")
        return {
            "minutes_uploaded": 0,
            "dr_uploaded": 0,
            "attachments_uploaded": 0,
            "rows_processed": 0,
            "rows_skipped": 0,
        }

    # ------------------------------------------------------------------
    # Group rows by (year_id, main_group_value_id) — portal "cells"
    # ------------------------------------------------------------------
    def _cell_key(row: Any) -> tuple[int, int]:
        return (row.year_id, row.main_group_value_id)

    # Sort ensures groupby produces correct groups (rows already ordered by
    # the repository but we make it explicit here).
    all_rows_sorted = sorted(all_rows, key=_cell_key)

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------
    minutes_uploaded = 0
    dr_uploaded = 0
    attachments_uploaded = 0
    rows_processed = 0
    rows_skipped = 0
    rows_server_unavailable = 0  # rows the source server can't render (NRE)

    # ------------------------------------------------------------------
    # Process each cell — delegate to _process_cell for the per-cell loop
    # body so the same logic is reusable from the parallel cell-task path.
    # ------------------------------------------------------------------
    for (year_id, mg_id), cell_rows_iter in groupby(all_rows_sorted, key=_cell_key):
        cell_rows = list(cell_rows_iter)
        cell_summary = _process_cell(
            client=client,
            storage=storage,
            repos=repos,
            lb_id=lb_id,
            scrape_run_id=scrape_run_id,
            year_id=year_id,
            mg_id=mg_id,
            district_id=district_id,
            lb_type_id=lb_type_id,
            year=_year_int(year_id),
            mg_ddl=_mgv_ddl(mg_id),
            cell_rows=cell_rows,
            log=log,
        )
        minutes_uploaded += cell_summary["minutes_uploaded"]
        dr_uploaded += cell_summary["dr_uploaded"]
        attachments_uploaded += cell_summary["attachments_uploaded"]
        rows_processed += cell_summary["rows_processed"]
        rows_skipped += cell_summary["rows_skipped"]
        rows_server_unavailable += cell_summary["rows_server_unavailable"]


    summary = {
        "minutes_uploaded": minutes_uploaded,
        "dr_uploaded": dr_uploaded,
        "attachments_uploaded": attachments_uploaded,
        "rows_processed": rows_processed,
        "rows_skipped": rows_skipped,
        "rows_server_unavailable": rows_server_unavailable,
    }
    log.info("artifacts.done", **summary)
    return summary




def _process_cell(
    *,
    client: SakarmaClient,
    storage: Any,
    repos: ArtifactsRepos,
    lb_id: int,
    scrape_run_id: int,
    year_id: int,
    mg_id: int,
    district_id: int,
    lb_type_id: int,
    year: int,
    mg_ddl: int,
    cell_rows: list,
    log: Any,
) -> dict:
    """Process every Approved manifest row for one (year_id, mg_id) cell.

    Owns its own SessionExpiredError recovery counter. Returns a per-cell
    summary dict matching the run_for_lb summary shape.
    """
    minutes_uploaded = 0
    dr_uploaded = 0
    attachments_uploaded = 0
    rows_processed = 0
    rows_skipped = 0
    rows_server_unavailable = 0
    session_recoveries = 0

    log.debug(
        "artifacts.cell_start",
        year_id=year_id,
        year=year,
        mg_id=mg_id,
        cell_row_count=len(cell_rows),
    )

    # Re-prime dashboard for this (lb, year, group) cell.
    try:
        state = _prime_dashboard(
            client, district_id, lb_type_id, lb_id, year_id, mg_ddl
        )
    except SessionExpiredError:
        session_recoveries += 1
        if session_recoveries > _MAX_SESSION_RECOVERIES:
            log.error(
                "artifacts.session_recovery_limit_exceeded",
                limit=_MAX_SESSION_RECOVERIES,
            )
            raise
        log.warning(
            "artifacts.session_expired_during_prime",
            year_id=year_id,
            mg_id=mg_id,
            recovery_attempt=session_recoveries,
        )
        state = _prime_dashboard(
            client, district_id, lb_type_id, lb_id, year_id, mg_ddl
        )

    # Process each manifest row in this cell.
    for row in cell_rows:
        # Heuristic skip: if Minutes AND DR already exist, row is done.
        min_exists = repos.meeting_artifact_repo.exists(
            row.id, ARTIFACT_MINUTES_HTML
        )
        dr_exists = repos.meeting_artifact_repo.exists(row.id, ARTIFACT_DR_HTML)

        if min_exists and dr_exists:
            log.debug(
                "artifacts.row_already_complete",
                manifest_id=row.id,
                meeting_no_label=row.meeting_no_label,
            )
            rows_skipped += 1
            continue

        # ------------------------------------------------------------
        # Minutes HTML
        # ------------------------------------------------------------
        if not min_exists:
            try:
                state, minutes_bytes = _fetch_minutes(client, state, row)
                path = build_meeting_path(
                    district_id=district_id,
                    lb_type_id=lb_type_id,
                    lb_id=lb_id,
                    year=year,
                    main_group_value_id=mg_id,
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_MINUTES_HTML,
                )
                gcs_path, content_hash, byte_size = upload_document(
                    storage,
                    minutes_bytes,
                    path,
                    content_type="text/html; charset=utf-8",
                )
                repos.meeting_artifact_repo.get_or_create(
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_MINUTES_HTML,
                    content_hash=content_hash,
                    gcs_path=gcs_path,
                    byte_size=byte_size,
                    source_page_url=(
                        f"{settings.scraper_base_url}"
                        f"{settings.scraper_minutes_path}"
                    ),
                    scrape_run_id=scrape_run_id,
                    decision_index=None,
                    original_filename=None,
                )
                minutes_uploaded += 1
                log.debug(
                    "artifacts.minutes_uploaded",
                    manifest_id=row.id,
                    gcs_path=gcs_path,
                )
            except SessionExpiredError:
                session_recoveries += 1
                if session_recoveries > _MAX_SESSION_RECOVERIES:
                    log.error(
                        "artifacts.session_recovery_limit_exceeded",
                        limit=_MAX_SESSION_RECOVERIES,
                    )
                    raise
                log.warning(
                    "artifacts.session_expired_minutes",
                    manifest_id=row.id,
                    recovery_attempt=session_recoveries,
                )
                state = _prime_dashboard(
                    client, district_id, lb_type_id, lb_id, year_id, mg_ddl
                )
                # Re-attempt the minutes fetch after recovery.
                state, minutes_bytes = _fetch_minutes(client, state, row)
                path = build_meeting_path(
                    district_id=district_id,
                    lb_type_id=lb_type_id,
                    lb_id=lb_id,
                    year=year,
                    main_group_value_id=mg_id,
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_MINUTES_HTML,
                )
                gcs_path, content_hash, byte_size = upload_document(
                    storage,
                    minutes_bytes,
                    path,
                    content_type="text/html; charset=utf-8",
                )
                repos.meeting_artifact_repo.get_or_create(
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_MINUTES_HTML,
                    content_hash=content_hash,
                    gcs_path=gcs_path,
                    byte_size=byte_size,
                    source_page_url=(
                        f"{settings.scraper_base_url}"
                        f"{settings.scraper_minutes_path}"
                    ),
                    scrape_run_id=scrape_run_id,
                    decision_index=None,
                    original_filename=None,
                )
                minutes_uploaded += 1
            except ServerSideUnavailableError as exc:
                # Source server crashes on this meeting (NRE) — record
                # and move on to the next row. Don't try DR/attachments
                # either; they'll fail the same way.
                log.warning(
                    "artifacts.server_side_unavailable",
                    manifest_id=row.id,
                    lb_id=lb_id,
                    error=str(exc),
                )
                rows_server_unavailable += 1
                continue
            except Exception as exc:
                log.error(
                    "artifacts.minutes_error",
                    manifest_id=row.id,
                    error=str(exc),
                    exc_type=type(exc).__name__,
                    exc_module=type(exc).__module__,
                )
                rows_skipped += 1
                continue

        # ------------------------------------------------------------
        # DR HTML
        # ------------------------------------------------------------
        dr_bytes: bytes | None = None
        if not dr_exists:
            try:
                state, dr_bytes = _fetch_dr(client, state, row)
                dr_path = build_meeting_path(
                    district_id=district_id,
                    lb_type_id=lb_type_id,
                    lb_id=lb_id,
                    year=year,
                    main_group_value_id=mg_id,
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_DR_HTML,
                )
                gcs_path_dr, content_hash_dr, byte_size_dr = upload_document(
                    storage,
                    dr_bytes,
                    dr_path,
                    content_type="text/html; charset=utf-8",
                )
                repos.meeting_artifact_repo.get_or_create(
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_DR_HTML,
                    content_hash=content_hash_dr,
                    gcs_path=gcs_path_dr,
                    byte_size=byte_size_dr,
                    source_page_url=(
                        f"{settings.scraper_base_url}"
                        f"{settings.scraper_dregister_path}"
                    ),
                    scrape_run_id=scrape_run_id,
                    decision_index=None,
                    original_filename=None,
                )
                dr_uploaded += 1
                log.debug(
                    "artifacts.dr_uploaded",
                    manifest_id=row.id,
                    gcs_path=gcs_path_dr,
                )
            except SessionExpiredError:
                session_recoveries += 1
                if session_recoveries > _MAX_SESSION_RECOVERIES:
                    log.error(
                        "artifacts.session_recovery_limit_exceeded",
                        limit=_MAX_SESSION_RECOVERIES,
                    )
                    raise
                log.warning(
                    "artifacts.session_expired_dr",
                    manifest_id=row.id,
                    recovery_attempt=session_recoveries,
                )
                state = _prime_dashboard(
                    client, district_id, lb_type_id, lb_id, year_id, mg_ddl
                )
                state, dr_bytes = _fetch_dr(client, state, row)
                dr_path = build_meeting_path(
                    district_id=district_id,
                    lb_type_id=lb_type_id,
                    lb_id=lb_id,
                    year=year,
                    main_group_value_id=mg_id,
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_DR_HTML,
                )
                gcs_path_dr, content_hash_dr, byte_size_dr = upload_document(
                    storage,
                    dr_bytes,
                    dr_path,
                    content_type="text/html; charset=utf-8",
                )
                repos.meeting_artifact_repo.get_or_create(
                    meeting_manifest_id=row.id,
                    artifact_type=ARTIFACT_DR_HTML,
                    content_hash=content_hash_dr,
                    gcs_path=gcs_path_dr,
                    byte_size=byte_size_dr,
                    source_page_url=(
                        f"{settings.scraper_base_url}"
                        f"{settings.scraper_dregister_path}"
                    ),
                    scrape_run_id=scrape_run_id,
                    decision_index=None,
                    original_filename=None,
                )
                dr_uploaded += 1
            except ServerSideUnavailableError as exc:
                # Source server NRE on the DR for this meeting — skip
                # attachments too (they live behind the same broken
                # session-bound page) and continue to the next row.
                log.warning(
                    "artifacts.dr_server_side_unavailable",
                    manifest_id=row.id,
                    lb_id=lb_id,
                    error=str(exc),
                )
                rows_server_unavailable += 1
                continue
            except Exception as exc:
                log.error(
                    "artifacts.dr_error",
                    manifest_id=row.id,
                    error=str(exc),
                )
                rows_skipped += 1
                continue

        # ------------------------------------------------------------
        # Attachments — parsed from DR bytes
        # ------------------------------------------------------------
        # We need DR bytes to parse attachments. If dr_bytes is None here
        # it means DR already existed from a prior run; re-fetch to get
        # bytes for attachment parsing.
        if dr_bytes is None:
            # DR was already captured — re-fetch to parse links.
            try:
                _, dr_bytes = _fetch_dr(client, state, row)
            except ServerSideUnavailableError:
                # Server NRE on DR re-fetch — can't parse attachments,
                # but we already have Minutes + DR (from a prior run).
                # Leave dr_bytes empty so the loop just skips attachments.
                dr_bytes = b""
            except Exception as exc:
                log.warning(
                    "artifacts.dr_refetch_for_attachments_failed",
                    manifest_id=row.id,
                    error=str(exc),
                )
                dr_bytes = b""

        if dr_bytes:
            attachments = parse_attachment_links(dr_bytes)
            # Build a fresh DR FormState. The lnkFileView postback must
            # target the same DR page our GET hit — that's PublicDRegister
            # for panchayats but PublicCouncilDRegister for Municipalities/
            # Corporations. Use the URL the dashboard's window.open emitted
            # (captured in state.raw_html from the click_dr postback);
            # fall back to the hard-coded settings path.
            dr_path = _resolve_artifact_path(
                state.raw_html, settings.scraper_dregister_path
            )
            dr_state = parse_form_state(
                dr_bytes,
                page_url=f"{settings.scraper_base_url}{dr_path}",
            )
            for decision_index, target in attachments:
                if not target:
                    continue

                try:
                    files = client.fetch_attachment_files(dr_state, target)
                except Exception as exc:
                    log.warning(
                        "artifacts.attachment_download_failed",
                        manifest_id=row.id,
                        target=target,
                        error=str(exc),
                    )
                    continue

                for file_idx, (pdf_bytes, original_filename) in enumerate(files):
                    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
                        log.warning(
                            "artifacts.attachment_not_pdf",
                            manifest_id=row.id,
                            target=target,
                            file_idx=file_idx,
                            first8=pdf_bytes[:8].hex() if pdf_bytes else "",
                        )
                        continue
                    content_hash_pdf = _sha256_hex(pdf_bytes)
                    att_path = build_meeting_path(
                        district_id=district_id,
                        lb_type_id=lb_type_id,
                        lb_id=lb_id,
                        year=year,
                        main_group_value_id=mg_id,
                        meeting_manifest_id=row.id,
                        artifact_type=ARTIFACT_ATTACHMENT_PDF,
                        original_filename=original_filename or None,
                        content_hash=content_hash_pdf,
                    )
                    gcs_path_att, _, byte_size_att = upload_document(
                        storage,
                        pdf_bytes,
                        att_path,
                        content_type="application/pdf",
                        original_filename=original_filename or None,
                    )
                    repos.meeting_artifact_repo.get_or_create(
                        meeting_manifest_id=row.id,
                        artifact_type=ARTIFACT_ATTACHMENT_PDF,
                        content_hash=content_hash_pdf,
                        gcs_path=gcs_path_att,
                        byte_size=byte_size_att,
                        source_page_url=(
                            f"{settings.scraper_base_url}"
                            f"{settings.scraper_dregister_path}"
                        ),
                        scrape_run_id=scrape_run_id,
                        decision_index=decision_index,
                        original_filename=original_filename or None,
                    )
                    attachments_uploaded += 1

        rows_processed += 1

    log.debug(
        "artifacts.cell_done",
        year_id=year_id,
        mg_id=mg_id,
    )


    return {
        "minutes_uploaded": minutes_uploaded,
        "dr_uploaded": dr_uploaded,
        "attachments_uploaded": attachments_uploaded,
        "rows_processed": rows_processed,
        "rows_skipped": rows_skipped,
        "rows_server_unavailable": rows_server_unavailable,
    }


def run_for_cell(
    client: SakarmaClient,
    storage: Any,
    repos: ArtifactsRepos,
    lb_id: int,
    scrape_run_id: int,
    year_id: int,
    main_group_value_id: int,
) -> dict:
    """Process one (year_id, main_group_value_id) cell end-to-end.

    Public entry point used by the Celery cell task. Resolves the LB
    dimensions, queries cell rows, and delegates to _process_cell.
    """
    lb = repos.lb_repo.get(lb_id)
    if lb is None:
        raise ValueError(f"LB {lb_id} not found in database")
    yr = repos.year_repo.get(year_id)
    if yr is None:
        raise ValueError(f"Year id={year_id} not found in database")
    mgv = repos.main_group_value_repo.get(main_group_value_id)
    if mgv is None:
        raise ValueError(
            f"MainGroupValue id={main_group_value_id} not found"
        )

    cell_rows = repos.meeting_manifest_repo.list_approved_for_cell(
        lb_id, scrape_run_id, year_id, main_group_value_id
    )
    log = logger.bind(
        lb_id=lb_id,
        scrape_run_id=scrape_run_id,
        year_id=year_id,
        mg_id=main_group_value_id,
    )
    log.info("artifacts.cell.start", cell_row_count=len(cell_rows))
    if not cell_rows:
        return {
            "minutes_uploaded": 0,
            "dr_uploaded": 0,
            "attachments_uploaded": 0,
            "rows_processed": 0,
            "rows_skipped": 0,
            "rows_server_unavailable": 0,
        }

    summary = _process_cell(
        client=client,
        storage=storage,
        repos=repos,
        lb_id=lb_id,
        scrape_run_id=scrape_run_id,
        year_id=year_id,
        mg_id=main_group_value_id,
        district_id=lb.district_id,
        lb_type_id=lb.lb_type_id,
        year=yr.year_int,
        mg_ddl=mgv.ddl_value,
        cell_rows=cell_rows,
        log=log,
    )
    log.info("artifacts.cell.done", **summary)
    return summary


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _prime_dashboard(
    client: SakarmaClient,
    district_id: int,
    lb_type_id: int,
    lb_id: int,
    year_id: int,
    mg_ddl_value: int,
) -> FormState:
    """Load the LBWise dashboard and prime all five dropdowns + Approved button.

    Returns the :class:`~sakarma.scraper.protocol.FormState` after clicking
    ``BTN_APPV_MEETINGS`` — i.e. the page that shows the Approved meetings
    grid for this (lb, year, group) combination.
    """
    # IMPORTANT: both ddlYear AND ddlMainGroup are parents of ddlLBName in
    # the source's cascade — changing either resets ddlLBName to 0, which
    # silently zeros every KPI. We must re-select LB AFTER each postback
    # that resets it. (The year-only fix recovered Governing Body meetings;
    # the additional mg re-bind unlocks Standing Committee data — every
    # mg=4 KPI was 0 before this because ddlLBName was 0 at read time.)
    state = client.load_page(settings.scraper_lbwise_path)
    state = client.select_dropdown(state, DDL_DISTRICT, str(district_id))
    state = client.select_dropdown(state, DDL_LB_TYPE, str(lb_type_id))
    state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))
    state = client.select_dropdown(state, DDL_YEAR, str(year_id))
    state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))  # re-bind after year
    state = client.select_dropdown(state, DDL_MAIN_GROUP, str(mg_ddl_value))
    state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))  # re-bind after mg
    state = client.click_button(state, BTN_APPV_MEETINGS)
    return state


def _resolve_artifact_path(
    state_html: bytes, fallback_path: str
) -> str:
    """Pick the artifact URL the source advertised in its ``window.open``.

    Municipalities/Corporations emit ``PublicCouncilMinutes.aspx``;
    Grama/Block/District panchayats emit ``PublicMinutes.aspx`` (etc.).
    Hard-coding either causes a 500 NRE for the other group, so we follow
    the URL the server itself just emitted. ``fallback_path`` keeps older
    flows working if the script is absent.
    """
    url = parse_window_open_url(state_html)
    if not url:
        return fallback_path
    # Normalise to a leading slash + Pages/ prefix when the popup uses a
    # bare relative path.
    if url.startswith("/"):
        return url
    if url.startswith("Pages/"):
        return "/" + url
    return "/Pages/" + url


_META_CHARSET = b'<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />'


def _ensure_charset_meta(html_bytes: bytes) -> bytes:
    """Inject a UTF-8 charset meta tag into ``<head>`` if missing.

    The source server returns UTF-8 bytes but relies on the HTTP
    ``Content-Type`` header for the encoding declaration — when these
    files are saved to disk and opened locally, browsers fall back to
    Latin-1 and Malayalam characters render as mojibake. Adding the
    ``<meta>`` tag inside the document makes it self-describing so any
    viewer (local file, gsutil-downloaded copy, archival format
    conversion) gets the right encoding.
    """
    if not html_bytes:
        return html_bytes
    # Already declares a charset somewhere in <head>? leave it alone.
    head_end = html_bytes.find(b"</head>")
    head_segment = html_bytes[:head_end] if head_end != -1 else html_bytes[:4096]
    lowered = head_segment.lower()
    if b"charset=" in lowered:
        return html_bytes
    # Insert right after <head ...> opening tag so it sits before any
    # other element. Fall back to prepending if no <head> exists.
    head_open_end = html_bytes.find(b">", html_bytes.lower().find(b"<head"))
    if head_open_end == -1:
        return _META_CHARSET + html_bytes
    return (
        html_bytes[: head_open_end + 1]
        + _META_CHARSET
        + html_bytes[head_open_end + 1 :]
    )


def _fetch_minutes(
    client: SakarmaClient,
    state: FormState,
    row: Any,
) -> tuple[FormState, bytes]:
    """Select the grid row and fetch Minutes HTML bytes."""
    if row.dashboard_grid_select_index is None:
        raise ValueError(
            f"Manifest row id={row.id} has no dashboard_grid_select_index; "
            "cannot fetch Minutes"
        )
    new_state = client.select_grid_row(state, row.dashboard_grid_select_index)
    path = _resolve_artifact_path(new_state.raw_html, settings.scraper_minutes_path)
    minutes_bytes = client.fetch_public_page(path)
    return new_state, _ensure_charset_meta(minutes_bytes)


def _fetch_dr(
    client: SakarmaClient,
    state: FormState,
    row: Any,
) -> tuple[FormState, bytes]:
    """Trigger the DR link and fetch DR HTML bytes."""
    if row.dashboard_grid_select_index is None:
        raise ValueError(
            f"Manifest row id={row.id} has no dashboard_grid_select_index; "
            "cannot fetch DR"
        )
    new_state = client.click_dr(state, row.dashboard_grid_select_index)
    path = _resolve_artifact_path(new_state.raw_html, settings.scraper_dregister_path)
    dr_bytes = client.fetch_public_page(path)
    return new_state, _ensure_charset_meta(dr_bytes)


def _sha256_hex(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()
