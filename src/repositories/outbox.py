"""Outbox repository.

Manages the transactional outbox events table.
The outbox publisher polls for PENDING events and marks them
PUBLISHED after successful queue delivery (architecture §3.3).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.outbox import OutboxEvent, OutboxStatus


class OutboxRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, event: OutboxEvent) -> OutboxEvent:
        """Insert a new outbox event (within the caller's transaction)."""
        self._session.add(event)
        await self._session.flush()
        return event

    async def get_pending(self, limit: int = 50) -> list[OutboxEvent]:
        """Fetch the oldest pending events for the publisher to process."""
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_published(self, event_id: str) -> None:
        """Mark an event as successfully published to the queue."""
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.event_id == event_id)
            .values(
                status=OutboxStatus.PUBLISHED,
                published_at=datetime.now(timezone.utc),
            )
        )
        await self._session.execute(stmt)

    async def increment_attempt(self, event_id: str) -> None:
        """Bump the attempt counter after a failed publish attempt."""
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.event_id == event_id)
            .values(attempt_count=OutboxEvent.attempt_count + 1)
        )
        await self._session.execute(stmt)

    async def claim_next_published(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> OutboxEvent | None:
        """Lease the oldest published or abandoned processing event.

        This is the cross-process "queue consume" operation. The
        ``outbox_events`` table doubles as the durable transport: the
        publisher marks a row PUBLISHED (equivalent to "sent to the
        broker"); this method is the worker's "receive from the broker".

        A claim moves the event to ``PROCESSING``. It is marked
        ``CONSUMED`` only after the workflow transaction succeeds. If a
        worker crashes, another worker may reclaim the event after the
        lease expires.

        Returns
        -------
        OutboxEvent | None
            The claimed event (now marked PROCESSING), or ``None`` if
            nothing is available or this process lost the race.
        """
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=lease_seconds)
        claimable = or_(
            OutboxEvent.status == OutboxStatus.PUBLISHED,
            and_(
                OutboxEvent.status == OutboxStatus.PROCESSING,
                or_(
                    OutboxEvent.claimed_at.is_(None),
                    OutboxEvent.claimed_at <= stale_before,
                ),
            ),
        )
        stmt = (
            select(OutboxEvent)
            .where(claimable)
            .order_by(OutboxEvent.created_at.asc())
            .limit(1)
        )
        if self._session.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        result = await self._session.execute(stmt)
        candidate = result.scalar_one_or_none()
        if candidate is None:
            return None

        claim_stmt = (
            update(OutboxEvent)
            .where(
                OutboxEvent.id == candidate.id,
                claimable,
            )
            .values(
                status=OutboxStatus.PROCESSING,
                claimed_at=now,
                claimed_by=worker_id,
            )
        )
        claim_result = await self._session.execute(claim_stmt)
        if claim_result.rowcount != 1:
            return None

        await self._session.refresh(candidate)
        return candidate

    async def mark_consumed(self, event_id: str, *, worker_id: str) -> bool:
        """Complete an event only when it is still leased by this worker."""
        stmt = (
            update(OutboxEvent)
            .where(
                OutboxEvent.event_id == event_id,
                OutboxEvent.status == OutboxStatus.PROCESSING,
                OutboxEvent.claimed_by == worker_id,
            )
            .values(
                status=OutboxStatus.CONSUMED,
                consumed_at=datetime.now(timezone.utc),
            )
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1
