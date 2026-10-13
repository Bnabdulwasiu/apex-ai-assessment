"""Unit tests for integration adapters and routing engine."""

import pytest

from src.adapters.base import (
    AIClassification,
    IntegrationNonRetryableError,
    IntegrationRetryableError,
    ReferralContext,
)
from src.adapters.mocks import (
    InMemoryEventQueue,
    MockAIAdapter,
    MockCRMAdapter,
    MockEHRAdapter,
)
from src.services.routing import RoutingService


@pytest.fixture
def sample_context() -> ReferralContext:
    return ReferralContext(
        referral_id="ref_01J8ABC123",
        patient_id="PAT-SYN-001",
        specialty="cardiology",
        hospital="RIYADH-01",
        reason="Routine cardiology follow-up",
        external_reference="REF-EXT-1001",
    )


# ---------------------------------------------------------------------------
# EHR Adapter Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ehr_eligible(sample_context: ReferralContext):
    ehr = MockEHRAdapter()
    result = await ehr.check_eligibility(sample_context)
    assert result.eligible is True
    assert result.eligibility_reference == "ELIG-0001"
    assert len(ehr.call_history) == 1


@pytest.mark.asyncio
async def test_ehr_ineligible_is_business_outcome():
    ehr = MockEHRAdapter()
    context = ReferralContext(
        referral_id="ref_01J8ABC123",
        patient_id="PAT-INELIGIBLE-999",
        specialty="cardiology",
        hospital="RIYADH-01",
        reason="Follow up",
        external_reference="REF-EXT-1002",
    )
    result = await ehr.check_eligibility(context)
    assert result.eligible is False
    assert result.reason_code == "INSURANCE_NOT_ELIGIBLE"


@pytest.mark.asyncio
async def test_ehr_retryable_errors(sample_context: ReferralContext):
    ehr = MockEHRAdapter()

    # Timeout
    timeout_ctx = ReferralContext(
        **{**sample_context.__dict__, "patient_id": "PAT-TIMEOUT-123"}
    )
    with pytest.raises(IntegrationRetryableError) as exc_info:
        await ehr.check_eligibility(timeout_ctx)
    assert exc_info.value.status_code == 504
    assert exc_info.value.is_retryable is True

    # 500 error
    error_ctx = ReferralContext(
        **{**sample_context.__dict__, "patient_id": "PAT-500-123"}
    )
    with pytest.raises(IntegrationRetryableError) as exc_info:
        await ehr.check_eligibility(error_ctx)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_ehr_non_retryable_error(sample_context: ReferralContext):
    ehr = MockEHRAdapter()
    bad_ctx = ReferralContext(
        **{**sample_context.__dict__, "patient_id": "PAT-400-BAD"}
    )
    with pytest.raises(IntegrationNonRetryableError) as exc_info:
        await ehr.check_eligibility(bad_ctx)
    assert exc_info.value.status_code == 400
    assert exc_info.value.is_retryable is False


# ---------------------------------------------------------------------------
# AI Adapter Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_classify_urgent(sample_context: ReferralContext):
    ai = MockAIAdapter()
    urgent_ctx = ReferralContext(
        **{**sample_context.__dict__, "reason": "Severe chest pain and palpitations"}
    )
    result = await ai.classify(urgent_ctx)
    assert result.classification == "CARDIOLOGY"
    assert result.urgency == "HIGH"
    assert result.confidence >= 0.95


@pytest.mark.asyncio
async def test_ai_classify_routine(sample_context: ReferralContext):
    ai = MockAIAdapter()
    result = await ai.classify(sample_context)
    assert result.urgency == "ROUTINE"
    assert result.confidence > 0.90


@pytest.mark.asyncio
async def test_ai_classify_timeout(sample_context: ReferralContext):
    ai = MockAIAdapter()
    timeout_ctx = ReferralContext(
        **{**sample_context.__dict__, "reason": "timeout test"}
    )
    with pytest.raises(IntegrationRetryableError) as exc_info:
        await ai.classify(timeout_ctx)
    assert exc_info.value.status_code == 504


# ---------------------------------------------------------------------------
# CRM Adapter Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_create_and_find_case(sample_context: ReferralContext):
    crm = MockCRMAdapter()
    # Before creation: not found
    existing = await crm.find_case(sample_context.external_reference)
    assert existing is None

    # Create case
    case = await crm.create_case(sample_context)
    assert case.case_id == "CRM-10001"
    assert case.external_reference == sample_context.external_reference
    assert case.status == "OPEN"

    # After creation: found
    found = await crm.find_case(sample_context.external_reference)
    assert found is not None
    assert found.case_id == "CRM-10001"


@pytest.mark.asyncio
async def test_crm_duplicate_create_is_safe(sample_context: ReferralContext):
    crm = MockCRMAdapter()
    case1 = await crm.create_case(sample_context)
    case2 = await crm.create_case(sample_context)
    assert case1.case_id == case2.case_id
    assert len(crm.cases_by_external_ref) == 1


@pytest.mark.asyncio
async def test_crm_transient_failure_simulation(sample_context: ReferralContext):
    crm = MockCRMAdapter()
    crm.fail_next_n_creates = 2

    # Attempt 1: 503
    with pytest.raises(IntegrationRetryableError) as exc1:
        await crm.create_case(sample_context)
    assert exc1.value.status_code == 503

    # Attempt 2: 503
    with pytest.raises(IntegrationRetryableError) as exc2:
        await crm.create_case(sample_context)
    assert exc2.value.status_code == 503

    # Attempt 3: succeeds
    case = await crm.create_case(sample_context)
    assert case.case_id == "CRM-10001"


# ---------------------------------------------------------------------------
# Routing Service Tests
# ---------------------------------------------------------------------------

def test_routing_service_matching_rule(sample_context: ReferralContext):
    router = RoutingService()
    ai_result = AIClassification(
        classification="CARDIOLOGY",
        urgency="HIGH",
        confidence=0.95,
    )
    decision = router.route(sample_context, ai_result)
    assert decision.hospital == "RIYADH-01"
    assert decision.specialty == "CARDIOLOGY"
    assert decision.routing_reason == "SPECIALTY_AND_HOSPITAL_RULE"


def test_routing_service_transfer_rule(sample_context: ReferralContext):
    router = RoutingService()
    # Oncology is only supported at RIYADH-02, not RIYADH-01
    ai_result = AIClassification(
        classification="ONCOLOGY",
        urgency="MEDIUM",
        confidence=0.91,
    )
    decision = router.route(sample_context, ai_result)
    assert decision.hospital == "RIYADH-02"
    assert decision.specialty == "ONCOLOGY"
    assert decision.routing_reason == "CROSS_FACILITY_CAPABILITY_TRANSFER"


# ---------------------------------------------------------------------------
# Event Queue Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_memory_event_queue():
    queue = InMemoryEventQueue()
    assert queue.qsize() == 0

    event = {"event_id": "evt_123", "event_type": "REFERRAL_RECEIVED"}
    await queue.publish(event)
    assert queue.qsize() == 1

    received = await queue.get(timeout=1.0)
    assert received == event
    assert queue.qsize() == 0

    timed_out = await queue.get(timeout=0.01)
    assert timed_out is None
