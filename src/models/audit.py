"""Audit log ORM model.

Implements the audit trail from docs/api-contract.md §18
and docs/requirements.md FR-15.

Audit records capture *what happened* without becoming a copy of the
clinical payload.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    """Immutable audit record.

    Fields follow the audit event contract in api-contract §18::

        timestamp, actor, action, resource_type, resource_id,
        outcome, request_id
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Optional context — must not contain sensitive patient data.
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
