"""Tests for POST /api/v1/referrals and GET /api/v1/referrals/{id}.

Covers:
- valid referral accepted (202)
- missing token → 401
- invalid token → 401
- wrong role → 403
- missing Idempotency-Key → 422
- validation errors → 422
- same key + same payload → 202, no duplicate workflow
- same key + different payload → 409
- cross-hospital GET → 404
- not found → 404
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_session
from src.main import app
from src.models.audit import AuditLog
from src.models.idempotency import IdempotencyRecord
from src.models.outbox import OutboxEvent
from src.models.referral import Referral

REFERRAL_URL = "/api/v1/referrals"

VALID_PAYLOAD = {
    "external_reference": "CONTACT-REF-10001",
    "patient": {"id": "PAT-SYN-001"},
    "specialty": "cardiology",
    "reason": "Routine cardiology referral",
    "preferred_hospital": "RIYADH-01",
}


# ---------------------------------------------------------------------------
# Authentication tests (api-contract §8)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_auth_returns_401(
    client: AsyncClient, session: AsyncSession
):
    resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={"Idempotency-Key": "idem-001"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["retryable"] is False
    assert resp.headers["X-Request-ID"] == body["error"]["request_id"]

    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.action == "AUTHENTICATION_FAILED")
        )
    ).scalar_one()
    assert audit.actor == "anonymous"
    assert json.loads(audit.details or "{}") == {"reason": "missing_credentials"}


@pytest.mark.asyncio
async def test_invalid_token_returns_401(
    client: AsyncClient, session: AsyncSession
):
    resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": "Bearer not.a.real.token",
            "Idempotency-Key": "idem-002",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
    request_id = resp.json()["error"]["request_id"]
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.action == "AUTHENTICATION_FAILED",
                AuditLog.request_id == request_id,
            )
        )
    ).scalar_one()
    assert json.loads(audit.details or "{}") == {"reason": "invalid_token"}


# ---------------------------------------------------------------------------
# Authorization tests (api-contract §9)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ops_admin_cannot_submit_referral(
    client: AsyncClient, ops_admin_token: str, session: AsyncSession
):
    """operations_admin role is not authorized to submit referrals."""
    resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": f"Bearer {ops_admin_token}",
            "Idempotency-Key": "idem-003",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    audit = (
        await session.execute(
            select(AuditLog).where(AuditLog.action == "REFERRAL_SUBMIT_DENIED")
        )
    ).scalar_one()
    assert audit.outcome == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Validation tests (api-contract §10)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_idempotency_key_returns_422(
    client: AsyncClient, agent_riyadh01_token: str
):
    resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={"Authorization": f"Bearer {agent_riyadh01_token}"},
        # No Idempotency-Key header
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"] == resp.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_missing_required_field_returns_422(
    client: AsyncClient, agent_riyadh01_token: str
):
    payload = {**VALID_PAYLOAD}
    del payload["specialty"]
    resp = await client.post(
        REFERRAL_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "idem-004",
        },
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert {"field": "specialty", "reason": "Field required"} in error["details"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("specialty", "unsupported-specialty"),
        ("preferred_hospital", "UNKNOWN-99"),
    ],
)
async def test_unsupported_catalog_value_returns_422_without_creating_work(
    client: AsyncClient,
    agent_riyadh01_token: str,
    session: AsyncSession,
    field: str,
    value: str,
):
    external_reference = f"INVALID-{field}"
    payload = {**VALID_PAYLOAD, "external_reference": external_reference, field: value}
    resp = await client.post(
        REFERRAL_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": f"invalid-{field}",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["field"] == field
    referral = (
        await session.execute(
            select(Referral).where(Referral.external_reference == external_reference)
        )
    ).scalar_one_or_none()
    assert referral is None
    outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert outbox_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotency_key", ["bad key", "_bad-start", "x" * 257])
async def test_invalid_idempotency_key_returns_422(
    client: AsyncClient,
    agent_riyadh01_token: str,
    idempotency_key: str,
):
    resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": idempotency_key,
        },
    )

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["field"] == "Idempotency-Key"


# ---------------------------------------------------------------------------
# Happy path (api-contract §4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_referral_returns_202(
    client: AsyncClient, agent_riyadh01_token: str
):
    resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "idem-happy-01",
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "RECEIVED"
    assert body["referral_id"].startswith("ref_")
    assert "request_id" in body
    assert resp.headers["X-Request-ID"] == body["request_id"]


@pytest.mark.asyncio
async def test_client_request_id_is_propagated(
    client: AsyncClient, agent_riyadh01_token: str
):
    request_id = "request-from-client-001"
    resp = await client.post(
        REFERRAL_URL,
        json={**VALID_PAYLOAD, "external_reference": "REQUEST-ID-REF"},
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "idem-request-id-01",
            "X-Request-ID": request_id,
        },
    )

    assert resp.status_code == 202
    assert resp.json()["request_id"] == request_id
    assert resp.headers["X-Request-ID"] == request_id


@pytest.mark.asyncio
async def test_unsafe_client_request_id_is_replaced(
    client: AsyncClient, agent_riyadh01_token: str
):
    resp = await client.post(
        REFERRAL_URL,
        json={**VALID_PAYLOAD, "external_reference": "SAFE-REQUEST-ID-REF"},
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "idem-safe-request-id-01",
            "X-Request-ID": "unsafe request id",
        },
    )

    assert resp.status_code == 202
    assert resp.json()["request_id"].startswith("req-")
    assert resp.json()["request_id"] != "unsafe request id"


# ---------------------------------------------------------------------------
# Idempotency tests (api-contract §7)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_key_same_payload_returns_existing_referral(
    client: AsyncClient, agent_riyadh01_token: str
):
    headers = {
        "Authorization": f"Bearer {agent_riyadh01_token}",
        "Idempotency-Key": "idem-dup-01",
    }
    r1 = await client.post(REFERRAL_URL, json=VALID_PAYLOAD, headers=headers)
    r2 = await client.post(REFERRAL_URL, json=VALID_PAYLOAD, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 202
    # Both must return the SAME referral — no duplicate workflow.
    assert r1.json()["referral_id"] == r2.json()["referral_id"]


@pytest.mark.asyncio
async def test_same_key_different_payload_returns_409(
    client: AsyncClient, agent_riyadh01_token: str
):
    headers = {
        "Authorization": f"Bearer {agent_riyadh01_token}",
        "Idempotency-Key": "idem-conflict-01",
    }
    payload_a = {**VALID_PAYLOAD, "external_reference": "REF-AAA"}
    payload_b = {**VALID_PAYLOAD, "external_reference": "REF-BBB"}

    r1 = await client.post(REFERRAL_URL, json=payload_a, headers=headers)
    r2 = await client.post(REFERRAL_URL, json=payload_b, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_same_key_is_scoped_to_authenticated_client(
    client: AsyncClient,
    agent_riyadh01_token: str,
    integration_svc_token: str,
):
    key = "idem-client-scope-01"
    agent_response = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": key,
        },
    )
    service_response = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": f"Bearer {integration_svc_token}",
            "Idempotency-Key": key,
        },
    )

    assert agent_response.status_code == 202
    assert service_response.status_code == 202
    assert agent_response.json()["referral_id"] != service_response.json()["referral_id"]


@pytest.mark.asyncio
async def test_expired_idempotency_key_can_start_a_new_workflow(
    client: AsyncClient,
    agent_riyadh01_token: str,
    session: AsyncSession,
):
    key = "idem-expired-01"
    headers = {
        "Authorization": f"Bearer {agent_riyadh01_token}",
        "Idempotency-Key": key,
    }
    first = await client.post(REFERRAL_URL, json=VALID_PAYLOAD, headers=headers)
    record = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.client_id == "agent_riyadh01",
                IdempotencyRecord.idempotency_key == key,
            )
        )
    ).scalar_one()
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()

    second_payload = {**VALID_PAYLOAD, "external_reference": "CONTACT-REF-EXPIRED"}
    second = await client.post(REFERRAL_URL, json=second_payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["referral_id"] != second.json()["referral_id"]


# ---------------------------------------------------------------------------
# GET /referrals/{id} tests (api-contract §5, §9)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_referral_success(client: AsyncClient, agent_riyadh01_token: str):
    headers = {
        "Authorization": f"Bearer {agent_riyadh01_token}",
        "Idempotency-Key": "idem-get-01",
    }
    post_resp = await client.post(REFERRAL_URL, json=VALID_PAYLOAD, headers=headers)
    referral_id = post_resp.json()["referral_id"]

    get_resp = await client.get(
        f"{REFERRAL_URL}/{referral_id}",
        headers={"Authorization": f"Bearer {agent_riyadh01_token}"},
    )
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["referral_id"] == referral_id
    assert body["status"] == "RECEIVED"
    # Sensitive fields must NOT appear in the status response.
    assert "patient_id" not in body
    assert "reason" not in body


@pytest.mark.asyncio
async def test_cross_hospital_get_returns_404(
    client: AsyncClient,
    agent_riyadh01_token: str,
    agent_riyadh02_token: str,
    session: AsyncSession,
):
    """Agent from RIYADH-01 cannot see a RIYADH-01 referral via RIYADH-02 credentials."""
    # Submit as RIYADH-01 agent
    post_resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,  # preferred_hospital = RIYADH-01
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "idem-xhospital-01",
        },
    )
    referral_id = post_resp.json()["referral_id"]

    # Attempt to retrieve as RIYADH-02 agent — must get 404, not 403
    # (must not reveal resource exists, api-contract §9)
    get_resp = await client.get(
        f"{REFERRAL_URL}/{referral_id}",
        headers={"Authorization": f"Bearer {agent_riyadh02_token}"},
    )
    assert get_resp.status_code == 404
    assert get_resp.json()["error"]["code"] == "REFERRAL_NOT_FOUND"
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.action == "AUTHORIZATION_FAILED",
                AuditLog.resource_id == referral_id,
            )
        )
    ).scalar_one()
    assert audit.actor == "agent_riyadh02"
    assert audit.outcome == "FORBIDDEN"


@pytest.mark.asyncio
async def test_get_nonexistent_referral_returns_404(
    client: AsyncClient, agent_riyadh01_token: str
):
    resp = await client.get(
        f"{REFERRAL_URL}/ref_doesnotexist",
        headers={"Authorization": f"Bearer {agent_riyadh01_token}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REFERRAL_NOT_FOUND"


@pytest.mark.asyncio
async def test_ops_admin_can_retrieve_any_referral(
    client: AsyncClient,
    agent_riyadh01_token: str,
    ops_admin_token: str,
):
    """operations_admin has cross-hospital read access."""
    post_resp = await client.post(
        REFERRAL_URL,
        json=VALID_PAYLOAD,
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "idem-admin-get-01",
        },
    )
    referral_id = post_resp.json()["referral_id"]

    get_resp = await client.get(
        f"{REFERRAL_URL}/{referral_id}",
        headers={"Authorization": f"Bearer {ops_admin_token}"},
    )
    assert get_resp.status_code == 200


@pytest.mark.asyncio
async def test_non_admin_replay_is_forbidden_and_audited(
    client: AsyncClient,
    agent_riyadh01_token: str,
    session: AsyncSession,
):
    referral_id = "ref-replay-probe"
    resp = await client.post(
        f"{REFERRAL_URL}/{referral_id}/replay",
        headers={"Authorization": f"Bearer {agent_riyadh01_token}"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.action == "AUTHORIZATION_FAILED",
                AuditLog.resource_id == referral_id,
            )
        )
    ).scalar_one()
    assert audit.actor == "agent_riyadh01"


@pytest.mark.asyncio
async def test_unhandled_exception_returns_safe_error(
    agent_riyadh01_token: str,
):
    async def broken_session():
        raise RuntimeError("internal database detail that must not be exposed")
        yield

    app.dependency_overrides[get_session] = broken_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as error_client:
            resp = await error_client.post(
                REFERRAL_URL,
                json=VALID_PAYLOAD,
                headers={
                    "Authorization": f"Bearer {agent_riyadh01_token}",
                    "Idempotency-Key": "idem-server-error-01",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["retryable"] is True
    assert error["request_id"] == resp.headers["X-Request-ID"]
    assert "internal database detail" not in resp.text


@pytest.mark.asyncio
async def test_router_generated_error_uses_public_envelope(client: AsyncClient):
    resp = await client.get("/api/v1/does-not-exist")

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "HTTP_404"
    assert error["message"] == "Not Found"
    assert error["request_id"] == resp.headers["X-Request-ID"]
