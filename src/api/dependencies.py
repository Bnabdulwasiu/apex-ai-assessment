"""FastAPI request dependencies.

Provides:
- ``get_current_user`` — validates the Bearer token and returns ``TokenClaims``
- ``get_request_id`` — returns the request correlation ID assigned by middleware
"""

import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import TokenClaims, verify_token
from src.database import get_session
from src.config import settings
from src.rate_limit import get_rate_limiter
from src.repositories.audit import AuditRepository
from src.services.rate_limiter import (
    RateLimitBackendError,
    RateLimiter,
    RateLimitPolicy,
)

logger = logging.getLogger(__name__)

# Declares the ``bearerAuth`` security scheme in the OpenAPI document so
# Swagger UI renders an Authorize dialog that automatically sends the
# ``Authorization: Bearer <token>`` header. ``auto_error=False`` lets us
# return the contract's 401 body ourselves instead of FastAPI's default 403.
_bearer_scheme = HTTPBearer(auto_error=False)


def _policy(name: str, requests_per_minute: int, burst: int) -> RateLimitPolicy:
    return RateLimitPolicy(
        name=name,
        requests_per_minute=requests_per_minute,
        burst=burst,
    )


REFERRAL_INTAKE_POLICY = _policy(
    "referral-intake",
    settings.rate_limit_referral_per_minute,
    settings.rate_limit_referral_burst,
)
REFERRAL_STATUS_POLICY = _policy(
    "referral-status",
    settings.rate_limit_status_per_minute,
    settings.rate_limit_status_burst,
)
REFERRAL_REPLAY_POLICY = _policy(
    "referral-replay",
    settings.rate_limit_replay_per_minute,
    settings.rate_limit_replay_burst,
)
AUTH_FAILURE_POLICY = _policy(
    "authentication-failure",
    settings.rate_limit_auth_failure_per_minute,
    settings.rate_limit_auth_failure_burst,
)


def get_request_id(request: Request) -> str:
    """Return the correlation ID assigned by the application middleware.

    The request ID must be returned in every response and included in
    structured logs (api-contract §2 — Correlation).
    """
    return request.state.request_id


async def _apply_rate_limit(
    *,
    subject: str,
    policy: RateLimitPolicy,
    request_id: str,
    limiter: RateLimiter,
    response: Response | None = None,
) -> None:
    """Apply one policy, failing open if the shared backend is unavailable."""
    try:
        decision = await limiter.check(subject=subject, policy=policy)
    except RateLimitBackendError:
        logger.error(
            "Rate-limit backend unavailable; request allowed",
            extra={
                "event": "rate_limit.backend_unavailable",
                "request_id": request_id,
                "rate_limit_policy": policy.name,
            },
        )
        return

    headers = {
        "X-RateLimit-Policy": policy.name,
        "X-RateLimit-Remaining": str(decision.remaining),
    }
    if response is not None:
        response.headers.update(headers)
    if decision.allowed:
        return

    retry_after = max(1, decision.retry_after_seconds)
    logger.warning(
        "API rate limit exceeded",
        extra={
            "event": "rate_limit.exceeded",
            "request_id": request_id,
            "rate_limit_policy": policy.name,
            "retry_after_seconds": retry_after,
        },
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "RATE_LIMIT_EXCEEDED",
            "message": "Too many requests. Try again later.",
            "request_id": request_id,
            "retryable": True,
        },
        headers={**headers, "Retry-After": str(retry_after)},
    )


def rate_limit_dependency(
    policy: RateLimitPolicy,
) -> Callable[..., Awaitable[None]]:
    """Create an authenticated, per-client route dependency."""

    async def enforce(
        response: Response,
        claims: TokenClaims = Depends(get_current_user),
        request_id: str = Depends(get_request_id),
        limiter: RateLimiter = Depends(get_rate_limiter),
    ) -> None:
        await _apply_rate_limit(
            subject=f"client:{claims.sub}",
            policy=policy,
            request_id=request_id,
            limiter=limiter,
            response=response,
        )

    return enforce


async def _audit_authentication_failure(
    *,
    request: Request,
    request_id: str,
    session: AsyncSession,
    reason: str,
) -> None:
    """Persist a minimized authentication-failure audit record."""
    try:
        await AuditRepository(session).log(
            actor="anonymous",
            action="AUTHENTICATION_FAILED",
            resource_type="api_request",
            resource_id=request.url.path[:128],
            outcome="UNAUTHENTICATED",
            request_id=request_id,
            details={"reason": reason},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "Failed to persist authentication audit event",
            extra={"request_id": request_id, "path": request.url.path},
        )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    request_id: str = Depends(get_request_id),
    session: AsyncSession = Depends(get_session),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenClaims:
    """Validate the Bearer token and return the decoded claims.

    Raises HTTP 401 for:
    - missing Authorization header
    - Authorization header without the ``Bearer `` scheme
    - invalid/expired token

    Never reveals implementation details in the error response
    (api-contract §8).
    """
    if credentials is None:
        client_host = request.client.host if request.client else "unknown"
        await _apply_rate_limit(
            subject=f"ip:{client_host}",
            policy=AUTH_FAILURE_POLICY,
            request_id=request_id,
            limiter=limiter,
        )
        await _audit_authentication_failure(
            request=request,
            request_id=request_id,
            session=session,
            reason="missing_credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHENTICATED",
                "message": "Authentication is required.",
                "retryable": False,
            },
        )
    try:
        return verify_token(credentials.credentials)
    except (JWTError, KeyError, ValidationError):
        client_host = request.client.host if request.client else "unknown"
        await _apply_rate_limit(
            subject=f"ip:{client_host}",
            policy=AUTH_FAILURE_POLICY,
            request_id=request_id,
            limiter=limiter,
        )
        await _audit_authentication_failure(
            request=request,
            request_id=request_id,
            session=session,
            reason="invalid_token",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHENTICATED",
                "message": "Authentication is required.",
                "retryable": False,
            },
        )
