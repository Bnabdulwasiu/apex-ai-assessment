"""Referral ORM model.

Maps to the ``referrals`` table defined in docs/architecture.md §3.2.

Status values follow docs/api-contract.md §6.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class ReferralStatus(str, enum.Enum):
    """Externally visible referral statuses (api-contract §6)."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    ELIGIBILITY_FAILED = "ELIGIBILITY_FAILED"
    ROUTED = "ROUTED"
    CRM_PENDING = "CRM_PENDING"
    COMPLETED = "COMPLETED"
    DEAD_LETTER = "DEAD_LETTER"


class ProcessingStep(str, enum.Enum):
    """Internal processing stage — used for step-level retry/resume."""

    INTAKE = "INTAKE"
    EHR_ELIGIBILITY = "EHR_ELIGIBILITY"
    AI_CLASSIFICATION = "AI_CLASSIFICATION"
    ROUTING = "ROUTING"
    CRM = "CRM"
    COMPLETE = "COMPLETE"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Referral(Base):
    """Persistent referral record.

    Columns that hold patient-like data (``patient_id``, ``reason``)
    are classified as *sensitive* in api-contract §23.  They are stored
    here for workflow purposes but must **never** appear in application
    logs, queue events, or error responses.
    """

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # --- Business identifiers ---
    referral_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    external_reference: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )

    # --- Sensitive patient fields ---
    patient_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Operational fields ---
    specialty: Mapped[str] = mapped_column(String(64), nullable=False)
    preferred_hospital: Mapped[str] = mapped_column(String(64), nullable=False)
    hospital: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Processing state ---
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus, name="referral_status"),
        nullable=False,
        default=ReferralStatus.RECEIVED,
    )
    current_step: Mapped[ProcessingStep] = mapped_column(
        Enum(ProcessingStep, name="processing_step"),
        nullable=False,
        default=ProcessingStep.INTAKE,
    )

    # --- Integration results (populated during workflow) ---
    eligibility_reference: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    ai_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_urgency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    crm_case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Failure tracking ---
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failed_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)

    # --- Ownership / correlation ---
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_referrals_client_hospital", "client_id", "preferred_hospital"),
        Index("ix_referrals_status", "status"),
    )
