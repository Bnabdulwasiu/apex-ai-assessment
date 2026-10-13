"""Idempotency record ORM model.

Implements the idempotency contract from docs/api-contract.md §7
and docs/requirements.md FR-05.

Uniqueness is enforced at the database level on (client_id, idempotency_key)
to protect against concurrent duplicate submissions.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IdempotencyRecord(Base):
    """Tracks previously seen (client_id, idempotency_key) pairs.

    ``request_hash`` is a SHA-256 digest of the canonical request body.
    This allows the system to detect same-key-different-payload conflicts
    (api-contract §7 — 409 Conflict).
    """

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    referral_id: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_client_idempotency"),
    )
