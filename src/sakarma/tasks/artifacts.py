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
from sakarma.scraper.client import SakarmaClient, SessionExpiredError
from sakarma.scraper.parsers import parse_attachment_links
from sakarma.scraper.protocol import (
    BTN_APPV_MEETINGS,
    DDL_DISTRICT,
    DDL_LB_NAME,
    DDL_LB_TYPE,
    DDL_MAIN_GROUP,
    DDL_YEAR,
    FormState,
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

    session_recoveries = 0

    # ------------------------------------------------------------------
    # Process each cell
    # ------------------------------------------------------------------
    for (year_id, mg_id), cell_rows_iter in groupby(all_rows_sorted, key=_cell_key):
        cell_rows = list(cell_rows_iter)
        year = _year_int(year_id)
        mg_ddl = _mgv_ddl(mg_id)

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
                except Exception as exc:
                    log.error(
                        "artifacts.minutes_error",
                        manifest_id=row.id,
                        error=str(exc),
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
                except Exception as exc:
                    log.warning(
                        "artifacts.dr_refetch_for_attachments_failed",
                        manifest_id=row.id,
                        error=str(exc),
                    )
                    dr_bytes = b""

            if dr_bytes:
                attachments = parse_attachment_links(dr_bytes)
                for decision_index, target in attachments:
                    if not target:
                        log.debug(
                            "artifacts.attachment_empty_target_skipped",
                            manifest_id=row.id,
                            decision_index=decision_index,
                        )
                        continue

                    try:
                        pdf_bytes, original_filename = (
                            client.click_attachment_lnkfileview(state, target)
                        )
                    except Exception as exc:
                        log.warning(
                            "artifacts.attachment_download_failed",
                            manifest_id=row.id,
                            target=target,
                            error=str(exc),
                        )
                        continue

                    # Compute hash BEFORE building path (path embeds sha8 prefix).
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
                    log.debug(
                        "artifacts.attachment_uploaded",
                        manifest_id=row.id,
                        decision_index=decision_index,
                        gcs_path=gcs_path_att,
                    )

            rows_processed += 1

        log.debug(
            "artifacts.cell_done",
            year_id=year_id,
            mg_id=mg_id,
        )

    summary = {
        "minutes_uploaded": minutes_uploaded,
        "dr_uploaded": dr_uploaded,
        "attachments_uploaded": attachments_uploaded,
        "rows_processed": rows_processed,
        "rows_skipped": rows_skipped,
    }
    log.info("artifacts.done", **summary)
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
    state = client.load_page(settings.scraper_lbwise_path)
    state = client.select_dropdown(state, DDL_DISTRICT, str(district_id))
    state = client.select_dropdown(state, DDL_LB_TYPE, str(lb_type_id))
    state = client.select_dropdown(state, DDL_LB_NAME, str(lb_id))
    state = client.select_dropdown(state, DDL_YEAR, str(year_id))
    state = client.select_dropdown(state, DDL_MAIN_GROUP, str(mg_ddl_value))
    state = client.click_button(state, BTN_APPV_MEETINGS)
    return state


def _fetch_minutes(
    client: SakarmaClient,
    state: FormState,
    row: Any,
) -> tuple[FormState, bytes]:
    """Select the grid row and fetch Minutes HTML bytes.

    Returns the updated ``state`` (after select_grid_row) and the raw bytes.
    ``row.dashboard_grid_select_index`` must be non-None for this to succeed.
    """
    if row.dashboard_grid_select_index is None:
        raise ValueError(
            f"Manifest row id={row.id} has no dashboard_grid_select_index; "
            "cannot fetch Minutes"
        )
    new_state = client.select_grid_row(state, row.dashboard_grid_select_index)
    minutes_bytes = client.fetch_public_page(settings.scraper_minutes_path)
    return new_state, minutes_bytes


def _fetch_dr(
    client: SakarmaClient,
    state: FormState,
    row: Any,
) -> tuple[FormState, bytes]:
    """Trigger the DR link and fetch DR HTML bytes.

    Returns the updated ``state`` (after click_dr) and the raw bytes.
    Uses ``row.dashboard_grid_select_index`` as the row_index argument to
    ``click_dr`` — the grid row index identifies which row's DR link to fire.
    """
    if row.dashboard_grid_select_index is None:
        raise ValueError(
            f"Manifest row id={row.id} has no dashboard_grid_select_index; "
            "cannot fetch DR"
        )
    new_state = client.click_dr(state, row.dashboard_grid_select_index)
    dr_bytes = client.fetch_public_page(settings.scraper_dregister_path)
    return new_state, dr_bytes


def _sha256_hex(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()
