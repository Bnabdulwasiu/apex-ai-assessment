"""Reliability tests for outbox publication and leased consumption."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.mocks import InMemoryEventQueue
from src.models.outbox import OutboxEvent, OutboxStatus
from src.repositories.outbox import OutboxRepository
from src.services.outbox_publisher import OutboxPublisher


def make_event(event_id: str, status: OutboxStatus) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        event_type="REFERRAL_RECEIVED",
        referral_id=f"ref-{event_id}",
        correlation_id=f"request-{event_id}",
        payload="{}",
        status=status,
    )


class FailingEventQueue(InMemoryEventQueue):
    async def publish(self, event: dict) -> None:
        raise ConnectionError("queue unavailable")


@pytest.mark.asyncio
async def test_queue_failure_keeps_event_pending_until_recovery(
    session: AsyncSession,
):
    event = make_event("evt-publish-recovery", OutboxStatus.PENDING)
    session.add(event)
    await session.flush()

    failed_count = await OutboxPublisher(FailingEventQueue()).publish_pending(session)
    await session.refresh(event)
    assert failed_count == 0
    assert event.status == OutboxStatus.PENDING
    assert event.attempt_count == 1

    queue = InMemoryEventQueue()
    published_count = await OutboxPublisher(queue).publish_pending(session)
    await session.refresh(event)
    assert published_count == 1
    assert event.status == OutboxStatus.PUBLISHED
    assert event.published_at is not None
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_event_is_consumed_only_by_its_lease_owner(session: AsyncSession):
    event = make_event("evt-owned-claim", OutboxStatus.PUBLISHED)
    session.add(event)
    await session.flush()
    repo = OutboxRepository(session)

    claimed = await repo.claim_next_published(worker_id="worker-1", lease_seconds=60)
    assert claimed is not None
    assert claimed.status == OutboxStatus.PROCESSING
    assert claimed.claimed_by == "worker-1"
    assert claimed.consumed_at is None
    await session.commit()

    duplicate_claim = await repo.claim_next_published(
        worker_id="worker-2", lease_seconds=60
    )
    assert duplicate_claim is None
    assert await repo.mark_consumed(event.event_id, worker_id="worker-2") is False
    assert await repo.mark_consumed(event.event_id, worker_id="worker-1") is True
    await session.commit()
    await session.refresh(event)
    assert event.status == OutboxStatus.CONSUMED
    assert event.consumed_at is not None


@pytest.mark.asyncio
async def test_abandoned_processing_lease_is_reclaimed(session: AsyncSession):
    event = make_event("evt-stale-claim", OutboxStatus.PUBLISHED)
    session.add(event)
    await session.flush()
    repo = OutboxRepository(session)

    first_claim = await repo.claim_next_published(
        worker_id="crashed-worker", lease_seconds=60
    )
    assert first_claim is not None
    await session.commit()

    # Simulate a crash followed by lease expiry.
    event.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.commit()

    reclaimed = await repo.claim_next_published(
        worker_id="recovery-worker", lease_seconds=60
    )
    assert reclaimed is not None
    assert reclaimed.event_id == event.event_id
    assert reclaimed.status == OutboxStatus.PROCESSING
    assert reclaimed.claimed_by == "recovery-worker"
