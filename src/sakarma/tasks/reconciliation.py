"""Reconciliation task for the SAKARMA scraper pipeline.

After the manifest stage completes for a local body (LB), this module
computes and persists ``reconciliation`` rows that compare dashboard KPI
counts against parsed manifest row counts per
(year × main_group_value × category).

The core logic lives in :func:`run_for_lb` which is a **pure DB operation** —
no HTTP calls.  The Celery task :func:`run` wraps it for async dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from sakarma.db.models import (
    CATEGORY_APPROVED,
    CATEGORY_CANCELLED,
    CATEGORY_INCOMPLETE,
    CATEGORY_ONGOING,
)
from sakarma.db.repositories import (
    DashboardKPISnapshotRepository,
    MeetingManifestRepository,
    ReconciliationRepository,
)
from sakarma.db.session import get_session
from sakarma.tasks.celery_app import SakarmaTask, celery_app

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

# Ordered list of all drill-down categories.
_ALL_CATEGORIES: list[int] = [
    CATEGORY_ONGOING,
    CATEGORY_APPROVED,
    CATEGORY_INCOMPLETE,
    CATEGORY_CANCELLED,
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _Repos:
    """Thin container for the three repositories used by reconciliation."""

    dashboard_kpi_snapshot_repo: DashboardKPISnapshotRepository
    meeting_manifest_repo: MeetingManifestRepository
    reconciliation_repo: ReconciliationRepository


def _kpi_count_for_category(snapshot: Any, category: int) -> int:
    """Map a category constant to the corresponding KPI counter on *snapshot*."""
    if category == CATEGORY_ONGOING:
        return int(snapshot.ongoing)
    if category == CATEGORY_APPROVED:
        return int(snapshot.minutes_complete)
    if category == CATEGORY_INCOMPLETE:
        return int(snapshot.minutes_incomplete)
    if category == CATEGORY_CANCELLED:
        return int(snapshot.cancelled)
    raise ValueError(f"Unknown category: {category}")  # pragma: no cover


def _determine_status(dashboard_kpi_count: int, manifest_row_count: int) -> str:
    """Compute the reconciliation status string.

    Rules (in priority order):
    - Both zero → ``matched``
    - delta == 0 (non-zero counts match) → ``matched``
    - KPI > 0 but manifest == 0 → ``missing_manifest``
    - manifest > 0 but KPI == 0, or any other mismatch → ``mismatch``
    """
    if dashboard_kpi_count == 0 and manifest_row_count == 0:
        return "matched"
    delta = dashboard_kpi_count - manifest_row_count
    if delta == 0:
        return "matched"
    if dashboard_kpi_count > 0 and manifest_row_count == 0:
        return "missing_manifest"
    return "mismatch"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_for_lb(repos: _Repos, lb_id: int, scrape_run_id: int) -> dict[str, int]:
    """Compute reconciliation rows for one LB.  Pure DB op — no HTTP.

    For each ``dashboard_kpi_snapshot`` row written for ``(lb_id,
    scrape_run_id)``, look up the corresponding ``meeting_manifest`` counts
    grouped by category, and write one reconciliation row per
    ``(lb_id, year_id, main_group_value_id, category)``.

    Additionally, any manifest group that has rows but lacks a KPI snapshot
    gets a reconciliation row with ``status=missing_kpi``.

    Args:
        repos: Container holding the three required repositories.
        lb_id: Primary key of the local body being reconciled.
        scrape_run_id: Primary key of the current scrape run.

    Returns:
        Summary dict with keys ``reconciliation_rows``, ``matched``,
        ``mismatch``, ``missing_kpi``, ``missing_manifest``.
    """
    log = logger.bind(lb_id=lb_id, scrape_run_id=scrape_run_id)
    log.info("reconciliation.run_for_lb started")

    rows: list[dict[str, Any]] = []

    # Track which (year_id, main_group_value_id) cells were covered by a KPI
    # snapshot so we can detect orphan manifest groups afterwards.
    snapshot_cells: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    # Step 1 + 2 + 3: KPI snapshots → manifest counts → reconciliation rows
    # ------------------------------------------------------------------
    snapshots = repos.dashboard_kpi_snapshot_repo.list_for_lb_run(lb_id, scrape_run_id)

    for snapshot in snapshots:
        year_id: int = int(snapshot.year_id)
        mgv_id: int = int(snapshot.main_group_value_id)
        snapshot_cells.add((year_id, mgv_id))

        for category in _ALL_CATEGORIES:
            dashboard_kpi_count = _kpi_count_for_category(snapshot, category)
            manifest_row_count = repos.meeting_manifest_repo.count_by_lb_year_group_category(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=category,
            )
            delta = dashboard_kpi_count - manifest_row_count
            status = _determine_status(dashboard_kpi_count, manifest_row_count)

            rows.append(
                {
                    "scrape_run_id": scrape_run_id,
                    "lb_id": lb_id,
                    "year_id": year_id,
                    "main_group_value_id": mgv_id,
                    "category": category,
                    "dashboard_kpi_count": dashboard_kpi_count,
                    "manifest_row_count": manifest_row_count,
                    "delta": delta,
                    "status": status,
                }
            )

    # ------------------------------------------------------------------
    # Step 4: Detect manifest groups with no corresponding KPI snapshot
    # ------------------------------------------------------------------
    manifest_groups = repos.meeting_manifest_repo.list_groups_for_lb_run(
        lb_id=lb_id, scrape_run_id=scrape_run_id
    )

    for year_id, mgv_id, category in manifest_groups:
        # Only add a MISSING_KPI row when this (year, group) pair was NOT
        # covered by any KPI snapshot at all.
        if (year_id, mgv_id) not in snapshot_cells:
            manifest_row_count = repos.meeting_manifest_repo.count_by_lb_year_group_category(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=category,
            )
            rows.append(
                {
                    "scrape_run_id": scrape_run_id,
                    "lb_id": lb_id,
                    "year_id": year_id,
                    "main_group_value_id": mgv_id,
                    "category": category,
                    "dashboard_kpi_count": 0,
                    "manifest_row_count": manifest_row_count,
                    "delta": -manifest_row_count,
                    "status": "missing_kpi",
                }
            )

    # ------------------------------------------------------------------
    # Step 5: Upsert all rows (idempotent on composite key)
    # ------------------------------------------------------------------
    repos.reconciliation_repo.upsert_many(rows)

    # ------------------------------------------------------------------
    # Step 6: Aggregate stats
    # ------------------------------------------------------------------
    matched = sum(1 for r in rows if r["status"] == "matched")
    mismatch = sum(1 for r in rows if r["status"] == "mismatch")
    missing_kpi = sum(1 for r in rows if r["status"] == "missing_kpi")
    missing_manifest = sum(1 for r in rows if r["status"] == "missing_manifest")

    summary: dict[str, int] = {
        "reconciliation_rows": len(rows),
        "matched": matched,
        "mismatch": mismatch,
        "missing_kpi": missing_kpi,
        "missing_manifest": missing_manifest,
    }

    log.info(
        "reconciliation.run_for_lb complete",
        total_cells=len(rows),
        **summary,
    )

    # ------------------------------------------------------------------
    # Step 7: Return summary
    # ------------------------------------------------------------------
    return summary


# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    base=SakarmaTask,
    name="sakarma.tasks.reconciliation.run",
)
def run(self, lb_id: int, scrape_run_id: int) -> dict[str, int]:
    """Celery task: compute and persist reconciliation rows for one LB.

    Args:
        lb_id: Primary key of the local body.
        scrape_run_id: Primary key of the current scrape run.

    Returns:
        Summary dict from :func:`run_for_lb`.
    """
    log = logger.bind(
        lb_id=lb_id,
        scrape_run_id=scrape_run_id,
        task_id=self.request.id,
    )
    log.info("reconciliation task started")

    with get_session() as session:
        repos = _Repos(
            dashboard_kpi_snapshot_repo=DashboardKPISnapshotRepository(session),
            meeting_manifest_repo=MeetingManifestRepository(session),
            reconciliation_repo=ReconciliationRepository(session),
        )
        summary = run_for_lb(repos, lb_id=lb_id, scrape_run_id=scrape_run_id)

    log.info("reconciliation task complete", **summary)
    return summary
