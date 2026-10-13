"""Structured error schemas.

All API errors follow the model from docs/api-contract.md §12.
"""

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Individual field-level validation error."""

    field: str
    reason: str


class ErrorBody(BaseModel):
    """Standard error payload returned by every error response.

    ``retryable`` tells the client whether the same request might
    succeed later (e.g. 503) or will always fail (e.g. 422).

    ``details`` is used only for validation errors (api-contract §10).
    """

    code: str = Field(..., examples=["VALIDATION_ERROR"])
    message: str = Field(..., examples=["Request validation failed."])
    request_id: str = Field(..., examples=["request-abc123"])
    retryable: bool = False
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    """Top-level error envelope."""

    error: ErrorBody
