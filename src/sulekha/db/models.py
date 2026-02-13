"""SQLAlchemy models for Sulekha Data Extraction Service.

This module defines the database schema for tracking:
- Scrape runs (pipeline executions)
- Districts (Year x LB Type x District combinations)
- Local Bodies within districts
- Projects within local bodies
- PDFs for each project
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


# =============================================================================
# Enums for Status Tracking
# =============================================================================


class ScrapeRunStatus(str, enum.Enum):
    """Status of a scrape run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DistrictStatus(str, enum.Enum):
    """Status of district discovery (Phase 2: discovering local bodies)."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    ERROR = "ERROR"


class LocalBodyStatus(str, enum.Enum):
    """Status of local body scraping (Phase 3: scraping projects)."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PARTIAL = "PARTIAL"  # Some pages scraped, more to go
    DONE = "DONE"
    ERROR = "ERROR"


class PdfStatus(str, enum.Enum):
    """Status of PDF download for a project."""

    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    MISSING = "MISSING"  # No PDF available for this project
    ERROR = "ERROR"


class GcsUploadStatus(str, enum.Enum):
    """Status of GCS upload for a PDF."""

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    FAILED = "FAILED"


# =============================================================================
# Models
# =============================================================================


class ScrapeRun(Base):
    """A single execution of the scraping pipeline.

    Tracks the overall progress of a scrape run across all phases.
    """

    __tablename__ = "scrape_runs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    current_phase: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[ScrapeRunStatus] = mapped_column(
        Enum(ScrapeRunStatus), default=ScrapeRunStatus.PENDING
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    districts: Mapped[list["District"]] = relationship(back_populates="scrape_run")


class District(Base):
    """A district entry for a specific (Year, LB Type, District) combination.

    Represents one row in the gvState table from the Sulekha portal.
    Phase 1 creates these records, Phase 2 discovers local bodies for each.
    """

    __tablename__ = "districts"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )

    # Foreign key to scrape run (optional, for tracking which run discovered this)
    scrape_run_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("scrape_runs.id"), nullable=True
    )

    # Hierarchical identifiers
    year_val: Mapped[int] = mapped_column(Integer, nullable=False)
    year_label: Mapped[str] = mapped_column(String(50), nullable=False)
    lb_type_val: Mapped[int] = mapped_column(Integer, nullable=False)
    lb_type_label: Mapped[str] = mapped_column(String(100), nullable=False)
    district_index: Mapped[int] = mapped_column(Integer, nullable=False)
    district_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Metadata from gvState table
    num_local_bodies: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    num_projects: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Postback info for navigation
    postback_target: Mapped[str] = mapped_column(String(100), default="gvState")
    postback_argument: Mapped[str] = mapped_column(String(100), nullable=False)

    # Status tracking for Phase 2
    status: Mapped[DistrictStatus] = mapped_column(
        Enum(DistrictStatus), default=DistrictStatus.PENDING
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    scrape_run: Mapped[Optional["ScrapeRun"]] = relationship(back_populates="districts")
    local_bodies: Mapped[list["LocalBody"]] = relationship(back_populates="district")

    __table_args__ = (
        UniqueConstraint(
            "year_val", "lb_type_val", "district_index", name="uq_district_identity"
        ),
        Index("idx_district_status", "status"),
        Index("idx_district_year_lb", "year_val", "lb_type_val"),
    )


class LocalBody(Base):
    """A local body within a district.

    Represents one row in the gvStat table from the Sulekha portal.
    Phase 2 creates these records, Phase 3 scrapes projects for each.
    """

    __tablename__ = "local_bodies"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )

    # Foreign key to district
    district_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("districts.id"), nullable=False
    )

    # Local body identifiers
    lb_index: Mapped[int] = mapped_column(Integer, nullable=False)
    lb_name: Mapped[str] = mapped_column(String(300), nullable=False)

    # Project counts
    expected_projects: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scraped_projects: Mapped[int] = mapped_column(Integer, default=0)

    # Pagination progress for resumption
    last_page_scraped: Mapped[int] = mapped_column(Integer, default=0)
    total_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Postback info for navigation
    postback_target: Mapped[str] = mapped_column(String(100), default="gvStat")
    postback_argument: Mapped[str] = mapped_column(String(100), nullable=False)

    # Status tracking for Phase 3
    status: Mapped[LocalBodyStatus] = mapped_column(
        Enum(LocalBodyStatus), default=LocalBodyStatus.PENDING
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    district: Mapped["District"] = relationship(back_populates="local_bodies")
    projects: Mapped[list["Project"]] = relationship(back_populates="local_body")

    __table_args__ = (
        UniqueConstraint("district_id", "lb_index", name="uq_local_body_identity"),
        Index("idx_lb_status", "status"),
        Index("idx_lb_district", "district_id"),
    )


class Project(Base):
    """A project within a local body.

    Represents one row in the gvProjects table from the Sulekha portal.
    Phase 3 creates these records, Phase 4 downloads PDFs for each.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )

    # Foreign key to local body
    local_body_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("local_bodies.id"), nullable=False
    )

    # Project identifiers
    project_no: Mapped[str] = mapped_column(String(50), nullable=False)
    project_name: Mapped[str] = mapped_column(Text, nullable=False)

    # Financial data from table
    formulation: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    expense: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Page number where this project was found (for debugging)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Postback info for PDF download
    select_argument: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # e.g., "Select$0"

    # PDF download status (Phase 4)
    pdf_status: Mapped[PdfStatus] = mapped_column(Enum(PdfStatus), default=PdfStatus.PENDING)
    pdf_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    pdf_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Timestamps
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    local_body: Mapped["LocalBody"] = relationship(back_populates="projects")
    pdf: Mapped[Optional["Pdf"]] = relationship(back_populates="project", uselist=False)

    __table_args__ = (
        UniqueConstraint("local_body_id", "project_no", name="uq_project_identity"),
        Index("idx_project_pdf_status", "pdf_status"),
        Index("idx_project_lb", "local_body_id"),
    )


class Pdf(Base):
    """A downloaded PDF file for a project.

    Stores metadata about the PDF and its location in GCS.
    """

    __tablename__ = "pdfs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )

    # Foreign key to project (one-to-one)
    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("projects.id"), nullable=False, unique=True
    )

    # GCS storage info
    gcs_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    gcs_bucket: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Original download info
    original_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    redirect_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # File metadata
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # SHA256

    # Upload status
    status: Mapped[GcsUploadStatus] = mapped_column(
        Enum(GcsUploadStatus), default=GcsUploadStatus.PENDING
    )
    upload_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    uploaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="pdf")

    __table_args__ = (Index("idx_pdf_status", "status"), Index("idx_pdf_hash", "content_hash"))
