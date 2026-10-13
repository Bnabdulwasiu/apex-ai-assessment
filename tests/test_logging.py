"""Tests for machine-parseable application logging."""

import json
import logging

from src.logging_config import JsonFormatter, startup_fields


def test_json_formatter_preserves_context_and_redacts_sensitive_fields():
    record = logging.LogRecord(
        name="src.api.routes",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Referral accepted",
        args=(),
        exc_info=None,
    )
    record.event = "referral.accepted"
    record.request_id = "request-001"
    record.referral_id = "ref-001"
    record.patient_id = "PAT-SENSITIVE"

    payload = json.loads(JsonFormatter(service="test-api").format(record))

    assert payload["service"] == "test-api"
    assert payload["event"] == "referral.accepted"
    assert payload["request_id"] == "request-001"
    assert payload["referral_id"] == "ref-001"
    assert payload["patient_id"] == "[REDACTED]"
    assert payload["timestamp"].endswith("Z")


def test_startup_fields_contain_runtime_identity_and_safe_configuration():
    fields = startup_fields(
        version="0.1.0",
        environment="test",
        worker_concurrency=4,
    )

    assert fields["event"] == "service.started"
    assert fields["version"] == "0.1.0"
    assert fields["environment"] == "test"
    assert fields["worker_concurrency"] == 4
    assert isinstance(fields["host"], str)
    assert isinstance(fields["pid"], int)
    assert "database_url" not in fields
    assert "jwt_secret_key" not in fields
