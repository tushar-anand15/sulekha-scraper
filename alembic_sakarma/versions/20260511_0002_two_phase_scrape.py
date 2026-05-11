"""Two-phase scrape: lb_progress.manifest_completed_at + manifest_done stage.

Adds a nullable timestamp column to track when Phase 1 (manifest discovery)
finished for each LB, plus extends the lb_progress stage enum with a
``manifest_done`` value. Both ALTERs are non-blocking on Postgres and safe
to run against the live ``sakarma`` schema while workers are connected.

Revision ID: 20260511_0002
Revises: 20260509_0001
Create Date: 2026-05-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511_0002"
down_revision: Union[str, None] = "20260509_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add the nullable timestamp column to lb_progress. NULL on all
    #    existing rows is the desired default — they haven't been through
    #    the new manifest-only flow.
    op.add_column(
        "lb_progress",
        sa.Column(
            "manifest_completed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema="sakarma",
    )

    # 2. Extend the stage enum with manifest_done. ALTER TYPE ... ADD VALUE
    #    must run outside a transaction on older Postgres versions, and is
    #    non-blocking on Postgres 12+. Alembic's autocommit-isolation guard
    #    handles both cases.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE sakarma.sakarma_lb_progress_stage "
            "ADD VALUE IF NOT EXISTS 'manifest_done'"
        )


def downgrade() -> None:
    # Drop the column first — straightforward.
    op.drop_column(
        "lb_progress", "manifest_completed_at", schema="sakarma"
    )

    # Removing an enum value requires creating a new type without the
    # value, switching column types, and dropping the old enum. Worth the
    # complexity only because keeping a defunct value is unclean.
    op.execute(
        "CREATE TYPE sakarma.sakarma_lb_progress_stage_new AS ENUM "
        "('discovery', 'manifest', 'artifacts', 'reconcile')"
    )
    op.execute(
        "ALTER TABLE sakarma.lb_progress "
        "ALTER COLUMN current_stage TYPE sakarma.sakarma_lb_progress_stage_new "
        "USING current_stage::text::sakarma.sakarma_lb_progress_stage_new"
    )
    op.execute("DROP TYPE sakarma.sakarma_lb_progress_stage")
    op.execute(
        "ALTER TYPE sakarma.sakarma_lb_progress_stage_new "
        "RENAME TO sakarma_lb_progress_stage"
    )
