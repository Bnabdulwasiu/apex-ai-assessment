"""Abstract base interfaces and data models for external integration adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class IntegrationError(Exception):
    """Base exception for all integration failures."""

    def __init__(
        self,
        message: str,
        *,
        is_retryable: bool = False,
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.is_retryable = is_retryable
        self.status_code = status_code
        self.details = details or {}


class IntegrationRetryableError(IntegrationError):
    """Transient failures suitable for retry (e.g. 503, 504, 429, timeouts)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = 503,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            is_retryable=True,
            status_code=status_code,
            details=details,
        )


class IntegrationNonRetryableError(IntegrationError):
    """Permanent failures that should go directly to DLQ (e.g. 400, 401, 403, 404)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = 400,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            is_retryable=False,
            status_code=status_code,
            details=details,
        )


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferralContext:
    """Bounded context passed to integration adapters."""

    referral_id: str
    patient_id: str
    specialty: str
    hospital: str
    reason: str
    external_reference: str


@dataclass(frozen=True)
class EligibilityResult:
    """EHR eligibility check result."""

    eligible: bool
    eligibility_reference: Optional[str] = None
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class AIClassification:
    """AI triage classification output."""

    classification: str
    urgency: str
    confidence: float


@dataclass(frozen=True)
class CRMCase:
    """CRM Case entity."""

    case_id: str
    external_reference: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CRMUpdate:
    """CRM case update payload."""

    status: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class RoutingDecision:
    """Deterministic routing decision."""

    hospital: str
    specialty: str
    routing_reason: str


# ---------------------------------------------------------------------------
# Adapter Interfaces
# ---------------------------------------------------------------------------

class EHRAdapter(ABC):
    """Interface for Electronic Health Record (EHR) integration."""

    @abstractmethod
    async def check_eligibility(self, referral: ReferralContext) -> EligibilityResult:
        """Verify insurance and clinical eligibility for the referral."""
        pass


class AIAdapter(ABC):
    """Interface for AI-assisted clinical classification and triage."""

    @abstractmethod
    async def classify(self, referral: ReferralContext) -> AIClassification:
        """Obtain advisory triage classification and urgency."""
        pass


class CRMAdapter(ABC):
    """Interface for CRM case management."""

    @abstractmethod
    async def find_case(self, external_reference: str) -> Optional[CRMCase]:
        """Lookup an existing case by business external reference to avoid duplicates."""
        pass

    @abstractmethod
    async def create_case(self, referral: ReferralContext) -> CRMCase:
        """Create a new case in the CRM system."""
        pass

    @abstractmethod
    async def update_case(self, case_id: str, update: CRMUpdate) -> CRMCase:
        """Update an existing CRM case."""
        pass


class EventQueue(ABC):
    """Interface for asynchronous event delivery."""

    @abstractmethod
    async def publish(self, event: dict[str, Any]) -> None:
        """Publish an event to the queue."""
        pass

    @abstractmethod
    async def get(self, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Retrieve the next event, waiting up to timeout seconds."""
        pass
