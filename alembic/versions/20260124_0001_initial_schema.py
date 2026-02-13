"""Initial schema for Sulekha Data Extraction Service.

Revision ID: 0001
Revises: 
Create Date: 2026-01-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum types
    op.execute(
        "CREATE TYPE scraperuns_status AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')"
    )
    op.execute(
        "CREATE TYPE district_status AS ENUM ('PENDING', 'IN_PROGRESS', 'DONE', 'ERROR')"
    )
    op.execute(
        "CREATE TYPE localbody_status AS ENUM ('PENDING', 'IN_PROGRESS', 'PARTIAL', 'DONE', 'ERROR')"
    )
    op.execute(
        "CREATE TYPE pdf_status AS ENUM ('PENDING', 'DOWNLOADING', 'DOWNLOADED', 'MISSING', 'ERROR')"
    )
    op.execute(
        "CREATE TYPE gcs_upload_status AS ENUM ('PENDING', 'UPLOADING', 'UPLOADED', 'FAILED')"
    )

    # Create scrape_runs table
    op.create_table(
        "scrape_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("current_phase", sa.Integer(), nullable=False, default=1),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                name="scraperuns_status",
            ),
            nullable=False,
            default="PENDING",
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Create districts table
    op.create_table(
        "districts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "scrape_run_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("scrape_runs.id"),
            nullable=True,
        ),
        sa.Column("year_val", sa.Integer(), nullable=False),
        sa.Column("year_label", sa.String(50), nullable=False),
        sa.Column("lb_type_val", sa.Integer(), nullable=False),
        sa.Column("lb_type_label", sa.String(100), nullable=False),
        sa.Column("district_index", sa.Integer(), nullable=False),
        sa.Column("district_name", sa.String(200), nullable=False),
        sa.Column("num_local_bodies", sa.Integer(), nullable=True),
        sa.Column("num_projects", sa.Integer(), nullable=True),
        sa.Column("postback_target", sa.String(100), nullable=False, default="gvState"),
        sa.Column("postback_argument", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "IN_PROGRESS", "DONE", "ERROR", name="district_status"),
            nullable=False,
            default="PENDING",
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, default=0),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_processed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("year_val", "lb_type_val", "district_index", name="uq_district_identity"),
    )
    op.create_index("idx_district_status", "districts", ["status"])
    op.create_index("idx_district_year_lb", "districts", ["year_val", "lb_type_val"])

    # Create local_bodies table
    op.create_table(
        "local_bodies",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "district_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("districts.id"),
            nullable=False,
        ),
        sa.Column("lb_index", sa.Integer(), nullable=False),
        sa.Column("lb_name", sa.String(300), nullable=False),
        sa.Column("expected_projects", sa.Integer(), nullable=True),
        sa.Column("scraped_projects", sa.Integer(), nullable=False, default=0),
        sa.Column("last_page_scraped", sa.Integer(), nullable=False, default=0),
        sa.Column("total_pages", sa.Integer(), nullable=True),
        sa.Column("postback_target", sa.String(100), nullable=False, default="gvStat"),
        sa.Column("postback_argument", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "IN_PROGRESS", "PARTIAL", "DONE", "ERROR", name="localbody_status"),
            nullable=False,
            default="PENDING",
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, default=0),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_scraped_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("district_id", "lb_index", name="uq_local_body_identity"),
    )
    op.create_index("idx_lb_status", "local_bodies", ["status"])
    op.create_index("idx_lb_district", "local_bodies", ["district_id"])

    # Create projects table
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "local_body_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("local_bodies.id"),
            nullable=False,
        ),
        sa.Column("project_no", sa.String(50), nullable=False),
        sa.Column("project_name", sa.Text(), nullable=False),
        sa.Column("formulation", sa.String(100), nullable=True),
        sa.Column("expense", sa.String(100), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("select_argument", sa.String(100), nullable=False),
        sa.Column(
            "pdf_status",
            sa.Enum("PENDING", "DOWNLOADING", "DOWNLOADED", "MISSING", "ERROR", name="pdf_status"),
            nullable=False,
            default="PENDING",
        ),
        sa.Column("pdf_retry_count", sa.Integer(), nullable=False, default=0),
        sa.Column("pdf_error_message", sa.Text(), nullable=True),
        sa.Column("pdf_last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("local_body_id", "project_no", name="uq_project_identity"),
    )
    op.create_index("idx_project_pdf_status", "projects", ["pdf_status"])
    op.create_index("idx_project_lb", "projects", ["local_body_id"])

    # Create pdfs table
    op.create_table(
        "pdfs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("projects.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("gcs_path", sa.String(500), nullable=True),
        sa.Column("gcs_bucket", sa.String(200), nullable=True),
        sa.Column("original_url", sa.String(500), nullable=True),
        sa.Column("original_filename", sa.String(300), nullable=True),
        sa.Column("redirect_url", sa.String(500), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "UPLOADING", "UPLOADED", "FAILED", name="gcs_upload_status"),
            nullable=False,
            default="PENDING",
        ),
        sa.Column("upload_error", sa.Text(), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_pdf_status", "pdfs", ["status"])
    op.create_index("idx_pdf_hash", "pdfs", ["content_hash"])


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_table("pdfs")
    op.drop_table("projects")
    op.drop_table("local_bodies")
    op.drop_table("districts")
    op.drop_table("scrape_runs")

    # Drop enum types
    op.execute("DROP TYPE gcs_upload_status")
    op.execute("DROP TYPE pdf_status")
    op.execute("DROP TYPE localbody_status")
    op.execute("DROP TYPE district_status")
    op.execute("DROP TYPE scraperuns_status")
