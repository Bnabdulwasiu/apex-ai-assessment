"""Standalone worker process entrypoint.

This is the piece that was missing: nothing in ``src/main.py`` (the API
process) ever calls ``OutboxPublisher.publish_pending()`` or
``ReferralWorker.process_referral()`` — those were previously only
exercised by hand in ``scripts/demo.py`` and in tests. Run this module
as its own process, separate from ``uvicorn``, to actually move
referrals through the pipeline:

    uv run python -m src.worker_main

Two independent, concurrently-running loops:

1. **Outbox publish loop** — polls ``outbox_events`` for PENDING rows
   and marks them PUBLISHED (``OutboxPublisher``). This is the "hand
   the durable event to the broker" step.
2. **Referral processing pool** — runs a configured number of consumer
   loops. Each loop leases one PUBLISHED outbox row at a time
   (``OutboxRepository.claim_next_published``) and runs it through
   ``ReferralWorker.process_referral`` (EHR → AI → Routing → CRM).

Running this as a separate process from the API is a deliberate
architecture decision, not an implementation detail: the outbox table
is the durable, cross-process transport, so the API and worker never
need to share in-process memory (see NFR-07/NFR-08 in
docs/requirements.md and the discussion in docs/architecture.md §3.3).
You can run more than one instance of this process concurrently; the
leased claims prevent concurrent processing. A crashed worker's claim is
recoverable after ``OUTBOX_CLAIM_LEASE_SECONDS``.

Adapters wired here are the same mocks used by the API/demo
(EHR/AI/CRM). Swapping in real integrations means implementing the
``EHRAdapter`` / ``AIAdapter`` / ``CRMAdapter`` interfaces in
``src/adapters/`` and constructing those here instead — nothing else
in this file, or in ``ReferralWorker``, needs to change. That
replaceability is the point of the adapter boundary.
"""

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Callable

from src.adapters.mocks import (
    MockAIAdapter,
    MockCRMAdapter,
    MockEHRAdapter,
    NullEventQueue,
)
from src.config import settings
from src.database import async_session_factory, engine
from src.logging_config import configure_logging, startup_fields
from src.repositories.outbox import OutboxRepository
from src.services.outbox_publisher import OutboxPublisher
from src.services.retry import RetryPolicy
from src.services.routing import RoutingService
from src.services.worker import ReferralWorker

logger = logging.getLogger(__name__)


def _build_retry_policy() -> RetryPolicy:
    """Derive the exponential backoff schedule from settings.

    ``settings`` stores a single base interval (``retry_backoff_base_ms``)
    rather than a list, so we expand it into the documented
    ``base -> 2x -> 4x`` schedule (500ms -> 1s -> 2s by default) here,
    once, rather than hardcoding the list a second time.
    """
    base_seconds = settings.retry_backoff_base_ms / 1000
    intervals = [base_seconds * (2**i) for i in range(settings.retry_max_attempts)]
    return RetryPolicy(
        max_attempts=settings.retry_max_attempts,
        backoff_intervals=intervals,
    )


def _build_referral_worker() -> ReferralWorker:
    """Build the process-shared, concurrency-safe workflow dependencies."""
    return ReferralWorker(
        ehr_adapter=MockEHRAdapter(),
        ai_adapter=MockAIAdapter(),
        routing_service=RoutingService(),
        crm_adapter=MockCRMAdapter(),
        retry_policy=_build_retry_policy(),
    )


async def _outbox_publish_loop(stop_event: asyncio.Event) -> None:
    """Continuously flush PENDING outbox rows to PUBLISHED."""
    publisher = OutboxPublisher(NullEventQueue())

    while not stop_event.is_set():
        try:
            async with async_session_factory() as session:
                published = await publisher.publish_pending(
                    session, batch_size=settings.outbox_batch_size
                )
                if published:
                    logger.info("Outbox publish cycle: %d event(s) published", published)
        except Exception:
            logger.exception("Outbox publish loop iteration failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.outbox_poll_interval_seconds
            )
        except asyncio.TimeoutError:
            pass  # normal — just means it's time to poll again


async def _referral_processing_loop(
    stop_event: asyncio.Event,
    *,
    worker: ReferralWorker,
    worker_id: str,
) -> None:
    """Continuously claim PUBLISHED outbox events and run the pipeline."""
    while not stop_event.is_set():
        claimed_something = False
        try:
            async with async_session_factory() as session:
                outbox_repo = OutboxRepository(session)
                event = await outbox_repo.claim_next_published(
                    worker_id=worker_id,
                    lease_seconds=settings.outbox_claim_lease_seconds,
                )
                if event is not None:
                    claimed_something = True
                    # Persist the lease before any external integration call.
                    await session.commit()
                    logger.info(
                        "Claimed outbox event %s for referral %s",
                        event.event_id,
                        event.referral_id,
                    )
                    await worker.process_referral(
                        session,
                        event.referral_id,
                        correlation_id=event.correlation_id,
                    )
                    consumed = await outbox_repo.mark_consumed(
                        event.event_id,
                        worker_id=worker_id,
                    )
                    if not consumed:
                        raise RuntimeError(
                            f"Outbox lease for {event.event_id} is no longer owned by {worker_id}"
                        )
                    await session.commit()
        except Exception:
            logger.exception("Referral processing loop iteration failed")

        if not claimed_something:
            # Nothing to do — back off before polling again. When we did
            # claim something, loop immediately in case more is waiting.
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.worker_poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass


def _install_signal_handlers(
    stop_event: asyncio.Event,
) -> Callable[[], None]:
    """Request cooperative shutdown on SIGINT/SIGTERM across platforms."""
    loop = asyncio.get_running_loop()
    registered_with_loop: list[signal.Signals] = []
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}
    shutdown_requested = False

    def _handle_signal(sig: signal.Signals) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            logger.info(
                "Shutdown signal %s received; draining in-flight work",
                sig.name,
            )
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
            registered_with_loop.append(sig)
        except NotImplementedError:
            # Windows event loops do not implement add_signal_handler, but
            # signal.signal is supported for a process running on the main
            # thread. Replacing asyncio.Runner's handler here prevents its
            # first Ctrl+C from cancelling an in-flight referral.
            try:
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda _signum, _frame, s=sig: _handle_signal(s))
            except (ValueError, OSError, RuntimeError):
                logger.warning(
                    "Could not install handler for %s; cancellation fallback remains active",
                    sig.name,
                )

    def _restore_signal_handlers() -> None:
        for sig in registered_with_loop:
            loop.remove_signal_handler(sig)
        for sig, previous_handler in previous_handlers.items():
            signal.signal(sig, previous_handler)

    return _restore_signal_handlers


async def _run() -> None:
    configure_logging(service="apex-referral-worker", log_level=settings.log_level)
    stop_event = asyncio.Event()
    restore_signal_handlers = _install_signal_handlers(stop_event)

    logger.info(
        "Worker service started",
        extra=startup_fields(
            version=settings.app_version,
            environment=settings.app_env,
            worker_concurrency=settings.worker_concurrency,
            outbox_poll_interval_seconds=settings.outbox_poll_interval_seconds,
            worker_poll_interval_seconds=settings.worker_poll_interval_seconds,
            outbox_batch_size=settings.outbox_batch_size,
            outbox_claim_lease_seconds=settings.outbox_claim_lease_seconds,
            retry_max_attempts=settings.retry_max_attempts,
        ),
    )

    worker = _build_referral_worker()
    process_id = f"{socket.gethostname()}:{os.getpid()}"
    tasks = [
        asyncio.create_task(
            _outbox_publish_loop(stop_event),
            name="outbox-publisher",
        ),
        *[
            asyncio.create_task(
                _referral_processing_loop(
                    stop_event,
                    worker=worker,
                    worker_id=f"{process_id}:{slot}",
                ),
                name=f"referral-consumer-{slot}",
            )
            for slot in range(1, settings.worker_concurrency + 1)
        ],
    ]
    loop_group = asyncio.gather(*tasks)

    try:
        # Shield active work from asyncio.Runner's cancellation fallback. The
        # stop flag prevents another claim while the current referral drains.
        await asyncio.shield(loop_group)
    except asyncio.CancelledError:
        logger.info("Worker cancellation received; draining in-flight work")
        stop_event.set()
        await loop_group
    finally:
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        restore_signal_handlers()
        await engine.dispose()

    logger.info(
        "Worker process stopped gracefully",
        extra={"event": "service.stopped"},
    )


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # A second interrupt may bypass cooperative draining. Keep the CLI
        # output clean while allowing the operator to force termination.
        logger.warning("Worker process interrupted before graceful shutdown completed")
