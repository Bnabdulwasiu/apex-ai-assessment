"""Integration adapters package."""

from src.adapters.base import (
    AIAdapter,
    AIClassification,
    CRMAdapter,
    CRMCase,
    CRMUpdate,
    EHRAdapter,
    EligibilityResult,
    EventQueue,
    IntegrationError,
    IntegrationNonRetryableError,
    IntegrationRetryableError,
    ReferralContext,
    RoutingDecision,
)
from src.adapters.mocks import (
    InMemoryEventQueue,
    MockAIAdapter,
    MockCRMAdapter,
    MockEHRAdapter,
)

__all__ = [
    "EHRAdapter",
    "AIAdapter",
    "CRMAdapter",
    "EventQueue",
    "ReferralContext",
    "EligibilityResult",
    "AIClassification",
    "CRMCase",
    "CRMUpdate",
    "RoutingDecision",
    "IntegrationError",
    "IntegrationRetryableError",
    "IntegrationNonRetryableError",
    "MockEHRAdapter",
    "MockAIAdapter",
    "MockCRMAdapter",
    "InMemoryEventQueue",
]
