"""SQLAlchemy models for the SAKARMA scraper.

All tables live in the dedicated ``sakarma`` Postgres schema. ``SakarmaBase``
is intentionally distinct from sulekha's ``Base`` so the two metadatas never
collide even when they share a single Postgres database.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    CHAR,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# =============================================================================
# Schema-bound declarative base
# =============================================================================

SAKARMA_SCHEMA = "sakarma"


class SakarmaBase(DeclarativeBase):
    """Declarative base for all SAKARMA tables (schema='sakarma')."""

    metadata = MetaData(schema=SAKARMA_SCHEMA)


# =============================================================================
# Postgres ENUM types — explicitly schema-bound and named
# =============================================================================

scrape_run_kind_enum = ENUM(
    "backfill",
    "diff",
    name="sakarma_scrape_run_kind",
    schema=SAKARMA_SCHEMA,
    create_type=True,
)

scrape_run_status_enum = ENUM(
    "running",
    "done",
    "failed",
    name="sakarma_scrape_run_status",
    schema=SAKARMA_SCHEMA,
    create_type=True,
)

lb_progress_status_enum = ENUM(
    "pending",
    "in_progress",
    "done",
    "error",
    name="sakarma_lb_progress_status",
    schema=SAKARMA_SCHEMA,
    create_type=True,
)

lb_progress_stage_enum = ENUM(
    "discovery",
    "manifest",
    "artifacts",
    "reconcile",
    name="sakarma_lb_progress_stage",
    schema=SAKARMA_SCHEMA,
    create_type=True,
)

artifact_type_enum = ENUM(
    "minutes_html",
    "dr_html",
    "attachment_pdf",
    name="sakarma_artifact_type",
    schema=SAKARMA_SCHEMA,
    create_type=True,
)

recon_status_enum = ENUM(
    "matched",
    "mismatch",
    "missing_kpi",
    "missing_manifest",
    name="sakarma_recon_status",
    schema=SAKARMA_SCHEMA,
    create_type=True,
)

# =============================================================================
# Category constants (kept as SMALLINT for forward-compat)
# =============================================================================

CATEGORY_ONGOING = 1
CATEGORY_APPROVED = 2
CATEGORY_INCOMPLETE = 3
CATEGORY_CANCELLED = 4


# =============================================================================
# Dimensions
# =============================================================================


class District(SakarmaBase):
    """Kerala district (ddlDistrict option value)."""

    __tablename__ = "district"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name_ml: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LBType(SakarmaBase):
    """Local body type (ddlLBType option value)."""

    __tablename__ = "lb_type"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name_ml: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Year(SakarmaBase):
    """Year option (e.g. ddlYear value 27 -> 2016)."""

    __tablename__ = "year"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    year_int: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)


class LB(SakarmaBase):
    """Local body (ddlLBName option value)."""

    __tablename__ = "lb"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    district_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.district.id"), nullable=False
    )
    lb_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.lb_type.id"), nullable=False
    )
    name_ml: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    scrape_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey(f"{SAKARMA_SCHEMA}.scrape_run.id"), nullable=True
    )

    __table_args__ = (
        Index("idx_lb_district", "district_id"),
        Index("idx_lb_lb_type", "lb_type_id"),
    )


class MainGroupValue(SakarmaBase):
    """Main Group ddl values discovered per LB (varies by LB)."""

    __tablename__ = "main_group_value"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.lb.id"), nullable=False
    )
    ddl_value: Mapped[int] = mapped_column(Integer, nullable=False)
    name_ml: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("lb_id", "ddl_value", name="uq_main_group_value_lb_ddl"),
        Index("idx_main_group_value_lb", "lb_id"),
    )


# =============================================================================
# Operational
# =============================================================================


class ScrapeRun(SakarmaBase):
    """A single SAKARMA pipeline execution (backfill or diff)."""

    __tablename__ = "scrape_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(scrape_run_kind_enum, nullable=False)
    status: Mapped[str] = mapped_column(
        scrape_run_status_enum, nullable=False, default="running"
    )
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    error_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class LBProgress(SakarmaBase):
    """Per-LB scraping progress within a scrape run."""

    __tablename__ = "lb_progress"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scrape_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{SAKARMA_SCHEMA}.scrape_run.id"), nullable=False
    )
    lb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.lb.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        lb_progress_status_enum, nullable=False, default="pending"
    )
    current_stage: Mapped[Optional[str]] = mapped_column(
        lb_progress_stage_enum, nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("scrape_run_id", "lb_id", name="uq_lb_progress_run_lb"),
        Index("idx_lb_progress_run_status", "scrape_run_id", "status"),
    )


# =============================================================================
# Universe
# =============================================================================


class DashboardKPISnapshot(SakarmaBase):
    """KPI snapshot from the LBWise dashboard for a (LB, year, main_group)."""

    __tablename__ = "dashboard_kpi_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.lb.id"), nullable=False
    )
    year_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.year.id"), nullable=False
    )
    main_group_value_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{SAKARMA_SCHEMA}.main_group_value.id"),
        nullable=False,
    )
    total_meetings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ongoing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes_complete: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minutes_incomplete: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_html_hash: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    snapshot_html_gcs_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    scrape_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{SAKARMA_SCHEMA}.scrape_run.id"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "lb_id",
            "year_id",
            "main_group_value_id",
            "scrape_run_id",
            name="uq_kpi_snapshot_natural",
        ),
        Index(
            "idx_kpi_snapshot_lb_year_group",
            "lb_id",
            "year_id",
            "main_group_value_id",
        ),
    )


class MeetingManifest(SakarmaBase):
    """Manifest row identifying a single meeting in the LBWise dashboard grid."""

    __tablename__ = "meeting_manifest"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.lb.id"), nullable=False
    )
    year_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.year.id"), nullable=False
    )
    main_group_value_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{SAKARMA_SCHEMA}.main_group_value.id"),
        nullable=False,
    )
    category: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dashboard_grid_select_index: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    dr_postback_target: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meeting_no_label: Mapped[str] = mapped_column(Text, nullable=False)
    meeting_date: Mapped[date] = mapped_column(Date, nullable=False)
    meeting_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meeting_nature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meeting_venue: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    scrape_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{SAKARMA_SCHEMA}.scrape_run.id"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "lb_id",
            "year_id",
            "main_group_value_id",
            "category",
            "meeting_no_label",
            "meeting_date",
            name="uq_meeting_manifest_natural",
        ),
        Index(
            "idx_meeting_manifest_lb_year_group_cat",
            "lb_id",
            "year_id",
            "main_group_value_id",
            "category",
        ),
        Index("idx_meeting_manifest_run", "scrape_run_id"),
    )


# =============================================================================
# Artifacts
# =============================================================================


class MeetingArtifact(SakarmaBase):
    """A single artifact captured for a meeting (Minutes HTML, DR HTML, PDF)."""

    __tablename__ = "meeting_artifact"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_manifest_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{SAKARMA_SCHEMA}.meeting_manifest.id"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(artifact_type_enum, nullable=False)
    decision_index: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    gcs_path: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_page_url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    scrape_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{SAKARMA_SCHEMA}.scrape_run.id"), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_meeting_artifact_manifest_type",
            "meeting_manifest_id",
            "artifact_type",
        ),
    )


# =============================================================================
# Reconciliation
# =============================================================================


class Reconciliation(SakarmaBase):
    """Per (run, lb, year, main_group, category) KPI-vs-manifest reconciliation."""

    __tablename__ = "reconciliation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scrape_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{SAKARMA_SCHEMA}.scrape_run.id"), nullable=False
    )
    lb_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.lb.id"), nullable=False
    )
    year_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{SAKARMA_SCHEMA}.year.id"), nullable=False
    )
    main_group_value_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(f"{SAKARMA_SCHEMA}.main_group_value.id"),
        nullable=False,
    )
    category: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dashboard_kpi_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(recon_status_enum, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "scrape_run_id",
            "lb_id",
            "year_id",
            "main_group_value_id",
            "category",
            name="uq_reconciliation_natural",
        ),
    )


__all__ = [
    "SakarmaBase",
    "SAKARMA_SCHEMA",
    "CATEGORY_ONGOING",
    "CATEGORY_APPROVED",
    "CATEGORY_INCOMPLETE",
    "CATEGORY_CANCELLED",
    "District",
    "LBType",
    "Year",
    "LB",
    "MainGroupValue",
    "ScrapeRun",
    "LBProgress",
    "DashboardKPISnapshot",
    "MeetingManifest",
    "MeetingArtifact",
    "Reconciliation",
    "scrape_run_kind_enum",
    "scrape_run_status_enum",
    "lb_progress_status_enum",
    "lb_progress_stage_enum",
    "artifact_type_enum",
    "recon_status_enum",
]
