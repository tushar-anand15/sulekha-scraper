"""Orchestrator tasks for the Sulekha scraping pipeline.

Coordinates the 4-phase breadth-first scraping workflow:
- Phase 1: Discover all districts
- Phase 2: Discover all local bodies
- Phase 3: Scrape all projects
- Phase 4: Download all PDFs
"""

import time

import structlog

from sulekha.db.models import ScrapeRunStatus
from sulekha.db.repositories import (
    DistrictRepository,
    LocalBodyRepository,
    ProjectRepository,
    ScrapeRunRepository,
)
from sulekha.db.session import get_session
from sulekha.tasks.celery_app import celery_app
from sulekha.tasks.discovery import discover_all_districts, run_phase2
from sulekha.tasks.pdf_scraper import run_phase4
from sulekha.tasks.table_scraper import run_phase3

logger = structlog.get_logger(__name__)


def get_pipeline_status() -> dict:
    """Get current status of the pipeline across all phases.

    Returns:
        Dictionary with status counts for each entity type
    """
    with get_session() as session:
        district_repo = DistrictRepository(session)
        lb_repo = LocalBodyRepository(session)
        project_repo = ProjectRepository(session)

        district_status = district_repo.count_by_status()
        lb_status = lb_repo.count_by_status()
        pdf_status = project_repo.count_by_pdf_status()

        return {
            "districts": district_status,
            "local_bodies": lb_status,
            "pdfs": pdf_status,
            "phase1_complete": district_status.get("DONE", 0) > 0
            and district_status.get("PENDING", 0) == 0,
            "phase2_complete": lb_status.get("DONE", 0) > 0
            and lb_status.get("PENDING", 0) == 0,
            "phase3_complete": pdf_status.get("PENDING", 0) == 0
            and pdf_status.get("DOWNLOADED", 0) > 0,
        }


@celery_app.task(
    bind=True,
    name="sulekha.tasks.orchestrator.run_full_pipeline",
    max_retries=0,
)
def run_full_pipeline(self, batch_size: int = 100) -> dict:
    """Run the complete 4-phase scraping pipeline.

    This task orchestrates all phases sequentially, waiting for each
    phase to complete before starting the next.

    Args:
        batch_size: Number of items to process per batch

    Returns:
        Dictionary with pipeline execution statistics
    """
    logger.info("Starting full pipeline execution")

    stats = {
        "run_id": None,
        "phases_completed": [],
        "current_phase": 0,
        "error": None,
    }

    with get_session() as session:
        run_repo = ScrapeRunRepository(session)

        # Create or get existing run
        run = run_repo.get_latest()
        if run and run.status == ScrapeRunStatus.RUNNING:
            stats["run_id"] = run.id
            stats["current_phase"] = run.current_phase
            logger.info("Resuming existing run", run_id=run.id, phase=run.current_phase)
        else:
            run = run_repo.create()
            run_repo.start(run.id)
            session.commit()
            stats["run_id"] = run.id
            logger.info("Created new run", run_id=run.id)

    try:
        # Phase 1: Discover districts
        if stats["current_phase"] <= 1:
            logger.info("Executing Phase 1: Discover districts")
            result = discover_all_districts.apply(args=[stats["run_id"]])
            stats["phases_completed"].append({"phase": 1, "result": result.result})

            with get_session() as session:
                run_repo = ScrapeRunRepository(session)
                run_repo.update_phase(stats["run_id"], 2)
                session.commit()

        # Phase 2: Discover local bodies
        if stats["current_phase"] <= 2:
            logger.info("Executing Phase 2: Discover local bodies")
            
            # Keep running until all districts are processed
            while True:
                status = get_pipeline_status()
                pending = status["districts"].get("PENDING", 0) + status["districts"].get("ERROR", 0)
                in_progress = status["districts"].get("IN_PROGRESS", 0)
                
                # Exit when no pending/error districts AND no in-progress districts
                if pending == 0 and in_progress == 0:
                    break
                
                # Only enqueue new tasks if there are pending districts
                if pending > 0:
                    result = run_phase2.apply(args=[batch_size])
                    if result.result.get("tasks_enqueued", 0) == 0:
                        # No tasks enqueued, but might still have in-progress
                        if in_progress == 0:
                            break
                
                # Wait a bit for in-progress tasks to complete before checking again
                time.sleep(5)

            stats["phases_completed"].append({"phase": 2, "status": "complete"})

            with get_session() as session:
                run_repo = ScrapeRunRepository(session)
                run_repo.update_phase(stats["run_id"], 3)
                session.commit()

        # Phase 3: Scrape projects
        if stats["current_phase"] <= 3:
            logger.info("Executing Phase 3: Scrape projects")
            
            # Keep running until all local bodies are processed
            while True:
                status = get_pipeline_status()
                pending = (
                    status["local_bodies"].get("PENDING", 0)
                    + status["local_bodies"].get("PARTIAL", 0)
                    + status["local_bodies"].get("ERROR", 0)
                )
                in_progress = status["local_bodies"].get("IN_PROGRESS", 0)
                
                # Exit when no pending/error/partial local bodies AND no in-progress
                if pending == 0 and in_progress == 0:
                    break
                
                # Only enqueue new tasks if there are pending local bodies
                if pending > 0:
                    result = run_phase3.apply(args=[batch_size])
                    if result.result.get("tasks_enqueued", 0) == 0:
                        if in_progress == 0:
                            break
                
                # Wait a bit for in-progress tasks to complete before checking again
                time.sleep(5)

            stats["phases_completed"].append({"phase": 3, "status": "complete"})

            with get_session() as session:
                run_repo = ScrapeRunRepository(session)
                run_repo.update_phase(stats["run_id"], 4)
                session.commit()

        # Phase 4: Download PDFs
        if stats["current_phase"] <= 4:
            logger.info("Executing Phase 4: Download PDFs")
            
            # Keep running until all PDFs are processed
            while True:
                status = get_pipeline_status()
                pending = status["pdfs"].get("PENDING", 0) + status["pdfs"].get("ERROR", 0)
                downloading = status["pdfs"].get("DOWNLOADING", 0)
                
                # Exit when no pending/error PDFs AND no downloading
                if pending == 0 and downloading == 0:
                    break
                
                # Only enqueue new tasks if there are pending PDFs
                if pending > 0:
                    result = run_phase4.apply(args=[batch_size])
                    if result.result.get("tasks_enqueued", 0) == 0:
                        if downloading == 0:
                            break
                
                # Wait a bit for downloading tasks to complete before checking again
                time.sleep(5)

            stats["phases_completed"].append({"phase": 4, "status": "complete"})

        # Mark run as complete
        with get_session() as session:
            run_repo = ScrapeRunRepository(session)
            run_repo.complete(stats["run_id"])
            session.commit()

        logger.info("Pipeline execution complete", **stats)

    except Exception as e:
        stats["error"] = str(e)
        logger.error("Pipeline execution failed", error=str(e))

        with get_session() as session:
            run_repo = ScrapeRunRepository(session)
            run_repo.fail(stats["run_id"], str(e))
            session.commit()

        raise

    return stats


@celery_app.task(
    bind=True,
    name="sulekha.tasks.orchestrator.run_phase",
)
def run_phase(self, phase: int, batch_size: int = 100) -> dict:
    """Run a specific phase of the pipeline.

    Args:
        phase: Phase number (1-4)
        batch_size: Number of items to process

    Returns:
        Dictionary with phase execution results
    """
    logger.info("Running phase", phase=phase, batch_size=batch_size)

    if phase == 1:
        return discover_all_districts.apply().result
    elif phase == 2:
        return run_phase2.apply(args=[batch_size]).result
    elif phase == 3:
        return run_phase3.apply(args=[batch_size]).result
    elif phase == 4:
        return run_phase4.apply(args=[batch_size]).result
    else:
        raise ValueError(f"Invalid phase: {phase}")


@celery_app.task(
    bind=True,
    name="sulekha.tasks.orchestrator.get_progress",
)
def get_progress(self) -> dict:
    """Get current progress of the scraping pipeline.

    Returns:
        Dictionary with progress statistics for all phases, including year-wise breakdowns
    """
    with get_session() as session:
        district_repo = DistrictRepository(session)
        lb_repo = LocalBodyRepository(session)
        project_repo = ProjectRepository(session)

        # Get overall status
        district_status = district_repo.count_by_status()
        lb_status = lb_repo.count_by_status()
        pdf_status = project_repo.count_by_pdf_status()

        # Get year-wise breakdowns
        districts_by_year = district_repo.count_by_year()
        districts_by_year_district = district_repo.count_by_year_district()
        lbs_by_year = lb_repo.count_by_year()
        lbs_by_year_district = lb_repo.count_by_year_district()
        pdfs_by_year = project_repo.count_pdfs_by_year()
        pdfs_by_year_district = project_repo.count_pdfs_by_year_district()

        # Calculate totals
        total_districts = sum(district_status.values())
        done_districts = district_status.get("DONE", 0)

        total_lbs = sum(lb_status.values())
        done_lbs = lb_status.get("DONE", 0)

        total_pdfs = sum(pdf_status.values())
        downloaded_pdfs = pdf_status.get("DOWNLOADED", 0)
        missing_pdfs = pdf_status.get("MISSING", 0)

        phase1_complete = district_status.get("DONE", 0) > 0 and district_status.get("PENDING", 0) == 0
        phase3_complete = pdf_status.get("PENDING", 0) == 0 and pdf_status.get("DOWNLOADED", 0) > 0

        return {
            "phase1": {
                "name": "Discover Districts",
                "complete": total_districts > 0,
                "total": total_districts,
                "done": total_districts,
                "percent": 100.0 if total_districts > 0 else 0,
            },
            "phase2": {
                "name": "Discover Local Bodies",
                "complete": phase1_complete,
                "total": total_districts,
                "done": done_districts,
                "percent": round(done_districts / total_districts * 100, 1) if total_districts > 0 else 0,
                "status": district_status,
                "by_year": districts_by_year,
                "by_year_district": districts_by_year_district,
            },
            "phase3": {
                "name": "Scrape Projects",
                "complete": phase3_complete,
                "total": total_lbs,
                "done": done_lbs,
                "percent": round(done_lbs / total_lbs * 100, 1) if total_lbs > 0 else 0,
                "status": lb_status,
                "by_year": lbs_by_year,
                "by_year_district": lbs_by_year_district,
            },
            "phase4": {
                "name": "Download PDFs",
                "total": total_pdfs,
                "downloaded": downloaded_pdfs,
                "missing": missing_pdfs,
                "percent": round((downloaded_pdfs + missing_pdfs) / total_pdfs * 100, 1)
                if total_pdfs > 0
                else 0,
                "status": pdf_status,
                "by_year": pdfs_by_year,
                "by_year_district": pdfs_by_year_district,
            },
        }


# Beat schedule for automatic pipeline execution
celery_app.conf.beat_schedule = {
    # Run the full pipeline daily at 2 AM
    "daily-pipeline": {
        "task": "sulekha.tasks.orchestrator.run_full_pipeline",
        "schedule": 86400,  # 24 hours in seconds
        "args": [100],
    },
    # Check progress every hour
    "hourly-progress-check": {
        "task": "sulekha.tasks.orchestrator.get_progress",
        "schedule": 3600,  # 1 hour
    },
}
