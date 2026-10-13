"""Pydantic schemas for the referral API.

Request/response shapes follow docs/api-contract.md §3–§6.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.catalog import SUPPORTED_HOSPITALS, SUPPORTED_SPECIALTIES


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class PatientInfo(BaseModel):
    """Nested patient block in a referral request (api-contract §3)."""

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        examples=["PAT-SYN-001"],
        description="Synthetic patient identifier. Classified as sensitive.",
    )

    @field_validator("id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ReferralCreateRequest(BaseModel):
    """POST /api/v1/referrals — request body (api-contract §3)."""

    external_reference: str = Field(
        ...,
        min_length=1,
        max_length=128,
        examples=["CONTACT-REF-10001"],
        description="Client-supplied business reference.",
    )
    patient: PatientInfo
    specialty: str = Field(
        ...,
        min_length=1,
        max_length=64,
        examples=["cardiology"],
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        examples=["Routine cardiology referral"],
        description="Referral reason. Classified as sensitive.",
    )
    preferred_hospital: str = Field(
        ...,
        min_length=1,
        max_length=64,
        examples=["RIYADH-01"],
    )

    @field_validator("external_reference", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("specialty", mode="before")
    @classmethod
    def validate_specialty(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().upper()
        if normalized not in SUPPORTED_SPECIALTIES:
            supported = ", ".join(
                sorted(specialty.lower() for specialty in SUPPORTED_SPECIALTIES)
            )
            raise ValueError(f"Unsupported specialty. Supported values: {supported}.")
        return normalized.lower()

    @field_validator("preferred_hospital", mode="before")
    @classmethod
    def validate_hospital(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().upper()
        if normalized not in SUPPORTED_HOSPITALS:
            supported = ", ".join(sorted(SUPPORTED_HOSPITALS))
            raise ValueError(f"Unsupported hospital. Supported values: {supported}.")
        return normalized


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class ReferralResponse(BaseModel):
    """Referral representation returned by POST and GET endpoints.

    The response deliberately omits sensitive patient fields
    (api-contract §5 — "The API does not expose the complete
    clinical/referral payload through this status endpoint.").
    """

    referral_id: str
    external_reference: str
    status: str
    hospital: str | None = None
    crm_case_id: str | None = None
    updated_at: datetime | None = None
    request_id: str


class ReferralCreateResponse(BaseModel):
    """202 Accepted response after referral intake (api-contract §4)."""

    referral_id: str
    status: str
    crm_case_id: str | None = None
    request_id: str
