"""Distributed token-bucket rate limiting."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError


TOKEN_BUCKET_SCRIPT = """
local current_time = redis.call('TIME')
local now_ms = (current_time[1] * 1000) + math.floor(current_time[2] / 1000)
local requests_per_minute = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_per_ms = requests_per_minute / 60000

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at_ms')
local tokens = tonumber(bucket[1]) or capacity
local updated_at_ms = tonumber(bucket[2]) or now_ms
local elapsed_ms = math.max(0, now_ms - updated_at_ms)
tokens = math.min(capacity, tokens + (elapsed_ms * refill_per_ms))

local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
    allowed = 1
    tokens = tokens - 1
else
    retry_after_ms = math.ceil((1 - tokens) / refill_per_ms)
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_at_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], math.ceil((capacity / refill_per_ms) * 2))
return {allowed, math.floor(tokens), retry_after_ms}
"""


@dataclass(frozen=True)
class RateLimitPolicy:
    """Named token-bucket policy."""

    name: str
    requests_per_minute: int
    burst: int


@dataclass(frozen=True)
class RateLimitDecision:
    """Result returned by a rate-limit backend."""

    allowed: bool
    remaining: int
    retry_after_seconds: int = 0


class RateLimitBackendError(RuntimeError):
    """Raised when the shared limiter backend cannot make a decision."""


class RateLimiter(Protocol):
    async def check(
        self, *, subject: str, policy: RateLimitPolicy
    ) -> RateLimitDecision: ...


class RedisTokenBucketRateLimiter:
    """Atomic shared token buckets backed by Redis."""

    def __init__(self, client: Redis, *, key_prefix: str = "apex:rate-limit:v1"):
        self._client = client
        self._key_prefix = key_prefix

    async def check(
        self, *, subject: str, policy: RateLimitPolicy
    ) -> RateLimitDecision:
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        key = f"{self._key_prefix}:{policy.name}:{subject_hash}"
        try:
            result = await self._client.eval(
                TOKEN_BUCKET_SCRIPT,
                1,
                key,
                policy.requests_per_minute,
                policy.burst,
            )
        except RedisError as exc:
            raise RateLimitBackendError("Redis rate limiter is unavailable") from exc

        allowed, remaining, retry_after_ms = (int(value) for value in result)
        return RateLimitDecision(
            allowed=bool(allowed),
            remaining=max(0, remaining),
            retry_after_seconds=(
                max(1, math.ceil(retry_after_ms / 1000)) if not allowed else 0
            ),
        )


class AllowAllRateLimiter:
    """Disabled/test backend that never rejects a request."""

    async def check(
        self, *, subject: str, policy: RateLimitPolicy
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, remaining=policy.burst)
