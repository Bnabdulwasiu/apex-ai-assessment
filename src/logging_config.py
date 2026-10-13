"""Shared JSON logging configuration for API and worker processes."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from typing import Any


_STANDARD_RECORD_FIELDS = frozenset(
    {*logging.makeLogRecord({}).__dict__, "asctime", "message"}
)
_CORE_PAYLOAD_FIELDS = frozenset(
    {"timestamp", "level", "logger", "service", "event", "message"}
)
_SENSITIVE_FIELDS = frozenset(
    {
        "authorization",
        "database_url",
        "jwt_secret_key",
        "password",
        "patient_id",
        "reason",
        "token",
    }
)


class JsonFormatter(logging.Formatter):
    """Render one machine-parseable JSON object per log record."""

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict[str, Any] = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "event": getattr(record, "event", "application.log"),
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if (
                key in _STANDARD_RECORD_FIELDS
                or key in _CORE_PAYLOAD_FIELDS
                or key.startswith("_")
            ):
                continue
            payload[key] = "[REDACTED]" if key.lower() in _SENSITIVE_FIELDS else value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(
            payload,
            ensure_ascii=True,
            default=str,
            separators=(",", ":"),
        )


class HealthCheckFilter(logging.Filter):
    """Drop readiness access log records produced by Docker's healthcheck.

    Uvicorn logs every HTTP request at INFO level on the
    ``uvicorn.access`` logger. The Docker healthcheck fires every
    30 s and generates pure noise — this filter silences those lines
    while leaving all other access log entries intact.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn access records embed the request line in the message,
        # e.g. '127.0.0.1:XXXXX - "GET /health/ready HTTP/1.1" 200'
        return "GET /health/ready " not in record.getMessage()


def configure_logging(*, service: str, log_level: str) -> None:
    """Configure application logs as JSON Lines on standard output."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))
    logging.basicConfig(level=level, handlers=[handler], force=True)

    # Silence healthcheck noise on the uvicorn access logger only.
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())


def startup_fields(
    *,
    version: str,
    environment: str,
    **operational_settings: Any,
) -> dict[str, Any]:
    """Return the common, non-sensitive service startup event fields."""
    return {
        "event": "service.started",
        "version": version,
        "environment": environment,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        **operational_settings,
    }
