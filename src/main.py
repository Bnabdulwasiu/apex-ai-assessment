"""FastAPI application factory and entry point."""

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.routes import router as referral_router
from src.config import settings
from src.database import engine
from src.health import database_is_ready
from src.logging_config import configure_logging, startup_fields
from src.rate_limit import close_rate_limit_backend

# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Release process-owned resources after the server drains requests."""
    configure_logging(service="apex-referral-api", log_level=settings.log_level)
    logger.info(
        "API service started",
        extra=startup_fields(
            version=settings.app_version,
            environment=settings.app_env,
            api_version="v1",
            docs_url="/docs",
            rate_limit_enabled=settings.rate_limit_enabled,
        ),
    )
    try:
        yield
    finally:
        logger.info(
            "API shutdown started; disposing database connections",
            extra={"event": "service.stopping"},
        )
        await close_rate_limit_backend()
        await engine.dispose()
        logger.info("API shutdown complete", extra={"event": "service.stopped"})


app = FastAPI(
    title="Apex AI Arabia — Referral Integration Service",
    version=settings.app_version,
    description=(
        "Backend integration layer for AI-assisted referral and appointment "
        "workflows for a Saudi private healthcare group."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Versioned API router
app.include_router(referral_router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# Request correlation
# ---------------------------------------------------------------------------

def _request_id(request: Request) -> str:
    """Return the request's single correlation identifier."""
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        return request_id
    return request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex}"


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach one correlation ID to every request and response."""
    supplied_request_id = request.headers.get("X-Request-ID")
    request_id = (
        supplied_request_id
        if supplied_request_id and REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        else f"req-{uuid.uuid4().hex}"
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------

def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the public error envelope defined by the API contract."""
    request_id = _request_id(request)
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "retryable": retryable,
    }
    if details is not None:
        error["details"] = details

    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=response_headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalize route and authentication errors into the public envelope."""
    detail = exc.detail
    if isinstance(detail, dict):
        source = detail.get("error", detail)
        code = str(source.get("code", "HTTP_ERROR"))
        message = str(source.get("message", "The request could not be completed."))
        retryable = bool(source.get("retryable", False))
        details = source.get("details")
    else:
        code = f"HTTP_{exc.status_code}"
        message = str(detail)
        retryable = exc.status_code >= 500
        details = None

    return _error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        retryable=retryable,
        details=details,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return safe field-level details for malformed requests."""
    details: list[dict[str, str]] = []
    for error in exc.errors():
        location = [
            str(part)
            for part in error.get("loc", ())
            if part not in {"body", "query", "path", "header"}
        ]
        details.append(
            {
                "field": ".".join(location) or "request",
                "reason": str(error.get("msg", "Invalid value.")),
            }
        )

    return _error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=details,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all — never expose stack traces or internal details."""
    logger.exception(
        "Unhandled exception",
        extra={"path": request.url.path, "request_id": _request_id(request)},
    )
    return _error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
        retryable=True,
    )


# ---------------------------------------------------------------------------
# Health checks (unauthenticated)
# ---------------------------------------------------------------------------

@app.get("/health/live", tags=["ops"], include_in_schema=False)
async def health_live() -> dict[str, str]:
    """Report that the API process is alive without checking dependencies."""
    return {"status": "alive"}


@app.get(
    "/health",
    tags=["ops"],
    include_in_schema=False,
    response_model=None,
)
@app.get(
    "/health/ready",
    tags=["ops"],
    include_in_schema=False,
    response_model=None,
)
async def health_ready() -> dict[str, str] | JSONResponse:
    """Report readiness only when the database accepts a query."""
    if not await database_is_ready(engine):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "dependency": "database"},
        )
    return {"status": "ready"}
