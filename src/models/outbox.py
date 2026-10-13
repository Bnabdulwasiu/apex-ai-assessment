"""Outbox event ORM model.

Implements the transactional outbox pattern from
docs/architecture.md §3.3 and docs/api-contract.md §17 / §20.

The outbox event is created in the *same* database transaction as the
referral record.  The outbox publisher polls for PENDING events and
publishes them to the event queue.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    PROCESSING = "PROCESSING"
    CONSUMED = "CONSUMED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OutboxEvent(Base):
    """Durable outbox event — survives queue unavailability.

    The ``payload`` column holds a minimal JSON string containing
    identifiers and processing metadata.  It must **not** contain
    patient-level clinical data (architecture §12).
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    referral_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_outbox_pending", "status", "created_at"),
        Index("ix_outbox_claim_lease", "status", "claimed_at"),
    )
