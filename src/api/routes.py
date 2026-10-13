"""Referral API routes.

Implements:
  POST /api/v1/referrals      (api-contract §3)
  GET  /api/v1/referrals/{id} (api-contract §5)
"""

import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import (
    REFERRAL_INTAKE_POLICY,
    REFERRAL_REPLAY_POLICY,
    REFERRAL_STATUS_POLICY,
    get_current_user,
    get_request_id,
    rate_limit_dependency,
)
from src.auth import TokenClaims, can_access_referral, require_role
from src.database import get_session
from src.models.outbox import OutboxEvent, OutboxStatus
from src.models.referral import ReferralStatus
from src.repositories.audit import AuditRepository
from src.repositories.outbox import OutboxRepository
from src.repositories.referrals import ReferralRepository
from src.schemas.error import ErrorBody, ErrorResponse
from src.schemas.referral import (
    ReferralCreateRequest,
    ReferralCreateResponse,
    ReferralResponse,
)
from src.services.referral_service import DuplicateKeyConflict, create_referral

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/referrals", tags=["referrals"])

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

limit_referral_intake = rate_limit_dependency(REFERRAL_INTAKE_POLICY)
limit_referral_status = rate_limit_dependency(REFERRAL_STATUS_POLICY)
limit_referral_replay = rate_limit_dependency(REFERRAL_REPLAY_POLICY)


# ---------------------------------------------------------------------------
# POST /api/v1/referrals
# ---------------------------------------------------------------------------

@router.post(
    "",
    dependencies=[Depends(limit_referral_intake)],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReferralCreateResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def submit_referral(
    payload: ReferralCreateRequest,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(None, alias="X-Idempotency-Key"),
    claims: TokenClaims = Depends(get_current_user),
    request_id: str = Depends(get_request_id),
    session: AsyncSession = Depends(get_session),
) -> ReferralCreateResponse:
    """Accept a referral for asynchronous processing.

    Returns 202 Accepted immediately. The EHR → AI → routing → CRM
    workflow runs in the background worker.
    """
    effective_idempotency_key = idempotency_key or x_idempotency_key
    if not effective_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Header 'Idempotency-Key' is required.",
                "request_id": request_id,
                "retryable": False,
            },
        )
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(effective_idempotency_key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "request_id": request_id,
                "retryable": False,
                "details": [
                    {
                        "field": "Idempotency-Key",
                        "reason": (
                            "Use 1-256 ASCII letters, digits, periods, underscores, "
                            "colons, or hyphens; the first character must be alphanumeric."
                        ),
                    }
                ],
            },
        )

    # Authorization: only contact_center_agent and integration_service
    # may submit referrals (FR-04).
    if not require_role(claims, "contact_center_agent", "integration_service"):
        audit = AuditRepository(session)
        await audit.log(
            actor=claims.sub,
            action="REFERRAL_SUBMIT_DENIED",
            resource_type="referral",
            resource_id="N/A",
            outcome="FORBIDDEN",
            request_id=request_id,
            details={"role": claims.role},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "You are not authorized to access this resource.",
                "request_id": request_id,
                "retryable": False,
            },
        )

    try:
        referral, is_new = await create_referral(
            session=session,
            payload=payload,
            client_id=claims.sub,
            idempotency_key=effective_idempotency_key,
            correlation_id=request_id,
        )
    except DuplicateKeyConflict:
        logger.warning(
            "Idempotency key reused with different payload",
            extra={"request_id": request_id, "client_id": claims.sub},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "The idempotency key has already been used with a different request.",
                "request_id": request_id,
                "retryable": False,
            },
        )

    response.headers["X-Request-ID"] = request_id
    log_extra = {
        "referral_id": referral.referral_id,
        "request_id": request_id,
        "is_new": is_new,
    }
    logger.info("Referral intake", extra=log_extra)

    return ReferralCreateResponse(
        referral_id=referral.referral_id,
        status=referral.status.value,
        crm_case_id=referral.crm_case_id,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/referrals/{referral_id}
# ---------------------------------------------------------------------------

@router.get(
    "/{referral_id}",
    dependencies=[Depends(limit_referral_status)],
    response_model=ReferralResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def get_referral(
    referral_id: str,
    response: Response,
    claims: TokenClaims = Depends(get_current_user),
    request_id: str = Depends(get_request_id),
    session: AsyncSession = Depends(get_session),
) -> ReferralResponse:
    """Retrieve current referral status.

    Hospital-scoped: a contact_center_agent may only access referrals
    belonging to their authorized hospital (api-contract §5 / FR-04).
    """
    repo = ReferralRepository(session)
    audit = AuditRepository(session)

    referral = await repo.get_by_referral_id(referral_id)

    # Authorization check BEFORE revealing whether the resource exists.
    # The system must not reveal whether an inaccessible resource exists
    # (api-contract §9).
    if referral is None or not can_access_referral(
        claims, referral.preferred_hospital, referral.hospital
    ):
        if referral is not None:
            # Log the unauthorized access attempt.
            await audit.log(
                actor=claims.sub,
                action="AUTHORIZATION_FAILED",
                resource_type="referral",
                resource_id=referral_id,
                outcome="FORBIDDEN",
                request_id=request_id,
                details={"role": claims.role, "hospital_scope": claims.hospital_scope},
            )
            await session.commit()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "REFERRAL_NOT_FOUND",
                "message": "Referral was not found.",
                "request_id": request_id,
                "retryable": False,
            },
        )

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "Referral status retrieved",
        extra={"referral_id": referral_id, "request_id": request_id},
    )

    return ReferralResponse(
        referral_id=referral.referral_id,
        external_reference=referral.external_reference,
        status=referral.status.value,
        hospital=referral.hospital,
        crm_case_id=referral.crm_case_id,
        updated_at=referral.updated_at,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/referrals/{referral_id}/replay
# ---------------------------------------------------------------------------

@router.post(
    "/{referral_id}/replay",
    dependencies=[Depends(limit_referral_replay)],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def replay_referral(
    referral_id: str,
    response: Response,
    claims: TokenClaims = Depends(get_current_user),
    request_id: str = Depends(get_request_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Replay a failed referral from the Dead Letter Queue.

    Restricted to operations_admin (FR-12 / architecture §6.3 / §11).
    Audited and idempotent.
    """
    repo = ReferralRepository(session)
    audit = AuditRepository(session)
    outbox = OutboxRepository(session)

    # FR-12: Only operations_admin may trigger DLQ replay
    if not require_role(claims, "operations_admin"):
        await audit.log(
            actor=claims.sub,
            action="AUTHORIZATION_FAILED",
            resource_type="referral",
            resource_id=referral_id,
            outcome="FORBIDDEN",
            request_id=request_id,
            details={"attempted_role": claims.role, "required_role": "operations_admin"},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "Only operations_admin may replay dead-letter referrals.",
                "request_id": request_id,
                "retryable": False,
            },
        )

    referral = await repo.get_by_referral_id(referral_id)
    if referral is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "REFERRAL_NOT_FOUND",
                "message": "Referral was not found.",
                "request_id": request_id,
                "retryable": False,
            },
        )

    if referral.status != ReferralStatus.DEAD_LETTER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INVALID_STATE",
                "message": f"Referral is in status '{referral.status.value}'. Only DEAD_LETTER referrals may be replayed.",
                "request_id": request_id,
                "retryable": False,
            },
        )

    previous_failed_step = referral.failed_step
    # Preserve successful step data; the worker resumes from persisted history.
    await repo.prepare_for_replay(referral_id)

    # Create new outbox event
    replay_event = OutboxEvent(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type="REFERRAL_RECEIVED",
        referral_id=referral.referral_id,
        correlation_id=request_id,
        payload=json.dumps({
            "referral_id": referral.referral_id,
            "replayed": True,
            "replayed_by": claims.sub,
        }),
        status=OutboxStatus.PENDING,
    )
    await outbox.create(replay_event)

    # Audit the replay action
    await audit.log(
        actor=claims.sub,
        action="DEAD_LETTER_REPLAYED",
        resource_type="referral",
        resource_id=referral_id,
        outcome="SUCCESS",
        request_id=request_id,
        details={"previous_failed_step": previous_failed_step},
    )

    response.headers["X-Request-ID"] = request_id
    return {
        "referral_id": referral.referral_id,
        "status": "RECEIVED",
        "replayed": True,
        "request_id": request_id,
    }
