"""Initial SAKARMA schema — creates the sakarma Postgres schema and all tables.

Revision ID: 20260509_0001
Revises:
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260509_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 0. Ensure the sakarma schema exists
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS sakarma")

    # ------------------------------------------------------------------
    # 1. Create Postgres ENUM types (schema-bound)
    # ------------------------------------------------------------------
    scrape_run_kind = postgresql.ENUM(
        "backfill",
        "diff",
        name="sakarma_scrape_run_kind",
        schema="sakarma",
        create_type=True,
    )
    scrape_run_kind.create(op.get_bind(), checkfirst=True)

    scrape_run_status = postgresql.ENUM(
        "running",
        "done",
        "failed",
        name="sakarma_scrape_run_status",
        schema="sakarma",
        create_type=True,
    )
    scrape_run_status.create(op.get_bind(), checkfirst=True)

    lb_progress_status = postgresql.ENUM(
        "pending",
        "in_progress",
        "done",
        "error",
        name="sakarma_lb_progress_status",
        schema="sakarma",
        create_type=True,
    )
    lb_progress_status.create(op.get_bind(), checkfirst=True)

    lb_progress_stage = postgresql.ENUM(
        "discovery",
        "manifest",
        "artifacts",
        "reconcile",
        name="sakarma_lb_progress_stage",
        schema="sakarma",
        create_type=True,
    )
    lb_progress_stage.create(op.get_bind(), checkfirst=True)

    artifact_type = postgresql.ENUM(
        "minutes_html",
        "dr_html",
        "attachment_pdf",
        name="sakarma_artifact_type",
        schema="sakarma",
        create_type=True,
    )
    artifact_type.create(op.get_bind(), checkfirst=True)

    recon_status = postgresql.ENUM(
        "matched",
        "mismatch",
        "missing_kpi",
        "missing_manifest",
        name="sakarma_recon_status",
        schema="sakarma",
        create_type=True,
    )
    recon_status.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # 2. Dimension tables
    # ------------------------------------------------------------------

    # sakarma.district
    op.create_table(
        "district",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("name_ml", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=True),
        schema="sakarma",
    )

    # sakarma.lb_type
    op.create_table(
        "lb_type",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("name_ml", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=True),
        schema="sakarma",
    )

    # sakarma.year
    op.create_table(
        "year",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("year_int", sa.Integer(), nullable=False, unique=True),
        schema="sakarma",
    )

    # ------------------------------------------------------------------
    # 3. Operational tables (scrape_run first — LB has a FK to it)
    # ------------------------------------------------------------------

    # sakarma.scrape_run
    op.create_table(
        "scrape_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "kind",
            postgresql.ENUM(name="sakarma_scrape_run_kind", schema="sakarma", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="sakarma_scrape_run_status", schema="sakarma", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_summary", postgresql.JSONB(), nullable=True),
        schema="sakarma",
    )

    # sakarma.lb  (depends on district, lb_type, scrape_run)
    op.create_table(
        "lb",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column(
            "district_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.district.id"),
            nullable=False,
        ),
        sa.Column(
            "lb_type_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.lb_type.id"),
            nullable=False,
        ),
        sa.Column("name_ml", sa.Text(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "scrape_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.scrape_run.id"),
            nullable=True,
        ),
        schema="sakarma",
    )
    op.create_index("idx_lb_district", "lb", ["district_id"], schema="sakarma")
    op.create_index("idx_lb_lb_type", "lb", ["lb_type_id"], schema="sakarma")

    # sakarma.main_group_value  (depends on lb)
    op.create_table(
        "main_group_value",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "lb_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.lb.id"),
            nullable=False,
        ),
        sa.Column("ddl_value", sa.Integer(), nullable=False),
        sa.Column("name_ml", sa.Text(), nullable=False),
        sa.UniqueConstraint("lb_id", "ddl_value", name="uq_main_group_value_lb_ddl"),
        schema="sakarma",
    )
    op.create_index(
        "idx_main_group_value_lb", "main_group_value", ["lb_id"], schema="sakarma"
    )

    # sakarma.lb_progress  (depends on scrape_run, lb)
    op.create_table(
        "lb_progress",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "scrape_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.scrape_run.id"),
            nullable=False,
        ),
        sa.Column(
            "lb_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.lb.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                name="sakarma_lb_progress_status", schema="sakarma", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "current_stage",
            postgresql.ENUM(
                name="sakarma_lb_progress_stage", schema="sakarma", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("scrape_run_id", "lb_id", name="uq_lb_progress_run_lb"),
        schema="sakarma",
    )
    op.create_index(
        "idx_lb_progress_run_status",
        "lb_progress",
        ["scrape_run_id", "status"],
        schema="sakarma",
    )

    # ------------------------------------------------------------------
    # 4. Universe tables
    # ------------------------------------------------------------------

    # sakarma.dashboard_kpi_snapshot  (depends on lb, year, main_group_value, scrape_run)
    op.create_table(
        "dashboard_kpi_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "lb_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.lb.id"),
            nullable=False,
        ),
        sa.Column(
            "year_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.year.id"),
            nullable=False,
        ),
        sa.Column(
            "main_group_value_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.main_group_value.id"),
            nullable=False,
        ),
        sa.Column("total_meetings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ongoing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minutes_complete", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minutes_incomplete", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_html_hash", sa.CHAR(64), nullable=True),
        sa.Column("snapshot_html_gcs_path", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "scrape_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.scrape_run.id"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "lb_id",
            "year_id",
            "main_group_value_id",
            "scrape_run_id",
            name="uq_kpi_snapshot_natural",
        ),
        schema="sakarma",
    )
    op.create_index(
        "idx_kpi_snapshot_lb_year_group",
        "dashboard_kpi_snapshot",
        ["lb_id", "year_id", "main_group_value_id"],
        schema="sakarma",
    )

    # sakarma.meeting_manifest  (depends on lb, year, main_group_value, scrape_run)
    op.create_table(
        "meeting_manifest",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "lb_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.lb.id"),
            nullable=False,
        ),
        sa.Column(
            "year_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.year.id"),
            nullable=False,
        ),
        sa.Column(
            "main_group_value_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.main_group_value.id"),
            nullable=False,
        ),
        sa.Column("category", sa.SmallInteger(), nullable=False),
        sa.Column("dashboard_grid_select_index", sa.Integer(), nullable=True),
        sa.Column("dr_postback_target", sa.Text(), nullable=True),
        sa.Column("meeting_no_label", sa.Text(), nullable=False),
        sa.Column("meeting_date", sa.Date(), nullable=False),
        sa.Column("meeting_type", sa.Text(), nullable=True),
        sa.Column("meeting_nature", sa.Text(), nullable=True),
        sa.Column("meeting_venue", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "scrape_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.scrape_run.id"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "lb_id",
            "year_id",
            "main_group_value_id",
            "category",
            "meeting_no_label",
            "meeting_date",
            name="uq_meeting_manifest_natural",
        ),
        schema="sakarma",
    )
    op.create_index(
        "idx_meeting_manifest_lb_year_group_cat",
        "meeting_manifest",
        ["lb_id", "year_id", "main_group_value_id", "category"],
        schema="sakarma",
    )
    op.create_index(
        "idx_meeting_manifest_run",
        "meeting_manifest",
        ["scrape_run_id"],
        schema="sakarma",
    )

    # ------------------------------------------------------------------
    # 5. Artifact table  (depends on meeting_manifest, scrape_run)
    # ------------------------------------------------------------------

    # sakarma.meeting_artifact
    op.create_table(
        "meeting_artifact",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "meeting_manifest_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.meeting_manifest.id"),
            nullable=False,
        ),
        sa.Column(
            "artifact_type",
            postgresql.ENUM(name="sakarma_artifact_type", schema="sakarma", create_type=False),
            nullable=False,
        ),
        sa.Column("decision_index", sa.SmallInteger(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.CHAR(64), nullable=False, unique=True),
        sa.Column("gcs_path", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("source_page_url", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "scrape_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.scrape_run.id"),
            nullable=False,
        ),
        schema="sakarma",
    )
    op.create_index(
        "idx_meeting_artifact_manifest_type",
        "meeting_artifact",
        ["meeting_manifest_id", "artifact_type"],
        schema="sakarma",
    )

    # ------------------------------------------------------------------
    # 6. Reconciliation table  (depends on scrape_run, lb, year, main_group_value)
    # ------------------------------------------------------------------

    # sakarma.reconciliation
    op.create_table(
        "reconciliation",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "scrape_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.scrape_run.id"),
            nullable=False,
        ),
        sa.Column(
            "lb_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.lb.id"),
            nullable=False,
        ),
        sa.Column(
            "year_id",
            sa.Integer(),
            sa.ForeignKey("sakarma.year.id"),
            nullable=False,
        ),
        sa.Column(
            "main_group_value_id",
            sa.BigInteger(),
            sa.ForeignKey("sakarma.main_group_value.id"),
            nullable=False,
        ),
        sa.Column("category", sa.SmallInteger(), nullable=False),
        sa.Column("dashboard_kpi_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delta", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            postgresql.ENUM(name="sakarma_recon_status", schema="sakarma", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "scrape_run_id",
            "lb_id",
            "year_id",
            "main_group_value_id",
            "category",
            name="uq_reconciliation_natural",
        ),
        schema="sakarma",
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # Drop tables in reverse dependency order
    # ------------------------------------------------------------------
    op.drop_table("reconciliation", schema="sakarma")
    op.drop_table("meeting_artifact", schema="sakarma")
    op.drop_table("meeting_manifest", schema="sakarma")
    op.drop_table("dashboard_kpi_snapshot", schema="sakarma")
    op.drop_table("lb_progress", schema="sakarma")
    op.drop_table("main_group_value", schema="sakarma")
    op.drop_table("lb", schema="sakarma")
    op.drop_table("scrape_run", schema="sakarma")
    op.drop_table("year", schema="sakarma")
    op.drop_table("lb_type", schema="sakarma")
    op.drop_table("district", schema="sakarma")

    # ------------------------------------------------------------------
    # Drop ENUM types
    # ------------------------------------------------------------------
    postgresql.ENUM(name="sakarma_recon_status", schema="sakarma").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(name="sakarma_artifact_type", schema="sakarma").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(name="sakarma_lb_progress_stage", schema="sakarma").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(name="sakarma_lb_progress_status", schema="sakarma").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(name="sakarma_scrape_run_status", schema="sakarma").drop(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(name="sakarma_scrape_run_kind", schema="sakarma").drop(
        op.get_bind(), checkfirst=True
    )

    # ------------------------------------------------------------------
    # Drop the schema itself
    # ------------------------------------------------------------------
    op.execute("DROP SCHEMA IF EXISTS sakarma CASCADE")
