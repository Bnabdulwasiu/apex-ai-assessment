"""Unit tests for authorization decisions (src/auth.can_access_referral).

Covers the P4 fix: a referral is visible to the hospital that
originally requested it AND the hospital routing actually assigned it
to, since RoutingService can legitimately cross-route a referral to a
different facility than requested (CROSS_FACILITY_CAPABILITY_TRANSFER).
"""

from src.auth import TokenClaims, can_access_referral


def _agent(hospital_scope: str) -> TokenClaims:
    return TokenClaims(
        sub=f"agent_{hospital_scope.lower()}",
        role="contact_center_agent",
        hospital_scope=hospital_scope,
    )


def test_agent_can_access_referral_in_their_own_hospital_before_routing():
    # routed_hospital is None until the Routing step runs — must not block access.
    assert can_access_referral(_agent("RIYADH-01"), "RIYADH-01", None) is True
 

def test_agent_cannot_access_other_hospitals_referral_before_routing():
    assert can_access_referral(_agent("RIYADH-02"), "RIYADH-01", None) is False


def test_originating_hospital_retains_access_after_cross_routing():
    # Requested RIYADH-01, routed elsewhere — the originating agent
    # must still be able to see their own submission.
    assert can_access_referral(_agent("RIYADH-01"), "RIYADH-01", "RIYADH-02") is True


def test_destination_hospital_gains_access_after_cross_routing():
    # This is the bug P4 fixes: previously RIYADH-02 agents had no way
    # to ever see a referral cross-routed to their own facility.
    assert can_access_referral(_agent("RIYADH-02"), "RIYADH-01", "RIYADH-02") is True


def test_uninvolved_hospital_still_denied_after_cross_routing():
    assert can_access_referral(_agent("RIYADH-03"), "RIYADH-01", "RIYADH-02") is False


def test_operations_admin_bypasses_hospital_scope_entirely():
    claims = TokenClaims(sub="ops_admin", role="operations_admin", hospital_scope=None)
    assert can_access_referral(claims, "RIYADH-01", None) is True


def test_integration_service_bypasses_hospital_scope_entirely():
    claims = TokenClaims(
        sub="integration_svc", role="integration_service", hospital_scope=None
    )
    assert can_access_referral(claims, "RIYADH-01", "RIYADH-02") is True