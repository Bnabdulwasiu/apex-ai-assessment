"""add leased outbox processing state

Revision ID: m5_outbox_leases
Revises: 078427a3490d
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "m5_outbox_leases"
down_revision: Union[str, None] = "078427a3490d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'PROCESSING'")
    op.add_column(
        "outbox_events",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_claim_lease",
        "outbox_events",
        ["status", "claimed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_claim_lease", table_name="outbox_events")
    op.drop_column("outbox_events", "consumed_at")
    op.drop_column("outbox_events", "claimed_by")
    op.drop_column("outbox_events", "claimed_at")
    # PostgreSQL enum values cannot be removed without rebuilding the type.
