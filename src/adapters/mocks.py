"""Mock implementations of integration adapters with test hooks and failure simulation."""

import asyncio
from typing import Any, Optional

from src.adapters.base import (
    AIAdapter,
    AIClassification,
    CRMAdapter,
    CRMCase,
    CRMUpdate,
    EHRAdapter,
    EligibilityResult,
    EventQueue,
    IntegrationNonRetryableError,
    IntegrationRetryableError,
    ReferralContext,
)


class MockEHRAdapter(EHRAdapter):
    """Configurable mock EHR adapter."""

    def __init__(self) -> None:
        self.call_history: list[ReferralContext] = []

    async def check_eligibility(self, referral: ReferralContext) -> EligibilityResult:
        self.call_history.append(referral)

        pid = referral.patient_id.upper()

        # Simulation triggers
        if "INELIGIBLE" in pid:
            return EligibilityResult(
                eligible=False,
                reason_code="INSURANCE_NOT_ELIGIBLE",
            )
        if "TIMEOUT" in pid:
            raise IntegrationRetryableError(
                "EHR connection timed out",
                status_code=504,
            )
        if "500" in pid or "ERROR" in pid:
            raise IntegrationRetryableError(
                "EHR internal server error",
                status_code=500,
            )
        if "400" in pid:
            raise IntegrationNonRetryableError(
                "EHR malformed patient identifier",
                status_code=400,
            )

        digits = "".join(c for c in referral.patient_id if c.isdigit())
        suffix = digits[-4:] if digits else "0001"
        return EligibilityResult(
            eligible=True,
            eligibility_reference=f"ELIG-{suffix.zfill(4)}",
        )


class MockAIAdapter(AIAdapter):
    """Configurable mock AI triage adapter."""

    def __init__(self) -> None:
        self.call_history: list[ReferralContext] = []

    async def classify(self, referral: ReferralContext) -> AIClassification:
        self.call_history.append(referral)

        reason_lower = referral.reason.lower()

        if "timeout" in reason_lower:
            raise IntegrationRetryableError(
                "AI service request timed out",
                status_code=504,
            )
        if "ai_fail" in reason_lower:
            raise IntegrationNonRetryableError(
                "AI service payload rejected",
                status_code=422,
            )

        if "urgent" in reason_lower or "chest pain" in reason_lower:
            return AIClassification(
                classification=referral.specialty.upper(),
                urgency="HIGH",
                confidence=0.96,
            )
        if "routine" in reason_lower:
            return AIClassification(
                classification=referral.specialty.upper(),
                urgency="ROUTINE",
                confidence=0.92,
            )

        return AIClassification(
            classification=referral.specialty.upper(),
            urgency="MEDIUM",
            confidence=0.88,
        )


class MockCRMAdapter(CRMAdapter):
    """Configurable mock CRM adapter with duplicate protection and failure simulation."""

    def __init__(self) -> None:
        self.cases_by_external_ref: dict[str, CRMCase] = {}
        self.cases_by_id: dict[str, CRMCase] = {}
        self.fail_next_n_creates: int = 0
        self.create_call_count: int = 0
        self.find_call_count: int = 0

    async def find_case(self, external_reference: str) -> Optional[CRMCase]:
        self.find_call_count += 1
        return self.cases_by_external_ref.get(external_reference)

    async def create_case(self, referral: ReferralContext) -> CRMCase:
        self.create_call_count += 1

        # Simulate transient failure if configured
        if self.fail_next_n_creates > 0:
            self.fail_next_n_creates -= 1
            raise IntegrationRetryableError(
                "CRM service temporarily unavailable (503 Service Unavailable)",
                status_code=503,
            )

        # Lookup-before-create safety: if case exists, return existing
        if referral.external_reference in self.cases_by_external_ref:
            return self.cases_by_external_ref[referral.external_reference]

        case_id = f"CRM-{len(self.cases_by_external_ref) + 10001}"
        crm_case = CRMCase(
            case_id=case_id,
            external_reference=referral.external_reference,
            status="OPEN",
            metadata={
                "referral_id": referral.referral_id,
                "hospital": referral.hospital,
                "specialty": referral.specialty,
            },
        )
        self.cases_by_external_ref[referral.external_reference] = crm_case
        self.cases_by_id[case_id] = crm_case
        return crm_case

    async def update_case(self, case_id: str, update: CRMUpdate) -> CRMCase:
        existing = self.cases_by_id.get(case_id)
        if not existing:
            raise IntegrationNonRetryableError(
                f"CRM case {case_id} not found",
                status_code=404,
            )

        updated_case = CRMCase(
            case_id=existing.case_id,
            external_reference=existing.external_reference,
            status=update.status or existing.status,
            metadata={**existing.metadata, "notes": update.notes} if update.notes else existing.metadata,
        )
        self.cases_by_id[case_id] = updated_case
        self.cases_by_external_ref[existing.external_reference] = updated_case
        return updated_case


class NullEventQueue(EventQueue):
    """EventQueue placeholder for the database-backed outbox transport.

    In the two-process deployment (API + worker), ``outbox_events`` is
    itself the durable, cross-process queue: the publisher marks a row
    PUBLISHED, the worker claims it via
    ``OutboxRepository.claim_next_published`` (see src/worker_main.py).

    ``OutboxPublisher`` still calls ``queue.publish()`` as its "send to
    the broker" step so that the call site is exactly where a real
    broker client (Kafka/RabbitMQ/SQS producer) would be substituted
    later — this class documents that boundary rather than silently
    doing nothing. ``get()`` is intentionally unused in this mode; the
    worker consumes via the repository claim, not via this method.
    """

    def __init__(self) -> None:
        self.publish_count = 0

    async def publish(self, event: dict[str, Any]) -> None:
        self.publish_count += 1

    async def get(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        raise NotImplementedError(
            "NullEventQueue.get() is not used — the worker consumes via "
            "OutboxRepository.claim_next_published() against the durable "
            "outbox table. Use InMemoryEventQueue for single-process "
            "demo/test flows that exercise the EventQueue.get() path."
        )


class InMemoryEventQueue(EventQueue):
    """In-memory asynchronous queue for local event publishing and consumption."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.published_history: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        self.published_history.append(event)
        await self._queue.put(event)

    async def get(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def qsize(self) -> int:
        return self._queue.qsize()
