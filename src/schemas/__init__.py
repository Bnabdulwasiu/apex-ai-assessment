"""Schema package — re-exports for convenient imports."""

from src.schemas.error import ErrorBody, ErrorDetail, ErrorResponse
from src.schemas.referral import (
    PatientInfo,
    ReferralCreateRequest,
    ReferralCreateResponse,
    ReferralResponse,
)

__all__ = [
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "PatientInfo",
    "ReferralCreateRequest",
    "ReferralCreateResponse",
    "ReferralResponse",
]
