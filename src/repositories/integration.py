"""Integration events repository — records each integration step attempt."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.integration import IntegrationEvent, IntegrationOutcome, IntegrationStep


class IntegrationRepository:
    """Data access wrapper around the ``integration_events`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_attempt(
        self,
        *,
        referral_id: str,
        step: IntegrationStep,
        attempt: int,
        outcome: IntegrationOutcome,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        result_reference: Optional[str] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> IntegrationEvent:
        """Record an attempt for an integration step."""
        now = datetime.now(timezone.utc)
        event = IntegrationEvent(
            referral_id=referral_id,
            step=step,
            attempt=attempt,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            result_reference=result_reference,
            started_at=started_at or now,
            completed_at=completed_at or now,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def get_latest_successful_step(
        self,
        referral_id: str,
        step: IntegrationStep,
    ) -> Optional[IntegrationEvent]:
        """Check if this step has already completed successfully (for duplicate/redelivery protection)."""
        stmt = (
            select(IntegrationEvent)
            .where(
                IntegrationEvent.referral_id == referral_id,
                IntegrationEvent.step == step,
                IntegrationEvent.outcome == IntegrationOutcome.SUCCESS,
            )
            .order_by(desc(IntegrationEvent.completed_at))
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_events_for_referral(
        self,
        referral_id: str,
    ) -> list[IntegrationEvent]:
        """Retrieve all recorded integration events for a referral in chronological order."""
        stmt = (
            select(IntegrationEvent)
            .where(IntegrationEvent.referral_id == referral_id)
            .order_by(IntegrationEvent.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
