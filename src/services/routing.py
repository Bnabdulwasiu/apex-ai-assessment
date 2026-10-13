"""Deterministic routing service for referral destination matching."""

from src.adapters.base import AIClassification, ReferralContext, RoutingDecision
from src.catalog import HOSPITAL_CAPABILITIES


class RoutingService:
    """Configurable deterministic routing engine.
    
    Evaluates referral parameters and advisory AI classification against
    operational rules to produce an auditable RoutingDecision.
    """

    def __init__(
        self,
        hospital_capabilities: dict[str, set[str] | frozenset[str]] | None = None,
    ) -> None:
        # Default capabilities map: which hospitals support which specialties
        self.capabilities = hospital_capabilities or HOSPITAL_CAPABILITIES

    def route(
        self,
        referral: ReferralContext,
        ai_result: AIClassification,
    ) -> RoutingDecision:
        """Deterministically determine the destination hospital and specialty."""
        requested_hospital = referral.hospital.upper()
        specialty = ai_result.classification.upper() if ai_result.classification else referral.specialty.upper()

        # Rule 1: Requested hospital has capability for the specialty
        if requested_hospital in self.capabilities and specialty in self.capabilities[requested_hospital]:
            return RoutingDecision(
                hospital=requested_hospital,
                specialty=specialty,
                routing_reason="SPECIALTY_AND_HOSPITAL_RULE",
            )

        # Rule 2: Fallback to another facility within group that has the capability
        for hospital, specialties in self.capabilities.items():
            if specialty in specialties:
                return RoutingDecision(
                    hospital=hospital,
                    specialty=specialty,
                    routing_reason="CROSS_FACILITY_CAPABILITY_TRANSFER",
                )

        # Rule 3: Default fallback to intake facility with manual review flag
        return RoutingDecision(
            hospital=requested_hospital,
            specialty=specialty,
            routing_reason="DEFAULT_INTAKE_FACILITY_MANUAL_REVIEW",
        )
