"""Integration event ORM model.

Records each external integration attempt (EHR, AI, CRM) for a referral.
Enables step-level retry/resume (architecture §6, §9) and auditability.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class IntegrationStep(str, enum.Enum):
    EHR_ELIGIBILITY = "EHR_ELIGIBILITY"
    AI_CLASSIFICATION = "AI_CLASSIFICATION"
    ROUTING = "ROUTING"
    CRM = "CRM"


class IntegrationOutcome(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_NON_RETRYABLE = "FAILED_NON_RETRYABLE"
    SKIPPED = "SKIPPED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationEvent(Base):
    """One row per integration attempt per step.

    Response payloads are **sanitized** — they record outcome codes and
    references, not full clinical content.
    """

    __tablename__ = "integration_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    referral_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step: Mapped[IntegrationStep] = mapped_column(
        Enum(IntegrationStep, name="integration_step"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    outcome: Mapped[IntegrationOutcome] = mapped_column(
        Enum(IntegrationOutcome, name="integration_outcome"), nullable=False
    )

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sanitized result references — no patient payloads.
    result_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_integration_referral_step", "referral_id", "step"),
    )
