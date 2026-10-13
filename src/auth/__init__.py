"""Authentication and authorization.

Implements the mock identity provider described in
docs/architecture.md §11 and docs/requirements.md FR-03 / FR-04.

The mock IdP uses symmetric HS256 JWTs.  In production this would be
replaced by the customer's IdP with asymmetric key verification
(JWKS endpoint).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from pydantic import BaseModel

from src.config import settings


class TokenClaims(BaseModel):
    """Decoded JWT claims used for authorization decisions.

    Attributes
    ----------
    sub : str
        Subject / client identity (e.g. ``agent_001``).
    role : str
        One of ``contact_center_agent``, ``integration_service``,
        ``operations_admin``.
    hospital_scope : str | None
        Hospital the subject is authorized to access.
        ``None`` for roles that are not hospital-scoped
        (e.g. ``operations_admin``).
    """

    sub: str
    role: str
    hospital_scope: str | None = None


# ---------------------------------------------------------------------------
# Mock identity provider — token generation
# ---------------------------------------------------------------------------

# Known test users (synthetic — no real credentials)
MOCK_USERS: dict[str, dict[str, Any]] = {
    "agent_riyadh01": {
        "sub": "agent_riyadh01",
        "role": "contact_center_agent",
        "hospital_scope": "RIYADH-01",
    },
    "agent_riyadh02": {
        "sub": "agent_riyadh02",
        "role": "contact_center_agent",
        "hospital_scope": "RIYADH-02",
    },
    "integration_svc": {
        "sub": "integration_svc",
        "role": "integration_service",
        "hospital_scope": None,
    },
    "ops_admin": {
        "sub": "ops_admin",
        "role": "operations_admin",
        "hospital_scope": None,
    },
}


def create_mock_token(user_key: str) -> str:
    """Generate a signed JWT for a mock user.

    This is used by tests and demo scripts.  Production tokens would
    come from the customer's identity provider.
    """
    if user_key not in MOCK_USERS:
        raise ValueError(f"Unknown mock user: {user_key}")

    claims = {
        **MOCK_USERS[user_key],
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
            + timedelta(minutes=settings.jwt_expiry_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

def verify_token(token: str) -> TokenClaims:
    """Decode and validate a bearer token.

    Raises
    ------
    JWTError
        If the token is missing, expired, or has an invalid signature.
    """
    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    return TokenClaims(
        sub=payload["sub"],
        role=payload["role"],
        hospital_scope=payload.get("hospital_scope"),
    )


# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

def can_access_referral(
    claims: TokenClaims,
    preferred_hospital: str,
    routed_hospital: str | None = None,
) -> bool:
    """Check whether the authenticated principal may access a referral.

    * ``operations_admin`` and ``integration_service`` have cross-hospital
      access for operational support / service-to-service calls.
    * ``contact_center_agent`` is restricted to their ``hospital_scope``,
      checked against EITHER the hospital the referral was originally
      requested for (``preferred_hospital``, always set) OR the hospital
      routing actually assigned it to (``routed_hospital``, ``None``
      until the Routing step runs).

      Both are checked, not just one, because routing can legitimately
      cross-route a referral to a different facility than requested
      (RoutingService's CROSS_FACILITY_CAPABILITY_TRANSFER rule).
      Checking only ``preferred_hospital`` would leave the destination
      hospital's own agents permanently unable to see a referral now
      actually assigned to them. Checking only ``routed_hospital``
      would lock the originating agent out of their own referral for
      as long as it sits at RECEIVED/PROCESSING, before routing has run
      and this field is still NULL.
    """
    if claims.role in ("operations_admin", "integration_service"):
        return True
    if claims.hospital_scope == preferred_hospital:
        return True
    return routed_hospital is not None and claims.hospital_scope == routed_hospital

def require_role(claims: TokenClaims, *allowed_roles: str) -> bool:
    """Return ``True`` if the principal's role is in *allowed_roles*."""
    return claims.role in allowed_roles
