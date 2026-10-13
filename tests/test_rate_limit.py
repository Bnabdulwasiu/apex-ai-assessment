"""Distributed API rate-limiting tests."""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from src.api.dependencies import get_rate_limiter
from src.main import app
from src.services.rate_limiter import (
    RateLimitBackendError,
    RateLimitDecision,
    RateLimitPolicy,
    RedisTokenBucketRateLimiter,
)


class StubLimiter:
    def __init__(self, decision=None, error: Exception | None = None):
        self.decision = decision or RateLimitDecision(True, 9)
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def check(self, *, subject: str, policy: RateLimitPolicy):
        self.calls.append((subject, policy.name))
        if self.error:
            raise self.error
        return self.decision


@pytest.mark.asyncio
async def test_redis_limiter_hashes_identity_and_maps_decision():
    captured: dict[str, object] = {}

    async def eval_script(*args):
        captured["args"] = args
        return [0, 0, 1501]

    limiter = RedisTokenBucketRateLimiter(SimpleNamespace(eval=eval_script))
    decision = await limiter.check(
        subject="client:agent_riyadh01",
        policy=RateLimitPolicy("referral-intake", 30, 10),
    )

    assert decision == RateLimitDecision(False, 0, 2)
    args = captured["args"]
    assert "agent_riyadh01" not in str(args[2])
    assert str(args[2]).startswith("apex:rate-limit:v1:referral-intake:")


@pytest.mark.asyncio
async def test_redis_failure_is_normalized():
    async def fail(*_):
        raise RedisConnectionError("connection details must not leak")

    limiter = RedisTokenBucketRateLimiter(SimpleNamespace(eval=fail))
    with pytest.raises(RateLimitBackendError):
        await limiter.check(
            subject="client:test",
            policy=RateLimitPolicy("test", 1, 1),
        )


@pytest.mark.asyncio
async def test_referral_limit_returns_standard_429(
    client: AsyncClient, agent_riyadh01_token: str
):
    limiter = StubLimiter(RateLimitDecision(False, 0, 7))
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    response = await client.post(
        "/api/v1/referrals",
        json={
            "external_reference": "RATE-LIMIT-001",
            "patient": {"id": "PAT-SYN-RATE"},
            "specialty": "cardiology",
            "reason": "Synthetic rate limit test",
            "preferred_hospital": "RIYADH-01",
        },
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "rate-limit-001",
        },
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.headers["X-RateLimit-Policy"] == "referral-intake"
    assert response.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert response.json()["error"]["retryable"] is True
    assert limiter.calls == [("client:agent_riyadh01", "referral-intake")]


@pytest.mark.asyncio
async def test_rate_limit_backend_failure_fails_open(
    client: AsyncClient, agent_riyadh01_token: str
):
    limiter = StubLimiter(error=RateLimitBackendError("unavailable"))
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    response = await client.post(
        "/api/v1/referrals",
        json={
            "external_reference": "RATE-LIMIT-FAIL-OPEN",
            "patient": {"id": "PAT-SYN-RATE-OPEN"},
            "specialty": "cardiology",
            "reason": "Synthetic backend failure test",
            "preferred_hospital": "RIYADH-01",
        },
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "rate-limit-fail-open",
        },
    )

    assert response.status_code == 202


@pytest.mark.asyncio
async def test_authentication_failure_has_ip_limit(client: AsyncClient):
    limiter = StubLimiter(RateLimitDecision(False, 0, 6))
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    response = await client.get("/api/v1/referrals/ref_missing")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "6"
    assert limiter.calls[0][1] == "authentication-failure"
    assert limiter.calls[0][0].startswith("ip:")


@pytest.mark.asyncio
async def test_health_endpoint_is_not_rate_limited(client: AsyncClient):
    limiter = StubLimiter(RateLimitDecision(False, 0, 60))
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    response = await client.get("/health/live")

    assert response.status_code == 200
    assert limiter.calls == []


@pytest.mark.asyncio
async def test_routes_apply_distinct_policies(
    client: AsyncClient,
    agent_riyadh01_token: str,
    ops_admin_token: str,
):
    limiter = StubLimiter()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    created = await client.post(
        "/api/v1/referrals",
        json={
            "external_reference": "RATE-POLICY-MAP",
            "patient": {"id": "PAT-SYN-POLICY"},
            "specialty": "cardiology",
            "reason": "Synthetic policy mapping test",
            "preferred_hospital": "RIYADH-01",
        },
        headers={
            "Authorization": f"Bearer {agent_riyadh01_token}",
            "Idempotency-Key": "rate-policy-map",
        },
    )
    referral_id = created.json()["referral_id"]
    fetched = await client.get(
        f"/api/v1/referrals/{referral_id}",
        headers={"Authorization": f"Bearer {agent_riyadh01_token}"},
    )
    replayed = await client.post(
        "/api/v1/referrals/ref_missing/replay",
        headers={"Authorization": f"Bearer {ops_admin_token}"},
    )

    assert created.status_code == 202
    assert created.headers["X-RateLimit-Policy"] == "referral-intake"
    assert fetched.status_code == 200
    assert fetched.headers["X-RateLimit-Policy"] == "referral-status"
    assert replayed.status_code == 404
    assert [policy for _, policy in limiter.calls] == [
        "referral-intake",
        "referral-status",
        "referral-replay",
    ]
