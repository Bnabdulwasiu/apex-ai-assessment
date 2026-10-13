"""Worker orchestration engine for asynchronous referral processing."""

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.base import (
    AIAdapter,
    AIClassification,
    CRMAdapter,
    CRMCase,
    EHRAdapter,
    EligibilityResult,
    EventQueue,
    ReferralContext,
    RoutingDecision,
)
from src.models.integration import IntegrationOutcome, IntegrationStep
from src.models.referral import ProcessingStep, Referral, ReferralStatus
from src.repositories.audit import AuditRepository
from src.repositories.integration import IntegrationRepository
from src.repositories.referrals import ReferralRepository
from src.services.retry import RetryPolicy
from src.services.routing import RoutingService

logger = logging.getLogger(__name__)


class ReferralWorker:
    """Orchestrates end-to-end processing for a referral event.
    
    Implements:
    - Step-level idempotency via ``integration_events``
    - Exponential retry policy per external integration
    - Dead-letter routing on non-retryable errors or retry exhaustion
    - Audit logging for every state transition and integration outcome
    - Safe at-least-once delivery handling
    """

    def __init__(
        self,
        ehr_adapter: EHRAdapter,
        ai_adapter: AIAdapter,
        routing_service: RoutingService,
        crm_adapter: CRMAdapter,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        self.ehr = ehr_adapter
        self.ai = ai_adapter
        self.routing = routing_service
        self.crm = crm_adapter
        self.retry_policy = retry_policy or RetryPolicy()

    async def process_referral(
        self,
        session: AsyncSession,
        referral_id: str,
        correlation_id: Optional[str] = None,
    ) -> Optional[Referral]:
        """Process a single referral through the integration pipeline."""
        referral_repo = ReferralRepository(session)
        integration_repo = IntegrationRepository(session)
        audit_repo = AuditRepository(session)

        referral = await referral_repo.get_by_referral_id(referral_id)
        if not referral:
            logger.error("Worker received non-existent referral_id: %s", referral_id)
            return None

        corr_id = correlation_id or referral.correlation_id

        # -------------------------------------------------------------------
        # 1. At-Least-Once Delivery / Terminal State Check (api-contract §21)
        # -------------------------------------------------------------------
        if referral.status in (
            ReferralStatus.COMPLETED,
            ReferralStatus.ELIGIBILITY_FAILED,
            ReferralStatus.DEAD_LETTER,
        ):
            logger.info(
                "Skipping redelivered event for referral in terminal state",
                extra={"referral_id": referral_id, "status": referral.status.value},
            )
            return referral

        # Transition to PROCESSING if currently RECEIVED
        if referral.status == ReferralStatus.RECEIVED:
            await referral_repo.update_status(
                referral_id,
                ReferralStatus.PROCESSING,
                current_step=referral.current_step,
            )
            await audit_repo.log(
                actor="system:worker",
                action="WORKFLOW_STARTED",
                resource_type="referral",
                resource_id=referral_id,
                outcome="SUCCESS",
                request_id=corr_id,
            )

        context = ReferralContext(
            referral_id=referral.referral_id,
            patient_id=referral.patient_id,
            specialty=referral.specialty,
            hospital=referral.preferred_hospital,
            reason=referral.reason,
            external_reference=referral.external_reference,
        )

        # -------------------------------------------------------------------
        # 2. Step: EHR Eligibility Check (api-contract §13)
        # -------------------------------------------------------------------
        ehr_ok = await self._run_ehr_step(
            session=session,
            referral=referral,
            context=context,
            referral_repo=referral_repo,
            integration_repo=integration_repo,
            audit_repo=audit_repo,
            correlation_id=corr_id,
        )
        if not ehr_ok:
            return referral

        # Refresh referral after EHR step
        referral = await referral_repo.get_by_referral_id(referral_id)
        if not referral or referral.status in (ReferralStatus.ELIGIBILITY_FAILED, ReferralStatus.DEAD_LETTER):
            return referral

        # -------------------------------------------------------------------
        # 3. Step: AI Classification (api-contract §14)
        # -------------------------------------------------------------------
        ai_result = await self._run_ai_step(
            session=session,
            referral=referral,
            context=context,
            referral_repo=referral_repo,
            integration_repo=integration_repo,
            audit_repo=audit_repo,
            correlation_id=corr_id,
        )
        if not ai_result:
            return referral

        # Refresh referral after AI step
        referral = await referral_repo.get_by_referral_id(referral_id)
        if not referral or referral.status == ReferralStatus.DEAD_LETTER:
            return referral

        # -------------------------------------------------------------------
        # 4. Step: Deterministic Routing (api-contract §15)
        # -------------------------------------------------------------------
        routing_decision = await self._run_routing_step(
            session=session,
            referral=referral,
            context=context,
            ai_result=ai_result,
            referral_repo=referral_repo,
            integration_repo=integration_repo,
            audit_repo=audit_repo,
            correlation_id=corr_id,
        )

        # Refresh referral after Routing step
        referral = await referral_repo.get_by_referral_id(referral_id)
        if not referral:
            return None

        crm_context = replace(context, hospital=routing_decision.hospital)

        # -------------------------------------------------------------------
        # 5. Step: CRM Case Synchronization (api-contract §16)
        # -------------------------------------------------------------------
        crm_ok = await self._run_crm_step(
            session=session,
            referral=referral,
            context=crm_context,
            referral_repo=referral_repo,
            integration_repo=integration_repo,
            audit_repo=audit_repo,
            correlation_id=corr_id,
        )
        if not crm_ok:
            return referral

        return await referral_repo.get_by_referral_id(referral_id)

    # -----------------------------------------------------------------------
    # Step Execution Helpers
    # -----------------------------------------------------------------------

    async def _run_ehr_step(
        self,
        session: AsyncSession,
        referral: Referral,
        context: ReferralContext,
        referral_repo: ReferralRepository,
        integration_repo: IntegrationRepository,
        audit_repo: AuditRepository,
        correlation_id: str,
    ) -> bool:
        """Run EHR check with step-level idempotency and retries. Returns True if workflow should proceed."""
        # Step-level duplicate protection
        existing = await integration_repo.get_latest_successful_step(
            referral.referral_id, IntegrationStep.EHR_ELIGIBILITY
        )
        if existing:
            return True

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            started_at = datetime.now(timezone.utc)
            try:
                result = await self.ehr.check_eligibility(context)

                if result.eligible:
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.EHR_ELIGIBILITY,
                        attempt=attempt,
                        outcome=IntegrationOutcome.SUCCESS,
                        result_reference=result.eligibility_reference,
                        started_at=started_at,
                    )
                    await referral_repo.update_status(
                        referral.referral_id,
                        ReferralStatus.PROCESSING,
                        current_step=ProcessingStep.EHR_ELIGIBILITY,
                        eligibility_reference=result.eligibility_reference,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="EHR_CHECK_COMPLETED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="SUCCESS",
                        request_id=correlation_id,
                    )
                    return True
                else:
                    # Business rejection (NOT a technical failure)
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.EHR_ELIGIBILITY,
                        attempt=attempt,
                        outcome=IntegrationOutcome.SUCCESS,
                        result_reference=result.reason_code,
                        started_at=started_at,
                    )
                    await referral_repo.update_status(
                        referral.referral_id,
                        ReferralStatus.ELIGIBILITY_FAILED,
                        current_step=ProcessingStep.EHR_ELIGIBILITY,
                        failure_code=result.reason_code,
                        failed_step="EHR_ELIGIBILITY",
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="ELIGIBILITY_FAILED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="BUSINESS_REJECTION",
                        request_id=correlation_id,
                    )
                    return False

            except Exception as exc:
                is_retryable = self.retry_policy.is_retryable(exc)
                status_code = getattr(exc, "status_code", 500)
                error_code = f"HTTP_{status_code}" if status_code else "INTEGRATION_ERROR"

                if is_retryable and attempt < self.retry_policy.max_attempts:
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.EHR_ELIGIBILITY,
                        attempt=attempt,
                        outcome=IntegrationOutcome.FAILED_RETRYABLE,
                        error_code=error_code,
                        error_message=str(exc),
                        started_at=started_at,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="RETRY_ATTEMPTED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="RETRY",
                        request_id=correlation_id,
                    )
                    await self.retry_policy.wait(attempt)
                else:
                    # Retry exhausted or non-retryable -> Dead Letter
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.EHR_ELIGIBILITY,
                        attempt=attempt,
                        outcome=IntegrationOutcome.FAILED_NON_RETRYABLE if not is_retryable else IntegrationOutcome.FAILED_RETRYABLE,
                        error_code=error_code,
                        error_message=str(exc),
                        started_at=started_at,
                    )
                    await referral_repo.update_status(
                        referral.referral_id,
                        ReferralStatus.DEAD_LETTER,
                        current_step=ProcessingStep.EHR_ELIGIBILITY,
                        failure_code=error_code,
                        failed_step="EHR_ELIGIBILITY",
                        retry_count=attempt,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="DEAD_LETTER_CREATED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="DEAD_LETTER",
                        request_id=correlation_id,
                    )
                    return False

        return False

    async def _run_ai_step(
        self,
        session: AsyncSession,
        referral: Referral,
        context: ReferralContext,
        referral_repo: ReferralRepository,
        integration_repo: IntegrationRepository,
        audit_repo: AuditRepository,
        correlation_id: str,
    ) -> Optional[AIClassification]:
        """Run AI classification step."""
        existing = await integration_repo.get_latest_successful_step(
            referral.referral_id, IntegrationStep.AI_CLASSIFICATION
        )
        if existing and referral.ai_classification:
            return AIClassification(
                classification=referral.ai_classification,
                urgency=referral.ai_urgency or "MEDIUM",
                confidence=referral.ai_confidence or 1.0,
            )

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            started_at = datetime.now(timezone.utc)
            try:
                result = await self.ai.classify(context)
                await integration_repo.record_attempt(
                    referral_id=referral.referral_id,
                    step=IntegrationStep.AI_CLASSIFICATION,
                    attempt=attempt,
                    outcome=IntegrationOutcome.SUCCESS,
                    result_reference=result.classification,
                    started_at=started_at,
                )
                await referral_repo.update_status(
                    referral.referral_id,
                    ReferralStatus.PROCESSING,
                    current_step=ProcessingStep.AI_CLASSIFICATION,
                    ai_classification=result.classification,
                    ai_urgency=result.urgency,
                    ai_confidence=result.confidence,
                )
                await audit_repo.log(
                    actor="system:worker",
                    action="AI_CLASSIFICATION_COMPLETED",
                    resource_type="referral",
                    resource_id=referral.referral_id,
                    outcome="SUCCESS",
                    request_id=correlation_id,
                )
                return result

            except Exception as exc:
                is_retryable = self.retry_policy.is_retryable(exc)
                status_code = getattr(exc, "status_code", 500)
                error_code = f"HTTP_{status_code}" if status_code else "INTEGRATION_ERROR"

                if is_retryable and attempt < self.retry_policy.max_attempts:
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.AI_CLASSIFICATION,
                        attempt=attempt,
                        outcome=IntegrationOutcome.FAILED_RETRYABLE,
                        error_code=error_code,
                        error_message=str(exc),
                        started_at=started_at,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="RETRY_ATTEMPTED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="RETRY",
                        request_id=correlation_id,
                    )
                    await self.retry_policy.wait(attempt)
                else:
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.AI_CLASSIFICATION,
                        attempt=attempt,
                        outcome=IntegrationOutcome.FAILED_NON_RETRYABLE if not is_retryable else IntegrationOutcome.FAILED_RETRYABLE,
                        error_code=error_code,
                        error_message=str(exc),
                        started_at=started_at,
                    )
                    await referral_repo.update_status(
                        referral.referral_id,
                        ReferralStatus.DEAD_LETTER,
                        current_step=ProcessingStep.AI_CLASSIFICATION,
                        failure_code=error_code,
                        failed_step="AI_CLASSIFICATION",
                        retry_count=attempt,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="DEAD_LETTER_CREATED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="DEAD_LETTER",
                        request_id=correlation_id,
                    )
                    return None

        return None

    async def _run_routing_step(
        self,
        session: AsyncSession,
        referral: Referral,
        context: ReferralContext,
        ai_result: AIClassification,
        referral_repo: ReferralRepository,
        integration_repo: IntegrationRepository,
        audit_repo: AuditRepository,
        correlation_id: str,
    ) -> RoutingDecision:
        """Run deterministic routing or restore its persisted result."""
        existing = await integration_repo.get_latest_successful_step(
            referral.referral_id, IntegrationStep.ROUTING
        )
        if existing and referral.hospital and referral.routing_reason:
            return RoutingDecision(
                hospital=referral.hospital,
                specialty=ai_result.classification.upper(),
                routing_reason=referral.routing_reason,
            )

        decision = self.routing.route(context, ai_result)

        await integration_repo.record_attempt(
            referral_id=referral.referral_id,
            step=IntegrationStep.ROUTING,
            attempt=1,
            outcome=IntegrationOutcome.SUCCESS,
            result_reference=decision.routing_reason,
        )
        await referral_repo.update_status(
            referral.referral_id,
            ReferralStatus.ROUTED,
            current_step=ProcessingStep.ROUTING,
            hospital=decision.hospital,
            routing_reason=decision.routing_reason,
        )
        await audit_repo.log(
            actor="system:worker",
            action="REFERRAL_STATE_CHANGED",
            resource_type="referral",
            resource_id=referral.referral_id,
            outcome="ROUTED",
            request_id=correlation_id,
        )
        return decision

    async def _run_crm_step(
        self,
        session: AsyncSession,
        referral: Referral,
        context: ReferralContext,
        referral_repo: ReferralRepository,
        integration_repo: IntegrationRepository,
        audit_repo: AuditRepository,
        correlation_id: str,
    ) -> bool:
        """Run CRM synchronization with lookup-before-create and retries."""
        existing = await integration_repo.get_latest_successful_step(
            referral.referral_id, IntegrationStep.CRM
        )
        if existing and referral.crm_case_id:
            return True

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            started_at = datetime.now(timezone.utc)
            try:
                await referral_repo.update_status(
                    referral.referral_id,
                    ReferralStatus.CRM_PENDING,
                    current_step=ProcessingStep.CRM,
                )

                # Anti-duplicate pattern: check if case already exists in CRM
                crm_case = await self.crm.find_case(context.external_reference)
                if not crm_case:
                    crm_case = await self.crm.create_case(context)

                await integration_repo.record_attempt(
                    referral_id=referral.referral_id,
                    step=IntegrationStep.CRM,
                    attempt=attempt,
                    outcome=IntegrationOutcome.SUCCESS,
                    result_reference=crm_case.case_id,
                    started_at=started_at,
                )
                await referral_repo.update_status(
                    referral.referral_id,
                    ReferralStatus.COMPLETED,
                    current_step=ProcessingStep.COMPLETE,
                    crm_case_id=crm_case.case_id,
                )
                await audit_repo.log(
                    actor="system:worker",
                    action="CRM_CASE_CREATED",
                    resource_type="referral",
                    resource_id=referral.referral_id,
                    outcome="SUCCESS",
                    request_id=correlation_id,
                )
                await audit_repo.log(
                    actor="system:worker",
                    action="REFERRAL_STATE_CHANGED",
                    resource_type="referral",
                    resource_id=referral.referral_id,
                    outcome="COMPLETED",
                    request_id=correlation_id,
                )
                return True

            except Exception as exc:
                is_retryable = self.retry_policy.is_retryable(exc)
                status_code = getattr(exc, "status_code", 500)
                error_code = f"HTTP_{status_code}" if status_code else "INTEGRATION_ERROR"

                if is_retryable and attempt < self.retry_policy.max_attempts:
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.CRM,
                        attempt=attempt,
                        outcome=IntegrationOutcome.FAILED_RETRYABLE,
                        error_code=error_code,
                        error_message=str(exc),
                        started_at=started_at,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="RETRY_ATTEMPTED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="RETRY",
                        request_id=correlation_id,
                    )
                    await self.retry_policy.wait(attempt)
                else:
                    await integration_repo.record_attempt(
                        referral_id=referral.referral_id,
                        step=IntegrationStep.CRM,
                        attempt=attempt,
                        outcome=IntegrationOutcome.FAILED_NON_RETRYABLE if not is_retryable else IntegrationOutcome.FAILED_RETRYABLE,
                        error_code=error_code,
                        error_message=str(exc),
                        started_at=started_at,
                    )
                    await referral_repo.update_status(
                        referral.referral_id,
                        ReferralStatus.DEAD_LETTER,
                        current_step=ProcessingStep.CRM,
                        failure_code=error_code,
                        failed_step="CRM",
                        retry_count=attempt,
                    )
                    await audit_repo.log(
                        actor="system:worker",
                        action="DEAD_LETTER_CREATED",
                        resource_type="referral",
                        resource_id=referral.referral_id,
                        outcome="DEAD_LETTER",
                        request_id=correlation_id,
                    )
                    return False

        return False
