"""Referral service.

Orchestrates referral intake:
1. Hash the request body for idempotency comparison.
2. Check (client_id, idempotency_key) — return cached result or
   raise 409 if the key was reused with a different payload.
3. Generate a referral ID.
4. In a single DB transaction: insert referral + outbox event.
5. Return the new referral.

The service does NOT directly call EHR, AI, or CRM — that is
the worker's responsibility.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.outbox import OutboxEvent, OutboxStatus
from src.models.referral import ProcessingStep, Referral, ReferralStatus
from src.repositories.audit import AuditRepository
from src.repositories.idempotency import IdempotencyConflict, IdempotencyRepository
from src.repositories.outbox import OutboxRepository
from src.repositories.referrals import ReferralRepository
from src.schemas.referral import ReferralCreateRequest


class DuplicateKeyConflict(Exception):
    """Re-raised from the repository layer for the API to catch."""

    def __init__(self, existing_referral_id: str) -> None:
        self.existing_referral_id = existing_referral_id


def _hash_request(payload: ReferralCreateRequest) -> str:
    """Stable SHA-256 digest of the canonical request body.

    Used to detect same-key-different-payload conflicts (api-contract §7).
    The digest is computed on the sorted JSON serialisation so field
    ordering in the client request does not matter.
    """
    canonical = json.dumps(payload.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _new_referral_id() -> str:
    """Generate a lexicographically sortable referral identifier."""
    return f"ref_{uuid.uuid4().hex[:12]}"


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


async def create_referral(
    *,
    session: AsyncSession,
    payload: ReferralCreateRequest,
    client_id: str,
    idempotency_key: str,
    correlation_id: str,
) -> tuple[Referral, bool]:
    """Create a new referral or return the existing one for duplicate requests.

    Returns
    -------
    (Referral, is_new)
        ``is_new=True`` when a new referral was created (respond 202).
        ``is_new=False`` when the idempotency key matched a previous
        request (still respond 202 with the existing referral).

    Raises
    ------
    DuplicateKeyConflict
        When the same idempotency key is reused with a *different* payload
        (respond 409).
    """
    request_hash = _hash_request(payload)
    referral_id = _new_referral_id()

    idempotency_repo = IdempotencyRepository(session)
    referral_repo = ReferralRepository(session)
    outbox_repo = OutboxRepository(session)
    audit_repo = AuditRepository(session)

    # --- Idempotency check ---
    try:
        existing_record = await idempotency_repo.reserve(
            client_id=client_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            referral_id=referral_id,
        )
    except IdempotencyConflict as exc:
        raise DuplicateKeyConflict(exc.existing_referral_id) from exc

    if existing_record is not None:
        # Same key + same payload — return the existing referral.
        referral = await referral_repo.get_by_referral_id(existing_record.referral_id)
        if referral is None:
            raise RuntimeError("Idempotency record references a missing referral")
        return referral, False

    # --- New referral: create referral + outbox event in one transaction ---
    event_id = _new_event_id()

    referral = Referral(
        referral_id=referral_id,
        external_reference=payload.external_reference,
        patient_id=payload.patient.id,
        reason=payload.reason,
        specialty=payload.specialty,
        preferred_hospital=payload.preferred_hospital,
        hospital=None,
        status=ReferralStatus.RECEIVED,
        current_step=ProcessingStep.INTAKE,
        client_id=client_id,
        correlation_id=correlation_id,
    )
    await referral_repo.create(referral)

    # Outbox event carries identifiers only — no patient data (architecture §12).
    outbox_payload = json.dumps(
        {
            "event_id": event_id,
            "event_type": "REFERRAL_RECEIVED",
            "referral_id": referral_id,
            "correlation_id": correlation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    outbox_event = OutboxEvent(
        event_id=event_id,
        event_type="REFERRAL_RECEIVED",
        referral_id=referral_id,
        correlation_id=correlation_id,
        payload=outbox_payload,
        status=OutboxStatus.PENDING,
    )
    await outbox_repo.create(outbox_event)

    # Audit: REFERRAL_CREATED
    await audit_repo.log(
        actor=client_id,
        action="REFERRAL_CREATED",
        resource_type="referral",
        resource_id=referral_id,
        outcome="SUCCESS",
        request_id=correlation_id,
        details={"specialty": payload.specialty, "hospital": payload.preferred_hospital},
    )

    return referral, True
