"""Rate-limit backend lifecycle and dependency provider."""

from redis.asyncio import Redis

from src.config import settings
from src.services.rate_limiter import (
    AllowAllRateLimiter,
    RateLimiter,
    RedisTokenBucketRateLimiter,
)

_redis_client: Redis | None = None

if settings.rate_limit_enabled:
    _redis_client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.rate_limit_backend_timeout_seconds,
        socket_timeout=settings.rate_limit_backend_timeout_seconds,
        retry_on_timeout=False,
    )
    _rate_limiter: RateLimiter = RedisTokenBucketRateLimiter(_redis_client)
else:
    _rate_limiter = AllowAllRateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Return the process-wide shared limiter adapter."""
    return _rate_limiter


async def close_rate_limit_backend() -> None:
    """Close the Redis connection pool during graceful API shutdown."""
    if _redis_client is not None:
        await _redis_client.aclose()
