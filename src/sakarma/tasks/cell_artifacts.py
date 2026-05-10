"""Per-cell artifact Celery task.

Wraps :func:`sakarma.tasks.artifacts.run_for_cell` with a Celery task that
owns its own DB session, HTTP session, and rate-limited client. Designed to
be fanned out by the orchestrator chord — many cell tasks per LB run in
parallel, each driving its own dashboard prime against the source portal.
"""

from __future__ import annotations

import requests
import structlog

from sakarma.config import settings as sakarma_settings
from sakarma.db.repositories import (
    LBRepository,
    MainGroupValueRepository,
    MeetingArtifactRepository,
    MeetingManifestRepository,
    YearRepository,
)
from sakarma.db.session import get_session
from sakarma.scraper.client import SakarmaClient
from sakarma.storage.gcs import get_storage
from sakarma.tasks import artifacts
from sakarma.tasks.artifacts import ArtifactsRepos
from sakarma.tasks.celery_app import SakarmaTask, celery_app
from sakarma.utils.rate_limiter import get_rate_limiter

logger = structlog.get_logger(__name__)


@celery_app.task(
    bind=True,
    base=SakarmaTask,
    name="sakarma.tasks.cell_artifacts.scrape_artifacts_cell",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=60,
)
def scrape_artifacts_cell(
    self,
    scrape_run_id: int,
    lb_id: int,
    year_id: int,
    main_group_value_id: int,
) -> dict:
    """Process one (year_id, main_group_value_id) cell for one LB.

    Owns a fresh ``requests.Session`` for the cell so multiple cells inside
    the same LB do not contend on one cookie jar / VIEWSTATE chain.
    Idempotent: rows already having both Minutes and DR are skipped, and
    artifact upserts deduplicate on ``(meeting_manifest_id, content_hash)``.
    """
    log = logger.bind(
        scrape_run_id=scrape_run_id,
        lb_id=lb_id,
        year_id=year_id,
        mg_id=main_group_value_id,
        task_id=self.request.id,
    )
    log.info("scrape_artifacts_cell.start")

    with get_session() as db_session:
        repos = ArtifactsRepos(
            lb_repo=LBRepository(db_session),
            year_repo=YearRepository(db_session),
            main_group_value_repo=MainGroupValueRepository(db_session),
            meeting_manifest_repo=MeetingManifestRepository(db_session),
            meeting_artifact_repo=MeetingArtifactRepository(db_session),
            lb_progress_repo=None,  # cells do not touch lb_progress
        )

        http_session = requests.Session()
        try:
            client = SakarmaClient(
                http_session,
                sakarma_settings,
                get_rate_limiter(),
                logger=logger,
            )
            storage = get_storage()
            summary = artifacts.run_for_cell(
                client=client,
                storage=storage,
                repos=repos,
                lb_id=lb_id,
                scrape_run_id=scrape_run_id,
                year_id=year_id,
                main_group_value_id=main_group_value_id,
            )
            db_session.commit()
            log.info("scrape_artifacts_cell.complete", **summary)
            return summary
        finally:
            http_session.close()
