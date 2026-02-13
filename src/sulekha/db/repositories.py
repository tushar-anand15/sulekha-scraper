"""Data access layer for Sulekha service.

Provides repository classes for CRUD operations on all entities.
Uses SQLAlchemy's upsert pattern for idempotent operations.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sulekha.config import settings
from sulekha.db.models import (
    District,
    DistrictStatus,
    GcsUploadStatus,
    LocalBody,
    LocalBodyStatus,
    Pdf,
    PdfStatus,
    Project,
    ScrapeRun,
    ScrapeRunStatus,
)

logger = structlog.get_logger(__name__)


# =============================================================================
# Scrape Run Repository
# =============================================================================


class ScrapeRunRepository:
    """Repository for ScrapeRun operations."""

    def __init__(self, session: Session):
        self.session = session

    def create(self, config: Optional[dict] = None) -> ScrapeRun:
        """Create a new scrape run."""
        run = ScrapeRun(
            id=str(uuid4()),
            current_phase=1,
            status=ScrapeRunStatus.PENDING,
            created_at=datetime.utcnow(),
        )
        self.session.add(run)
        self.session.flush()
        logger.info("Created scrape run", run_id=run.id)
        return run

    def get(self, run_id: str) -> Optional[ScrapeRun]:
        """Get a scrape run by ID."""
        return self.session.get(ScrapeRun, run_id)

    def get_latest(self) -> Optional[ScrapeRun]:
        """Get the most recent scrape run."""
        stmt = select(ScrapeRun).order_by(ScrapeRun.created_at.desc()).limit(1)
        return self.session.execute(stmt).scalar_one_or_none()

    def start(self, run_id: str) -> None:
        """Mark a scrape run as started."""
        stmt = (
            update(ScrapeRun)
            .where(ScrapeRun.id == run_id)
            .values(status=ScrapeRunStatus.RUNNING, started_at=datetime.utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def complete(self, run_id: str) -> None:
        """Mark a scrape run as completed."""
        stmt = (
            update(ScrapeRun)
            .where(ScrapeRun.id == run_id)
            .values(status=ScrapeRunStatus.COMPLETED, completed_at=datetime.utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def fail(self, run_id: str, error: str) -> None:
        """Mark a scrape run as failed."""
        stmt = (
            update(ScrapeRun)
            .where(ScrapeRun.id == run_id)
            .values(
                status=ScrapeRunStatus.FAILED,
                completed_at=datetime.utcnow(),
                error_message=error,
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def update_phase(self, run_id: str, phase: int) -> None:
        """Update the current phase of a scrape run."""
        stmt = update(ScrapeRun).where(ScrapeRun.id == run_id).values(current_phase=phase)
        self.session.execute(stmt)
        self.session.expire_all()


# =============================================================================
# District Repository
# =============================================================================


class DistrictRepository:
    """Repository for District operations."""

    def __init__(self, session: Session):
        self.session = session

    def upsert(
        self,
        year_val: int,
        year_label: str,
        lb_type_val: int,
        lb_type_label: str,
        district_index: int,
        district_name: str,
        postback_argument: str,
        num_local_bodies: Optional[int] = None,
        num_projects: Optional[int] = None,
        scrape_run_id: Optional[str] = None,
    ) -> District:
        """Insert or update a district record.

        Uses PostgreSQL's ON CONFLICT DO UPDATE for idempotency.
        """
        stmt = insert(District).values(
            id=str(uuid4()),
            scrape_run_id=scrape_run_id,
            year_val=year_val,
            year_label=year_label,
            lb_type_val=lb_type_val,
            lb_type_label=lb_type_label,
            district_index=district_index,
            district_name=district_name,
            num_local_bodies=num_local_bodies,
            num_projects=num_projects,
            postback_target="gvState",
            postback_argument=postback_argument,
            status=DistrictStatus.PENDING,
            discovered_at=datetime.utcnow(),
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["year_val", "lb_type_val", "district_index"],
            set_={
                "district_name": district_name,
                "num_local_bodies": num_local_bodies,
                "num_projects": num_projects,
                "postback_argument": postback_argument,
            },
        )

        self.session.execute(stmt)
        self.session.flush()
        self.session.expire_all()

        # Return the district
        return self.get_by_identity(year_val, lb_type_val, district_index)

    def get(self, district_id: str) -> Optional[District]:
        """Get a district by ID."""
        return self.session.get(District, district_id)

    def get_by_identity(
        self, year_val: int, lb_type_val: int, district_index: int
    ) -> Optional[District]:
        """Get a district by its unique identity."""
        stmt = select(District).where(
            District.year_val == year_val,
            District.lb_type_val == lb_type_val,
            District.district_index == district_index,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_pending(self, limit: Optional[int] = None) -> list[District]:
        """Get districts that need local body discovery (Phase 2)."""
        stmt = (
            select(District)
            .where(
                District.status.in_([DistrictStatus.PENDING, DistrictStatus.ERROR]),
                District.retry_count < settings.max_district_retries,
            )
            .order_by(District.year_val.desc(), District.lb_type_val, District.district_index)
        )
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_all_for_year_lb(self, year_val: int, lb_type_val: int) -> list[District]:
        """Get all districts for a year and LB type."""
        stmt = (
            select(District)
            .where(District.year_val == year_val, District.lb_type_val == lb_type_val)
            .order_by(District.district_index)
        )
        return list(self.session.execute(stmt).scalars().all())

    def mark_in_progress(self, district_id: str) -> None:
        """Mark a district as in progress (task enqueued)."""
        stmt = (
            update(District)
            .where(District.id == district_id)
            .values(status=DistrictStatus.IN_PROGRESS)
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_done(self, district_id: str) -> None:
        """Mark a district as done (local bodies discovered)."""
        stmt = (
            update(District)
            .where(District.id == district_id)
            .values(status=DistrictStatus.DONE, last_processed_at=datetime.utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_error(self, district_id: str, error: str) -> None:
        """Mark a district as errored."""
        stmt = (
            update(District)
            .where(District.id == district_id)
            .values(
                status=DistrictStatus.ERROR,
                error_message=error,
                retry_count=District.retry_count + 1,
                last_processed_at=datetime.utcnow(),
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def count_by_status(self) -> dict[str, int]:
        """Get count of districts by status."""
        from sqlalchemy import func

        stmt = select(District.status, func.count(District.id)).group_by(District.status)
        results = self.session.execute(stmt).all()
        return {str(status.value): count for status, count in results}

    def get_random(self, limit: int = 10) -> list[District]:
        """Get random districts.

        Args:
            limit: Maximum number of districts to return

        Returns:
            List of randomly selected districts
        """
        from sqlalchemy.sql.expression import func

        stmt = select(District).order_by(func.random()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_total_count(self) -> int:
        """Get total count of districts."""
        from sqlalchemy import func

        stmt = select(func.count(District.id))
        return self.session.execute(stmt).scalar() or 0

    def is_phase_complete(self) -> bool:
        """Check if Phase 1 is complete (all districts discovered).

        Note: Phase 1 is considered complete if there are districts
        and none are pending/in_progress. Districts with errors
        that have exceeded retry count are considered "done" for this check.
        """
        counts = self.count_by_status()
        total = sum(counts.values())
        if total == 0:
            return False

        pending = counts.get("PENDING", 0)
        in_progress = counts.get("IN_PROGRESS", 0)
        return pending == 0 and in_progress == 0


# =============================================================================
# Local Body Repository
# =============================================================================


class LocalBodyRepository:
    """Repository for LocalBody operations."""

    def __init__(self, session: Session):
        self.session = session

    def upsert(
        self,
        district_id: str,
        lb_index: int,
        lb_name: str,
        postback_argument: str,
        expected_projects: Optional[int] = None,
    ) -> LocalBody:
        """Insert or update a local body record."""
        stmt = insert(LocalBody).values(
            id=str(uuid4()),
            district_id=district_id,
            lb_index=lb_index,
            lb_name=lb_name,
            expected_projects=expected_projects,
            postback_target="gvStat",
            postback_argument=postback_argument,
            status=LocalBodyStatus.PENDING,
            discovered_at=datetime.utcnow(),
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["district_id", "lb_index"],
            set_={
                "lb_name": lb_name,
                "expected_projects": expected_projects,
                "postback_argument": postback_argument,
            },
        )

        self.session.execute(stmt)
        self.session.flush()
        self.session.expire_all()

        return self.get_by_identity(district_id, lb_index)

    def get(self, lb_id: str) -> Optional[LocalBody]:
        """Get a local body by ID."""
        return self.session.get(LocalBody, lb_id)

    def get_by_identity(self, district_id: str, lb_index: int) -> Optional[LocalBody]:
        """Get a local body by its unique identity."""
        stmt = select(LocalBody).where(
            LocalBody.district_id == district_id,
            LocalBody.lb_index == lb_index,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_pending(self, limit: Optional[int] = None) -> list[LocalBody]:
        """Get local bodies that need project scraping (Phase 3)."""
        stmt = (
            select(LocalBody)
            .where(
                LocalBody.status.in_(
                    [LocalBodyStatus.PENDING, LocalBodyStatus.PARTIAL, LocalBodyStatus.ERROR]
                ),
                LocalBody.retry_count < settings.max_lb_retries,
            )
            .order_by(LocalBody.district_id, LocalBody.lb_index)
        )
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_all_for_district(self, district_id: str) -> list[LocalBody]:
        """Get all local bodies for a district."""
        stmt = (
            select(LocalBody)
            .where(LocalBody.district_id == district_id)
            .order_by(LocalBody.lb_index)
        )
        return list(self.session.execute(stmt).scalars().all())

    def mark_in_progress(self, lb_id: str) -> None:
        """Mark a local body as in progress."""
        stmt = (
            update(LocalBody)
            .where(LocalBody.id == lb_id)
            .values(status=LocalBodyStatus.IN_PROGRESS)
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def update_progress(
        self,
        lb_id: str,
        last_page_scraped: int,
        scraped_projects: int,
        total_pages: Optional[int] = None,
    ) -> None:
        """Update scraping progress for a local body."""
        values = {
            "last_page_scraped": last_page_scraped,
            "scraped_projects": scraped_projects,
            "status": LocalBodyStatus.PARTIAL,
            "last_scraped_at": datetime.utcnow(),
        }
        if total_pages is not None:
            values["total_pages"] = total_pages

        stmt = update(LocalBody).where(LocalBody.id == lb_id).values(**values)
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_done(self, lb_id: str, scraped_projects: int) -> None:
        """Mark a local body as done (all projects scraped)."""
        stmt = (
            update(LocalBody)
            .where(LocalBody.id == lb_id)
            .values(
                status=LocalBodyStatus.DONE,
                scraped_projects=scraped_projects,
                last_scraped_at=datetime.utcnow(),
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_error(self, lb_id: str, error: str) -> None:
        """Mark a local body as errored."""
        stmt = (
            update(LocalBody)
            .where(LocalBody.id == lb_id)
            .values(
                status=LocalBodyStatus.ERROR,
                error_message=error,
                retry_count=LocalBody.retry_count + 1,
                last_scraped_at=datetime.utcnow(),
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def count_by_status(self) -> dict[str, int]:
        """Get count of local bodies by status."""
        from sqlalchemy import func

        stmt = select(LocalBody.status, func.count(LocalBody.id)).group_by(LocalBody.status)
        results = self.session.execute(stmt).all()
        return {str(status.value): count for status, count in results}

    def get_random(self, limit: int = 10) -> list[LocalBody]:
        """Get random local bodies.

        Args:
            limit: Maximum number of local bodies to return

        Returns:
            List of randomly selected local bodies
        """
        from sqlalchemy.sql.expression import func

        stmt = select(LocalBody).order_by(func.random()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_random_for_district(self, district_id: str, limit: int = 10) -> list[LocalBody]:
        """Get random local bodies for a specific district.

        Args:
            district_id: ID of the district
            limit: Maximum number of local bodies to return

        Returns:
            List of randomly selected local bodies for the district
        """
        from sqlalchemy.sql.expression import func

        stmt = (
            select(LocalBody)
            .where(LocalBody.district_id == district_id)
            .order_by(func.random())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_total_count(self) -> int:
        """Get total count of local bodies."""
        from sqlalchemy import func

        stmt = select(func.count(LocalBody.id))
        return self.session.execute(stmt).scalar() or 0

    def is_phase_complete(self) -> bool:
        """Check if Phase 2/3 is complete for local bodies.

        Returns True if all local bodies are DONE (no pending/partial/in_progress).
        """
        counts = self.count_by_status()
        total = sum(counts.values())
        if total == 0:
            return False

        pending = counts.get("PENDING", 0)
        partial = counts.get("PARTIAL", 0)
        in_progress = counts.get("IN_PROGRESS", 0)
        return pending == 0 and partial == 0 and in_progress == 0


# =============================================================================
# Project Repository
# =============================================================================


class ProjectRepository:
    """Repository for Project operations."""

    def __init__(self, session: Session):
        self.session = session

    def upsert(
        self,
        local_body_id: str,
        project_no: str,
        project_name: str,
        select_argument: str,
        page_number: int,
        formulation: Optional[str] = None,
        expense: Optional[str] = None,
    ) -> Project:
        """Insert or update a project record."""
        stmt = insert(Project).values(
            id=str(uuid4()),
            local_body_id=local_body_id,
            project_no=project_no,
            project_name=project_name,
            formulation=formulation,
            expense=expense,
            page_number=page_number,
            select_argument=select_argument,
            pdf_status=PdfStatus.PENDING,
            scraped_at=datetime.utcnow(),
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=["local_body_id", "project_no"],
            set_={
                "project_name": project_name,
                "formulation": formulation,
                "expense": expense,
                "select_argument": select_argument,
            },
        )

        self.session.execute(stmt)
        self.session.flush()
        self.session.expire_all()

        return self.get_by_identity(local_body_id, project_no)

    def get(self, project_id: str) -> Optional[Project]:
        """Get a project by ID."""
        return self.session.get(Project, project_id)

    def get_by_identity(self, local_body_id: str, project_no: str) -> Optional[Project]:
        """Get a project by its unique identity."""
        stmt = select(Project).where(
            Project.local_body_id == local_body_id,
            Project.project_no == project_no,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def get_pending_pdfs(self, limit: Optional[int] = None) -> list[Project]:
        """Get projects that need PDF download (Phase 4)."""
        stmt = (
            select(Project)
            .where(
                Project.pdf_status.in_([PdfStatus.PENDING, PdfStatus.ERROR]),
                Project.pdf_retry_count < settings.max_pdf_retries,
            )
            .order_by(Project.local_body_id, Project.project_no)
        )
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_all_for_local_body(self, local_body_id: str) -> list[Project]:
        """Get all projects for a local body."""
        stmt = (
            select(Project)
            .where(Project.local_body_id == local_body_id)
            .order_by(Project.page_number, Project.project_no)
        )
        return list(self.session.execute(stmt).scalars().all())

    def mark_downloading(self, project_id: str) -> None:
        """Mark a project's PDF as downloading."""
        stmt = (
            update(Project)
            .where(Project.id == project_id)
            .values(pdf_status=PdfStatus.DOWNLOADING, pdf_last_attempt_at=datetime.utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_downloaded(self, project_id: str) -> None:
        """Mark a project's PDF as downloaded."""
        stmt = (
            update(Project)
            .where(Project.id == project_id)
            .values(pdf_status=PdfStatus.DOWNLOADED, pdf_last_attempt_at=datetime.utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_missing(self, project_id: str) -> None:
        """Mark a project's PDF as missing (no PDF available)."""
        stmt = (
            update(Project)
            .where(Project.id == project_id)
            .values(pdf_status=PdfStatus.MISSING, pdf_last_attempt_at=datetime.utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_error(self, project_id: str, error: str) -> None:
        """Mark a project's PDF download as errored."""
        stmt = (
            update(Project)
            .where(Project.id == project_id)
            .values(
                pdf_status=PdfStatus.ERROR,
                pdf_error_message=error,
                pdf_retry_count=Project.pdf_retry_count + 1,
                pdf_last_attempt_at=datetime.utcnow(),
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def count_by_pdf_status(self) -> dict[str, int]:
        """Get count of projects by PDF status."""
        from sqlalchemy import func

        stmt = select(Project.pdf_status, func.count(Project.id)).group_by(Project.pdf_status)
        results = self.session.execute(stmt).all()
        return {str(status.value): count for status, count in results}

    def get_random(self, limit: int = 10) -> list[Project]:
        """Get random projects.

        Args:
            limit: Maximum number of projects to return

        Returns:
            List of randomly selected projects
        """
        from sqlalchemy.sql.expression import func

        stmt = select(Project).order_by(func.random()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def get_random_for_local_body(self, local_body_id: str, limit: int = 10) -> list[Project]:
        """Get random projects for a specific local body.

        Args:
            local_body_id: ID of the local body
            limit: Maximum number of projects to return

        Returns:
            List of randomly selected projects for the local body
        """
        from sqlalchemy.sql.expression import func

        stmt = (
            select(Project)
            .where(Project.local_body_id == local_body_id)
            .order_by(func.random())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_total_count(self) -> int:
        """Get total count of projects."""
        from sqlalchemy import func

        stmt = select(func.count(Project.id))
        return self.session.execute(stmt).scalar() or 0

    def is_pdf_phase_complete(self) -> bool:
        """Check if Phase 4 is complete (all PDFs downloaded or marked missing).

        Returns True if no projects have pending/downloading/error PDF status.
        """
        counts = self.count_by_pdf_status()
        total = sum(counts.values())
        if total == 0:
            return False

        pending = counts.get("PENDING", 0)
        downloading = counts.get("DOWNLOADING", 0)
        return pending == 0 and downloading == 0


# =============================================================================
# PDF Repository
# =============================================================================


class PdfRepository:
    """Repository for PDF operations."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        project_id: str,
        original_url: Optional[str] = None,
        original_filename: Optional[str] = None,
        redirect_url: Optional[str] = None,
    ) -> Pdf:
        """Create a new PDF record."""
        pdf = Pdf(
            id=str(uuid4()),
            project_id=project_id,
            original_url=original_url,
            original_filename=original_filename,
            redirect_url=redirect_url,
            status=GcsUploadStatus.PENDING,
        )
        self.session.add(pdf)
        self.session.flush()
        return pdf

    def get(self, pdf_id: str) -> Optional[Pdf]:
        """Get a PDF by ID."""
        return self.session.get(Pdf, pdf_id)

    def get_by_project(self, project_id: str) -> Optional[Pdf]:
        """Get a PDF by project ID."""
        stmt = select(Pdf).where(Pdf.project_id == project_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def mark_uploaded(
        self,
        pdf_id: str,
        gcs_bucket: str,
        gcs_path: str,
        file_size_bytes: int,
        content_hash: str,
    ) -> None:
        """Mark a PDF as uploaded to GCS."""
        stmt = (
            update(Pdf)
            .where(Pdf.id == pdf_id)
            .values(
                gcs_bucket=gcs_bucket,
                gcs_path=gcs_path,
                file_size_bytes=file_size_bytes,
                content_hash=content_hash,
                status=GcsUploadStatus.UPLOADED,
                uploaded_at=datetime.utcnow(),
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_failed(self, pdf_id: str, error: str) -> None:
        """Mark a PDF upload as failed."""
        stmt = (
            update(Pdf)
            .where(Pdf.id == pdf_id)
            .values(status=GcsUploadStatus.FAILED, upload_error=error)
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def get_by_hash(self, content_hash: str) -> Optional[Pdf]:
        """Get a PDF by content hash (for deduplication)."""
        stmt = select(Pdf).where(Pdf.content_hash == content_hash).limit(1)
        return self.session.execute(stmt).scalar_one_or_none()
