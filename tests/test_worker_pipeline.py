"""End-to-end tests for worker progression, retries, and DLQ replay."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.base import AIClassification, ReferralContext, RoutingDecision
from src.adapters.mocks import MockAIAdapter, MockCRMAdapter, MockEHRAdapter
from src.models.audit import AuditLog
from src.models.integration import IntegrationEvent, IntegrationOutcome, IntegrationStep
from src.models.outbox import OutboxEvent, OutboxStatus
from src.models.referral import ProcessingStep, Referral, ReferralStatus
from src.services.retry import RetryPolicy
from src.services.routing import RoutingService
from src.services.worker import ReferralWorker


class CountingRoutingService(RoutingService):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    def route(
        self,
        referral: ReferralContext,
        ai_result: AIClassification,
    ) -> RoutingDecision:
        self.call_count += 1
        return super().route(referral, ai_result)


def build_worker():
    ehr = MockEHRAdapter()
    ai = MockAIAdapter()
    routing = CountingRoutingService()
    crm = MockCRMAdapter()
    worker = ReferralWorker(
        ehr_adapter=ehr,
        ai_adapter=ai,
        routing_service=routing,
        crm_adapter=crm,
        retry_policy=RetryPolicy(max_attempts=3, apply_backoff=False),
    )
    return worker, ehr, ai, routing, crm


async def create_referral(
    session: AsyncSession,
    *,
    referral_id: str,
    patient_id: str = "PAT-SYN-001",
    specialty: str = "cardiology",
    reason: str = "Routine referral",
    external_reference: str | None = None,
) -> Referral:
    referral = Referral(
        referral_id=referral_id,
        external_reference=external_reference or f"EXT-{referral_id}",
        patient_id=patient_id,
        specialty=specialty,
        reason=reason,
        preferred_hospital="RIYADH-01",
        status=ReferralStatus.RECEIVED,
        current_step=ProcessingStep.INTAKE,
        client_id="agent_riyadh01",
        correlation_id=f"request-{referral_id}",
    )
    session.add(referral)
    await session.flush()
    return referral


@pytest.mark.asyncio
async def test_worker_completes_once_and_ignores_duplicate_delivery(
    session: AsyncSession,
):
    referral = await create_referral(
        session,
        referral_id="ref-worker-happy",
        specialty="oncology",
    )
    worker, ehr, ai, routing, crm = build_worker()

    result = await worker.process_referral(session, referral.referral_id)
    assert result is not None
    assert result.status == ReferralStatus.COMPLETED
    assert result.hospital == "RIYADH-02"
    assert result.crm_case_id is not None
    crm_case = crm.cases_by_external_ref[referral.external_reference]
    assert crm_case.metadata["hospital"] == "RIYADH-02"

    first_counts = (
        len(ehr.call_history),
        len(ai.call_history),
        routing.call_count,
        crm.create_call_count,
    )
    duplicate_result = await worker.process_referral(session, referral.referral_id)
    assert duplicate_result is not None
    assert duplicate_result.status == ReferralStatus.COMPLETED
    assert (
        len(ehr.call_history),
        len(ai.call_history),
        routing.call_count,
        crm.create_call_count,
    ) == first_counts


@pytest.mark.asyncio
async def test_ineligible_referral_stops_before_ai_routing_and_crm(
    session: AsyncSession,
):
    referral = await create_referral(
        session,
        referral_id="ref-worker-ineligible",
        patient_id="PAT-INELIGIBLE-001",
    )
    worker, ehr, ai, routing, crm = build_worker()

    result = await worker.process_referral(session, referral.referral_id)
    assert result is not None
    assert result.status == ReferralStatus.ELIGIBILITY_FAILED
    assert len(ehr.call_history) == 1
    assert len(ai.call_history) == 0
    assert routing.call_count == 0
    assert crm.create_call_count == 0


@pytest.mark.asyncio
async def test_crm_dlq_replay_resumes_without_repeating_successful_steps(
    session: AsyncSession,
    client: AsyncClient,
    ops_admin_token: str,
):
    referral = await create_referral(
        session,
        referral_id="ref-worker-replay",
        external_reference="EXT-WORKER-REPLAY",
    )
    worker, ehr, ai, routing, crm = build_worker()
    crm.fail_next_n_creates = 3

    failed = await worker.process_referral(session, referral.referral_id)
    assert failed is not None
    assert failed.status == ReferralStatus.DEAD_LETTER
    assert failed.failed_step == "CRM"
    assert failed.retry_count == 3
    assert len(ehr.call_history) == 1
    assert len(ai.call_history) == 1
    assert routing.call_count == 1
    assert crm.create_call_count == 3
    await session.commit()

    replay_response = await client.post(
        f"/api/v1/referrals/{referral.referral_id}/replay",
        headers={"Authorization": f"Bearer {ops_admin_token}"},
    )
    assert replay_response.status_code == 200
    await session.refresh(referral)
    assert referral.status == ReferralStatus.RECEIVED
    assert referral.current_step == ProcessingStep.CRM
    assert referral.failure_code is None
    assert referral.failed_step is None
    assert referral.retry_count == 0

    replay_event = (
        await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.referral_id == referral.referral_id,
                OutboxEvent.status == OutboxStatus.PENDING,
            )
        )
    ).scalar_one()
    assert replay_event is not None

    completed = await worker.process_referral(session, referral.referral_id)
    assert completed is not None
    assert completed.status == ReferralStatus.COMPLETED
    assert len(ehr.call_history) == 1
    assert len(ai.call_history) == 1
    assert routing.call_count == 1
    assert crm.create_call_count == 4

    routing_success_count = await session.scalar(
        select(func.count())
        .select_from(IntegrationEvent)
        .where(
            IntegrationEvent.referral_id == referral.referral_id,
            IntegrationEvent.step == IntegrationStep.ROUTING,
            IntegrationEvent.outcome == IntegrationOutcome.SUCCESS,
        )
    )
    assert routing_success_count == 1
    replay_audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.resource_id == referral.referral_id,
                AuditLog.action == "DEAD_LETTER_REPLAYED",
            )
        )
    ).scalar_one()
    assert replay_audit.outcome == "SUCCESS"
