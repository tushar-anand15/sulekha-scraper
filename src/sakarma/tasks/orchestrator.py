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

            # ---- Stage: artifacts ----------------------------------------
            progress_repo.mark_stage(lb_progress_id, "artifacts")
            artifacts_repos = ArtifactsRepos(
                lb_repo=lb_repo,
                year_repo=year_repo,
                main_group_value_repo=mg_repo,
                meeting_manifest_repo=manifest_repo,
                meeting_artifact_repo=artifact_repo,
                lb_progress_repo=progress_repo,
            )
            artifacts_summary = artifacts.run_for_lb(
                client, storage, artifacts_repos, lb_id, scrape_run_id
            )
            log.info("scrape_lb.artifacts_done", **artifacts_summary)

            # ---- Stage: reconciliation (pure DB, no HTTP) ----------------
            progress_repo.mark_stage(lb_progress_id, "reconcile")
            recon_repos = ReconciliationRepos(
                dashboard_kpi_snapshot_repo=kpi_repo,
                meeting_manifest_repo=manifest_repo,
                reconciliation_repo=recon_repo,
            )
            recon_summary = reconciliation.run_for_lb(
                recon_repos, lb_id, scrape_run_id
            )
            log.info("scrape_lb.reconciliation_done", **recon_summary)

            # ---- Mark success -------------------------------------------
            progress_repo.mark_done(lb_progress_id)
            db_session.commit()

            summary = {
                "lb_id": lb_id,
                "scrape_run_id": scrape_run_id,
                "manifest": manifest_summary,
                "artifacts": artifacts_summary,
                "reconciliation": recon_summary,
            }
            log.info("scrape_lb.complete", **summary)
            return summary

        except Exception as exc:
            progress_repo.mark_error(lb_progress_id, error_message=repr(exc))
            # Commit the error state even though we re-raise — the orchestrator
            # must persist the error so operators can inspect lb_progress.
            db_session.commit()
            log.error("scrape_lb.failed", lb_id=lb_id, error=repr(exc))
            raise

        finally:
            http_session.close()
