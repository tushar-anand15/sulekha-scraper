"""Per-LB orchestrator task: manifest → artifacts → reconciliation.

Owns a single ``requests.Session`` for the full LB scrape lifetime so that
session-bound navigation (PublicMinutes / PublicDRegister) shares the same
cookie jar across all three stages.

This is the Unit-12 integration point where the plain-function modules
``manifest``, ``artifacts``, and ``reconciliation`` are composed into a
single Celery task with ``lb_progress`` tracking at every stage boundary.
"""

from __future__ import annotations

import types

import requests
import structlog
from celery import chord, group

from sakarma.config import settings as sakarma_settings
from sakarma.db.repositories import (
    DashboardKPISnapshotRepository,
    LBProgressRepository,
    LBRepository,
    MainGroupValueRepository,
    MeetingArtifactRepository,
    MeetingManifestRepository,
    ReconciliationRepository,
    YearRepository,
)
from sakarma.db.session import get_session
from sakarma.scraper.client import SakarmaClient
from sakarma.storage.gcs import get_storage
from sakarma.tasks import artifacts, manifest, reconciliation
from sakarma.tasks.artifacts import ArtifactsRepos
from sakarma.tasks.cell_artifacts import scrape_artifacts_cell
from sakarma.tasks.celery_app import SakarmaTask, celery_app
from sakarma.tasks.reconciliation import ReconciliationRepos
from sakarma.utils.rate_limiter import get_rate_limiter

logger = structlog.get_logger(__name__)


@celery_app.task(
    bind=True,
    base=SakarmaTask,
    name="sakarma.tasks.orchestrator.scrape_lb",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=60,
)
def scrape_lb(self, scrape_run_id: int, lb_id: int) -> dict:
    """Per-LB orchestration: manifest → artifacts → reconciliation in one Session.

    Owns the ``requests.Session`` lifetime for this LB.  All three stages share
    the same session/cookie jar so cross-page session-bound navigation
    (PublicMinutes / PublicDRegister) works correctly.

    Args:
        scrape_run_id: PK of the enclosing :class:`~sakarma.db.models.ScrapeRun`.
        lb_id: PK of the local body to scrape.

    Returns:
        Summary dict with keys ``lb_id``, ``scrape_run_id``, ``manifest``,
        ``artifacts``, and ``reconciliation``.

    Raises:
        Any exception raised by a stage is re-raised after persisting the error
        state so that Celery's ``autoretry_for`` mechanism retries the task.
    """
    log = logger.bind(
        lb_id=lb_id,
        scrape_run_id=scrape_run_id,
        task_id=self.request.id,
    )
    log.info("scrape_lb.start")

    with get_session() as db_session:
        # ------------------------------------------------------------------
        # Construct repositories
        # ------------------------------------------------------------------
        lb_repo = LBRepository(db_session)
        year_repo = YearRepository(db_session)
        mg_repo = MainGroupValueRepository(db_session)
        manifest_repo = MeetingManifestRepository(db_session)
        artifact_repo = MeetingArtifactRepository(db_session)
        kpi_repo = DashboardKPISnapshotRepository(db_session)
        recon_repo = ReconciliationRepository(db_session)
        progress_repo = LBProgressRepository(db_session)

        # ------------------------------------------------------------------
        # Acquire (or create) the lb_progress row
        # ------------------------------------------------------------------
        lb_progress = progress_repo.get_by_run_lb(scrape_run_id, lb_id)
        if lb_progress is None:
            # Create a PENDING row for this LB so mark_in_progress has something
            # to update.  bulk_create is idempotent via ON CONFLICT DO NOTHING.
            progress_repo.bulk_create(scrape_run_id, [lb_id])
            db_session.flush()
            lb_progress = progress_repo.get_by_run_lb(scrape_run_id, lb_id)

        if lb_progress is None:
            raise RuntimeError(
                f"Failed to acquire lb_progress row for "
                f"scrape_run_id={scrape_run_id}, lb_id={lb_id}"
            )

        lb_progress_id: int = lb_progress.id
        progress_repo.mark_in_progress(lb_progress_id)
        # Commit so the dashboard (and any other observer connection) sees
        # this LB as in_progress while we work. Without this commit, the
        # status change stays in our session-local view until the whole LB
        # run completes — which is why "in flight" looked like 0.
        db_session.commit()

        # ------------------------------------------------------------------
        # HTTP session lifetime spans all three stages
        # ------------------------------------------------------------------
        http_session = requests.Session()
        try:
            client = SakarmaClient(
                http_session,
                sakarma_settings,
                get_rate_limiter(),
                logger=logger,
            )
            storage = get_storage()

            # ---- Stage: manifest ----------------------------------------
            # manifest.run_for_lb accepts Any duck-typed repos namespace.
            progress_repo.mark_stage(lb_progress_id, "manifest")
            db_session.commit()  # publish stage transition to dashboard
            manifest_repos = types.SimpleNamespace(
                lb_repo=lb_repo,
                main_group_value_repo=mg_repo,
                meeting_manifest_repo=manifest_repo,
                dashboard_kpi_snapshot_repo=kpi_repo,
                lb_progress_repo=progress_repo,
            )
            manifest_summary = manifest.run_for_lb(
                client, manifest_repos, lb_id, scrape_run_id
            )
            log.info("scrape_lb.manifest_done", **manifest_summary)

            # Commit manifest writes + KPI snapshots so partial progress
            # is visible even if artifacts stage fails partway through.
            db_session.commit()

            # ---- Stage: artifacts (fan out via chord) --------------------
            progress_repo.mark_stage(lb_progress_id, "artifacts")
            db_session.commit()  # publish stage transition to dashboard

            # Enumerate non-empty (year_id, mg_id) cells for this run
            # (manifest stage just finished; rows are committed).
            cells = manifest_repo.list_approved_cells_for_lb_run(
                lb_id, scrape_run_id
            )
            log.info("scrape_lb.cells_enumerated", cell_count=len(cells))

            if not cells:
                # Nothing to fetch — run reconciliation inline + mark done.
                progress_repo.mark_stage(lb_progress_id, "reconcile")
                db_session.commit()
                recon_repos = ReconciliationRepos(
                    dashboard_kpi_snapshot_repo=kpi_repo,
                    meeting_manifest_repo=manifest_repo,
                    reconciliation_repo=recon_repo,
                )
                recon_summary = reconciliation.run_for_lb(
                    recon_repos, lb_id, scrape_run_id
                )
                progress_repo.mark_done(lb_progress_id)
                db_session.commit()
                summary = {
                    "lb_id": lb_id,
                    "scrape_run_id": scrape_run_id,
                    "manifest": manifest_summary,
                    "artifacts": {
                        "minutes_uploaded": 0,
                        "dr_uploaded": 0,
                        "attachments_uploaded": 0,
                        "rows_processed": 0,
                        "rows_skipped": 0,
                        "rows_server_unavailable": 0,
                    },
                    "reconciliation": recon_summary,
                    "dispatched_cells": 0,
                }
                log.info("scrape_lb.complete", **summary)
                return summary

            # Build the chord. The callback (_artifacts_complete) receives
            # the list of cell summaries, runs reconciliation, and marks
            # lb_progress done.
            cell_signatures = [
                scrape_artifacts_cell.s(scrape_run_id, lb_id, year_id, mg_id)
                for (year_id, mg_id) in cells
            ]
            # IMPORTANT: commit + close DB session BEFORE dispatching the
            # chord. Otherwise the cell tasks could pick up stale state, and
            # the parent's transaction would block the cell tasks.
            db_session.commit()

        except Exception as exc:
            progress_repo.mark_error(lb_progress_id, error_message=repr(exc))
            db_session.commit()
            log.error("scrape_lb.failed", lb_id=lb_id, error=repr(exc))
            raise
        finally:
            http_session.close()

    # Dispatch chord OUTSIDE the DB session context. The callback is its
    # own task that re-opens a session and runs reconciliation + mark_done.
    chord(group(cell_signatures))(
        _artifacts_complete.s(scrape_run_id=scrape_run_id, lb_id=lb_id)
    )

    summary = {
        "lb_id": lb_id,
        "scrape_run_id": scrape_run_id,
        "manifest": manifest_summary,
        "dispatched_cells": len(cell_signatures),
    }
    log.info("scrape_lb.dispatched", **summary)
    return summary


@celery_app.task(
    bind=True,
    base=SakarmaTask,
    name="sakarma.tasks.orchestrator._artifacts_complete",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
)
def _artifacts_complete(
    self,
    cell_summaries: list,
    scrape_run_id: int,
    lb_id: int,
) -> dict:
    """Chord callback: aggregate cell summaries, run reconciliation, mark done.

    ``cell_summaries`` is the list of dicts returned by each
    ``scrape_artifacts_cell`` task in the group.
    """
    log = logger.bind(
        lb_id=lb_id,
        scrape_run_id=scrape_run_id,
        task_id=self.request.id,
    )

    # Aggregate per-cell counters.
    agg = {
        "minutes_uploaded": 0,
        "dr_uploaded": 0,
        "attachments_uploaded": 0,
        "rows_processed": 0,
        "rows_skipped": 0,
        "rows_server_unavailable": 0,
    }
    for cs in cell_summaries or []:
        if not isinstance(cs, dict):
            continue
        for k in agg:
            agg[k] += int(cs.get(k, 0) or 0)

    log.info("scrape_lb.artifacts_done", **agg)

    with get_session() as db_session:
        kpi_repo = DashboardKPISnapshotRepository(db_session)
        manifest_repo = MeetingManifestRepository(db_session)
        recon_repo = ReconciliationRepository(db_session)
        progress_repo = LBProgressRepository(db_session)

        lb_progress = progress_repo.get_by_run_lb(scrape_run_id, lb_id)
        if lb_progress is None:
            log.error("artifacts_complete.no_lb_progress")
            return {"lb_id": lb_id, "scrape_run_id": scrape_run_id, "artifacts": agg}

        try:
            progress_repo.mark_stage(lb_progress.id, "reconcile")
            db_session.commit()

            recon_repos = ReconciliationRepos(
                dashboard_kpi_snapshot_repo=kpi_repo,
                meeting_manifest_repo=manifest_repo,
                reconciliation_repo=recon_repo,
            )
            recon_summary = reconciliation.run_for_lb(
                recon_repos, lb_id, scrape_run_id
            )
            log.info("scrape_lb.reconciliation_done", **recon_summary)

            progress_repo.mark_done(lb_progress.id)
            db_session.commit()

            summary = {
                "lb_id": lb_id,
                "scrape_run_id": scrape_run_id,
                "artifacts": agg,
                "reconciliation": recon_summary,
            }
            log.info("scrape_lb.complete", **summary)
            return summary
        except Exception as exc:
            progress_repo.mark_error(lb_progress.id, error_message=repr(exc))
            db_session.commit()
            log.error("artifacts_complete.failed", error=repr(exc))
            raise
