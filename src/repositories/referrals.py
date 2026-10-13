"""Referral repository — data access for the referrals table.

All queries go through this layer so the service/API never
touches SQLAlchemy session operations directly.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.referral import ProcessingStep, Referral, ReferralStatus


class ReferralRepository:
    """Thin data-access wrapper around the ``referrals`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, referral: Referral) -> Referral:
        """Insert a new referral row (within the caller's transaction)."""
        self._session.add(referral)
        await self._session.flush()  # assigns PK, stays in same txn
        return referral

    async def get_by_referral_id(self, referral_id: str) -> Referral | None:
        """Look up a referral by its public business identifier."""
        stmt = select(Referral).where(Referral.referral_id == referral_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_external_reference(self, external_ref: str) -> Referral | None:
        stmt = select(Referral).where(Referral.external_reference == external_ref)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        referral_id: str,
        status: ReferralStatus,
        *,
        current_step: ProcessingStep | None = None,
        failure_code: str | None = None,
        failed_step: str | None = None,
        crm_case_id: str | None = None,
        hospital: str | None = None,
        eligibility_reference: str | None = None,
        ai_classification: str | None = None,
        ai_urgency: str | None = None,
        ai_confidence: float | None = None,
        routing_reason: str | None = None,
        retry_count: int | None = None,
    ) -> Referral | None:
        """Update referral processing state.

        Only supplied keyword arguments are written; ``None`` values
        for optional fields are **not** applied so that previous data
        is preserved.
        """
        referral = await self.get_by_referral_id(referral_id)
        if referral is None:
            return None

        referral.status = status
        referral.updated_at = datetime.now(timezone.utc)

        if current_step is not None:
            referral.current_step = current_step
        if failure_code is not None:
            referral.failure_code = failure_code
        if failed_step is not None:
            referral.failed_step = failed_step
        if crm_case_id is not None:
            referral.crm_case_id = crm_case_id
        if hospital is not None:
            referral.hospital = hospital
        if eligibility_reference is not None:
            referral.eligibility_reference = eligibility_reference
        if ai_classification is not None:
            referral.ai_classification = ai_classification
        if ai_urgency is not None:
            referral.ai_urgency = ai_urgency
        if ai_confidence is not None:
            referral.ai_confidence = ai_confidence
        if routing_reason is not None:
            referral.routing_reason = routing_reason
        if retry_count is not None:
            referral.retry_count = retry_count

        await self._session.flush()
        return referral

    async def prepare_for_replay(self, referral_id: str) -> Referral | None:
        """Clear terminal failure state while preserving completed step data."""
        referral = await self.get_by_referral_id(referral_id)
        if referral is None:
            return None

        referral.status = ReferralStatus.RECEIVED
        referral.failure_code = None
        referral.failed_step = None
        referral.retry_count = 0
        referral.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        return referral
