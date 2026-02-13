"""Phase runner module for controlled pipeline execution.

This module provides the PhaseRunner class which runs each phase
of the pipeline with proper checks, waiting, and progress reporting.
"""

import time
from typing import Optional

import structlog

from sulekha.config import settings
from sulekha.db.models import DistrictStatus, LocalBodyStatus, PdfStatus
from sulekha.db.repositories import (
    DistrictRepository,
    LocalBodyRepository,
    ProjectRepository,
    ScrapeRunRepository,
)
from sulekha.db.session import get_session
from sulekha.tasks.scheduler import (
    can_enqueue,
    get_queue_size,
    wait_for_queue_space,
)

logger = structlog.get_logger(__name__)


class PhaseStatus:
    """Status information for a phase."""

    def __init__(
        self,
        phase: int,
        name: str,
        total: int = 0,
        pending: int = 0,
        in_progress: int = 0,
        done: int = 0,
        error: int = 0,
        skipped: bool = False,
        message: str = "",
    ):
        self.phase = phase
        self.name = name
        self.total = total
        self.pending = pending
        self.in_progress = in_progress
        self.done = done
        self.error = error
        self.skipped = skipped
        self.message = message

    @property
    def is_complete(self) -> bool:
        """Check if phase is complete (all done or no pending/in_progress)."""
        return self.total > 0 and self.pending == 0 and self.in_progress == 0

    @property
    def progress_pct(self) -> float:
        """Get progress percentage."""
        if self.total == 0:
            return 0.0
        return (self.done / self.total) * 100

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "phase": self.phase,
            "name": self.name,
            "total": self.total,
            "pending": self.pending,
            "in_progress": self.in_progress,
            "done": self.done,
            "error": self.error,
            "skipped": self.skipped,
            "message": self.message,
            "is_complete": self.is_complete,
            "progress_pct": round(self.progress_pct, 1),
        }


class PhaseRunner:
    """Runs pipeline phases with status checks and controlled task scheduling."""

    def __init__(
        self,
        batch_size: Optional[int] = None,
        max_queue_size: Optional[int] = None,
        check_interval: Optional[float] = None,
    ):
        """Initialize the phase runner.

        Args:
            batch_size: Number of tasks to enqueue per batch
            max_queue_size: Maximum queue size before pausing
            check_interval: Seconds between status checks
        """
        self.batch_size = batch_size or settings.default_batch_size
        self.max_queue_size = max_queue_size or settings.max_queue_size
        self.check_interval = check_interval or settings.queue_check_interval
        self._db_initialized = False

    def _ensure_db_initialized(self) -> None:
        """Ensure database tables exist."""
        if self._db_initialized:
            return

        from sulekha.db.session import init_db

        logger.info("Initializing database tables...")
        init_db()
        self._db_initialized = True
        logger.info("Database tables initialized")

    def get_phase1_status(self) -> PhaseStatus:
        """Get status of Phase 1 (Discovery)."""
        try:
            with get_session() as session:
                district_repo = DistrictRepository(session)
                counts = district_repo.count_by_status()

                total = sum(counts.values())
                return PhaseStatus(
                    phase=1,
                    name="Discovery",
                    total=total,
                    pending=counts.get("PENDING", 0),
                    in_progress=counts.get("IN_PROGRESS", 0),
                    done=counts.get("DONE", 0),
                    error=counts.get("ERROR", 0),
                )
        except Exception as e:
            # Handle case where table doesn't exist yet
            if "does not exist" in str(e) or "UndefinedTable" in str(type(e).__name__):
                logger.warning("Districts table does not exist, initializing database...")
                self._ensure_db_initialized()
                return PhaseStatus(phase=1, name="Discovery", total=0)
            raise

    def get_phase2_status(self) -> PhaseStatus:
        """Get status of Phase 2 (Local Bodies)."""
        try:
            with get_session() as session:
                lb_repo = LocalBodyRepository(session)
                counts = lb_repo.count_by_status()

                total = sum(counts.values())
                return PhaseStatus(
                    phase=2,
                    name="Local Bodies",
                    total=total,
                    pending=counts.get("PENDING", 0) + counts.get("PARTIAL", 0),
                    in_progress=counts.get("IN_PROGRESS", 0),
                    done=counts.get("DONE", 0),
                    error=counts.get("ERROR", 0),
                )
        except Exception as e:
            if "does not exist" in str(e) or "UndefinedTable" in str(type(e).__name__):
                self._ensure_db_initialized()
                return PhaseStatus(phase=2, name="Local Bodies", total=0)
            raise

    def get_phase3_status(self) -> PhaseStatus:
        """Get status of Phase 3 (Projects)."""
        try:
            with get_session() as session:
                lb_repo = LocalBodyRepository(session)
                counts = lb_repo.count_by_status()

                total = sum(counts.values())
                return PhaseStatus(
                    phase=3,
                    name="Project Tables",
                    total=total,
                    pending=counts.get("PENDING", 0) + counts.get("PARTIAL", 0),
                    in_progress=counts.get("IN_PROGRESS", 0),
                    done=counts.get("DONE", 0),
                    error=counts.get("ERROR", 0),
                )
        except Exception as e:
            if "does not exist" in str(e) or "UndefinedTable" in str(type(e).__name__):
                self._ensure_db_initialized()
                return PhaseStatus(phase=3, name="Project Tables", total=0)
            raise

    def get_phase4_status(self) -> PhaseStatus:
        """Get status of Phase 4 (PDFs)."""
        try:
            with get_session() as session:
                project_repo = ProjectRepository(session)
                counts = project_repo.count_by_pdf_status()

                total = sum(counts.values())
                done = counts.get("DOWNLOADED", 0) + counts.get("MISSING", 0)
                return PhaseStatus(
                    phase=4,
                    name="PDFs",
                    total=total,
                    pending=counts.get("PENDING", 0),
                    in_progress=counts.get("DOWNLOADING", 0),
                    done=done,
                    error=counts.get("ERROR", 0),
                )
        except Exception as e:
            if "does not exist" in str(e) or "UndefinedTable" in str(type(e).__name__):
                self._ensure_db_initialized()
                return PhaseStatus(phase=4, name="PDFs", total=0)
            raise

    def run_discovery(self, skip_if_exists: bool = True) -> PhaseStatus:
        """Run Phase 1: Discover all districts.

        Args:
            skip_if_exists: Skip if districts already exist in database

        Returns:
            PhaseStatus with results
        """
        from sulekha.tasks.discovery import discover_all_districts

        logger.info("Starting Phase 1: Discovery", skip_if_exists=skip_if_exists)

        # Check if we should skip
        status = self.get_phase1_status()
        if skip_if_exists and status.total > 0:
            if status.is_complete:
                status.skipped = True
                status.message = f"Skipped: All {status.total} districts already discovered"
                logger.info(status.message)
                return status
            else:
                logger.info(
                    "Resuming discovery",
                    total=status.total,
                    done=status.done,
                    pending=status.pending,
                )

        # Run discovery task synchronously
        logger.info("Running discover_all_districts task")
        result = discover_all_districts.apply()

        if result.successful():
            task_result = result.result
            status = self.get_phase1_status()
            status.message = f"Discovered {task_result.get('districts_discovered', 0)} districts"
            logger.info("Phase 1 complete", **status.to_dict())
        else:
            status.message = f"Discovery failed: {result.result}"
            logger.error("Phase 1 failed", error=str(result.result))

        return status

    def run_local_bodies(self, skip_if_exists: bool = True) -> PhaseStatus:
        """Run Phase 2: Discover local bodies for all districts.

        Args:
            skip_if_exists: Skip if all districts are already processed

        Returns:
            PhaseStatus with results
        """
        from sulekha.tasks.discovery import (
            discover_local_bodies_for_district,
            run_phase2,
        )

        logger.info("Starting Phase 2: Local Bodies", skip_if_exists=skip_if_exists)

        # Check prerequisite
        phase1_status = self.get_phase1_status()
        if phase1_status.total == 0:
            status = PhaseStatus(
                phase=2,
                name="Local Bodies",
                message="ERROR: No districts found. Run Phase 1 first.",
            )
            logger.error(status.message)
            return status

        # Check if complete
        if skip_if_exists and phase1_status.is_complete:
            phase2_status = self.get_phase2_status()
            if phase2_status.total > 0:
                status = PhaseStatus(
                    phase=2,
                    name="Local Bodies",
                    total=phase2_status.total,
                    done=phase2_status.done,
                    skipped=True,
                    message=f"Skipped: All districts processed, {phase2_status.total} local bodies exist",
                )
                logger.info(status.message)
                return status

        # Run phase 2 with queue management
        tasks_enqueued = 0
        while True:
            # Check current status
            status = self.get_phase1_status()
            pending = status.pending + status.error
            in_progress = status.in_progress

            # Exit when no more work
            if pending == 0 and in_progress == 0:
                break

            # Wait for queue space if needed
            if not can_enqueue("discovery", self.max_queue_size):
                logger.info(
                    "Waiting for queue space",
                    queue="discovery",
                    current_size=get_queue_size("discovery"),
                    max_size=self.max_queue_size,
                )
                wait_for_queue_space("discovery", self.max_queue_size)

            # Enqueue a batch
            if pending > 0:
                result = run_phase2.apply(args=[self.batch_size])
                if result.successful():
                    batch_enqueued = result.result.get("tasks_enqueued", 0)
                    tasks_enqueued += batch_enqueued
                    logger.info(
                        "Enqueued batch",
                        batch_size=batch_enqueued,
                        total_enqueued=tasks_enqueued,
                    )

                    if batch_enqueued == 0 and in_progress == 0:
                        break

            # Wait before next check
            time.sleep(self.check_interval)

        # Get final status
        final_status = self.get_phase2_status()
        final_status.message = f"Processed {tasks_enqueued} districts, {final_status.total} local bodies discovered"
        logger.info("Phase 2 complete", **final_status.to_dict())
        return final_status

    def run_project_tables(self, skip_if_exists: bool = True) -> PhaseStatus:
        """Run Phase 3: Scrape project tables for all local bodies.

        Args:
            skip_if_exists: Skip if all local bodies are already processed

        Returns:
            PhaseStatus with results
        """
        from sulekha.tasks.table_scraper import run_phase3, scrape_projects_for_local_body

        logger.info("Starting Phase 3: Project Tables", skip_if_exists=skip_if_exists)

        # Check prerequisite
        phase2_status = self.get_phase2_status()
        if phase2_status.total == 0:
            status = PhaseStatus(
                phase=3,
                name="Project Tables",
                message="ERROR: No local bodies found. Run Phase 2 first.",
            )
            logger.error(status.message)
            return status

        # Check if complete
        if skip_if_exists and phase2_status.is_complete:
            status = PhaseStatus(
                phase=3,
                name="Project Tables",
                total=phase2_status.total,
                done=phase2_status.done,
                skipped=True,
                message=f"Skipped: All {phase2_status.total} local bodies already processed",
            )
            logger.info(status.message)
            return status

        # Run phase 3 with queue management
        tasks_enqueued = 0
        while True:
            # Check current status
            status = self.get_phase3_status()
            pending = status.pending + status.error
            in_progress = status.in_progress

            # Exit when no more work
            if pending == 0 and in_progress == 0:
                break

            # Wait for queue space if needed
            if not can_enqueue("scraper", self.max_queue_size):
                logger.info(
                    "Waiting for queue space",
                    queue="scraper",
                    current_size=get_queue_size("scraper"),
                    max_size=self.max_queue_size,
                )
                wait_for_queue_space("scraper", self.max_queue_size)

            # Enqueue a batch
            if pending > 0:
                result = run_phase3.apply(args=[self.batch_size])
                if result.successful():
                    batch_enqueued = result.result.get("tasks_enqueued", 0)
                    tasks_enqueued += batch_enqueued
                    logger.info(
                        "Enqueued batch",
                        batch_size=batch_enqueued,
                        total_enqueued=tasks_enqueued,
                    )

                    if batch_enqueued == 0 and in_progress == 0:
                        break

            # Wait before next check
            time.sleep(self.check_interval)

        # Get final status
        final_status = self.get_phase3_status()
        final_status.message = f"Processed {tasks_enqueued} local bodies"
        logger.info("Phase 3 complete", **final_status.to_dict())
        return final_status

    def run_pdfs(self, skip_if_exists: bool = True) -> PhaseStatus:
        """Run Phase 4: Download PDFs for all projects.

        Args:
            skip_if_exists: Skip if all PDFs are already processed

        Returns:
            PhaseStatus with results
        """
        from sulekha.tasks.pdf_scraper import download_pdf_for_project, run_phase4

        logger.info("Starting Phase 4: PDFs", skip_if_exists=skip_if_exists)

        # Check prerequisite
        phase4_status = self.get_phase4_status()
        if phase4_status.total == 0:
            status = PhaseStatus(
                phase=4,
                name="PDFs",
                message="ERROR: No projects found. Run Phase 3 first.",
            )
            logger.error(status.message)
            return status

        # Check if complete
        if skip_if_exists and phase4_status.is_complete:
            status = PhaseStatus(
                phase=4,
                name="PDFs",
                total=phase4_status.total,
                done=phase4_status.done,
                skipped=True,
                message=f"Skipped: All {phase4_status.total} PDFs already processed",
            )
            logger.info(status.message)
            return status

        # Run phase 4 with queue management
        tasks_enqueued = 0
        while True:
            # Check current status
            status = self.get_phase4_status()
            pending = status.pending + status.error
            in_progress = status.in_progress

            # Exit when no more work
            if pending == 0 and in_progress == 0:
                break

            # Wait for queue space if needed
            if not can_enqueue("pdf", self.max_queue_size):
                logger.info(
                    "Waiting for queue space",
                    queue="pdf",
                    current_size=get_queue_size("pdf"),
                    max_size=self.max_queue_size,
                )
                wait_for_queue_space("pdf", self.max_queue_size)

            # Enqueue a batch
            if pending > 0:
                result = run_phase4.apply(args=[self.batch_size])
                if result.successful():
                    batch_enqueued = result.result.get("tasks_enqueued", 0)
                    tasks_enqueued += batch_enqueued
                    logger.info(
                        "Enqueued batch",
                        batch_size=batch_enqueued,
                        total_enqueued=tasks_enqueued,
                    )

                    if batch_enqueued == 0 and in_progress == 0:
                        break

            # Wait before next check
            time.sleep(self.check_interval)

        # Get final status
        final_status = self.get_phase4_status()
        final_status.message = f"Processed {tasks_enqueued} PDFs"
        logger.info("Phase 4 complete", **final_status.to_dict())
        return final_status

    def run_full_pipeline(self, skip_if_exists: bool = True) -> dict:
        """Run all phases of the pipeline sequentially.

        Args:
            skip_if_exists: Skip phases that are already complete

        Returns:
            Dictionary with status for each phase
        """
        logger.info("Starting full pipeline", skip_if_exists=skip_if_exists)

        results = {}

        # Phase 1
        results["phase1"] = self.run_discovery(skip_if_exists).to_dict()
        if "ERROR" in results["phase1"].get("message", ""):
            return results

        # Phase 2
        results["phase2"] = self.run_local_bodies(skip_if_exists).to_dict()
        if "ERROR" in results["phase2"].get("message", ""):
            return results

        # Phase 3
        results["phase3"] = self.run_project_tables(skip_if_exists).to_dict()
        if "ERROR" in results["phase3"].get("message", ""):
            return results

        # Phase 4
        results["phase4"] = self.run_pdfs(skip_if_exists).to_dict()

        logger.info("Full pipeline complete", results=results)
        return results
