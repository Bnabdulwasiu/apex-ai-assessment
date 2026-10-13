"""ensure CONSUMED exists in the outbox_status enum

Revision ID: 078427a3490d
Revises: ba8f84093b53
Create Date: 2026-09-26 12:49:27.266457

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '078427a3490d'
down_revision: Union[str, None] = 'ba8f84093b53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The initial schema already contains CONSUMED. Keep this historical
    # revision safe for both fresh databases and older migration paths.
    op.execute("ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'CONSUMED'")


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type
    # directly. A real downgrade would require recreating the type
    # and remapping every dependent column — out of scope for this
    # prototype. Documented as a known limitation instead.
    pass
