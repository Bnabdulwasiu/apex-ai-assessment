"""Outbox publisher — reliably publishes durable outbox events to the event queue."""

import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.base import EventQueue
from src.repositories.audit import AuditRepository
from src.repositories.outbox import OutboxRepository

logger = logging.getLogger(__name__)


class OutboxPublisher:
    """Polls pending outbox records and publishes them to the EventQueue.
    
    Guarantees at-least-once delivery from database outbox to queue.
    Conforms to docs/api-contract.md §20:
    - Events remain PENDING during queue outages.
    - Marked PUBLISHED only after queue delivery is confirmed.
    """

    def __init__(self, event_queue: EventQueue) -> None:
        self.queue = event_queue

    async def publish_pending(
        self,
        session: AsyncSession,
        batch_size: int = 50,
    ) -> int:
        """Fetch and publish a batch of pending events.
        
        Returns the count of successfully published events.
        """
        outbox_repo = OutboxRepository(session)
        audit_repo = AuditRepository(session)

        pending_events = await outbox_repo.get_pending(limit=batch_size)
        published_count = 0

        for event in pending_events:
            try:
                # Prepare minimal event payload (api-contract §17)
                msg = {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "referral_id": event.referral_id,
                    "correlation_id": event.correlation_id,
                    "attempt": event.attempt_count + 1,
                }
                # Publish to message broker / event queue
                await self.queue.publish(msg)

                # Mark as published
                await outbox_repo.mark_published(event.event_id)
                await audit_repo.log(
                    actor="system:outbox_publisher",
                    action="OUTBOX_EVENT_PUBLISHED",
                    resource_type="outbox_event",
                    resource_id=event.event_id,
                    outcome="SUCCESS",
                    request_id=event.correlation_id,
                )
                published_count += 1

            except Exception as exc:
                logger.warning(
                    "Failed to publish outbox event %s to queue: %s",
                    event.event_id,
                    exc,
                )
                await outbox_repo.increment_attempt(event.event_id)
                # Stop batch on queue connectivity failure to preserve ordering
                break

        await session.commit()
        return published_count
