"""add CONSUMED to outbox_status enum


Revision ID: ba8f84093b53
Revises: 0001_initial
Create Date: 2026-09-25 21:52:25.935210

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ba8f84093b53'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'CONSUMED'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type easily
    pass
