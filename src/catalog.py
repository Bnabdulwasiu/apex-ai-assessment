"""Prototype hospital and specialty catalog used by validation and routing."""

HOSPITAL_CAPABILITIES: dict[str, frozenset[str]] = {
    "RIYADH-01": frozenset(
        {"CARDIOLOGY", "NEUROLOGY", "ORTHOPEDICS", "PEDIATRICS"}
    ),
    "RIYADH-02": frozenset(
        {"CARDIOLOGY", "ONCOLOGY", "GASTROENTEROLOGY", "GENERAL_SURGERY"}
    ),
}

SUPPORTED_HOSPITALS = frozenset(HOSPITAL_CAPABILITIES)
SUPPORTED_SPECIALTIES = frozenset(
    specialty
    for capabilities in HOSPITAL_CAPABILITIES.values()
    for specialty in capabilities
)
