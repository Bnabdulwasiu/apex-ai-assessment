"""Application configuration loaded from environment variables."""

from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration.

    Values come from environment variables or a ``.env`` file.
    The mock identity provider uses a symmetric HMAC secret for JWTs.
    In production this would be replaced by the customer's IdP and
    asymmetric key verification.
    """

    # Database
    database_url: str = (
        "postgresql+asyncpg://apex:apex_dev_password@localhost:5433/apex_referrals"
    )

    # Application
    app_env: str = "development"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # JWT / Mock IdP
    jwt_secret_key: str = "CHANGE_ME_BEFORE_ANY_DEPLOYMENT"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60

    # Outbox publisher
    outbox_poll_interval_seconds: int = 2
    outbox_batch_size: int = 50

    # Worker
    worker_poll_interval_seconds: int = 2
    worker_concurrency: int = Field(default=4, ge=1, le=8)
    outbox_claim_lease_seconds: int = 300

    # Retry policy
    retry_max_attempts: int = 3
    retry_backoff_base_ms: int = 500

    # Idempotency
    idempotency_ttl_hours: int = 24

    # Distributed API rate limiting
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_enabled: bool = True
    rate_limit_backend_timeout_seconds: float = Field(default=0.25, gt=0, le=5)
    rate_limit_referral_per_minute: int = Field(default=30, ge=1)
    rate_limit_referral_burst: int = Field(default=10, ge=1)
    rate_limit_status_per_minute: int = Field(default=120, ge=1)
    rate_limit_status_burst: int = Field(default=30, ge=1)
    rate_limit_replay_per_minute: int = Field(default=5, ge=1)
    rate_limit_replay_burst: int = Field(default=2, ge=1)
    rate_limit_auth_failure_per_minute: int = Field(default=10, ge=1)
    rate_limit_auth_failure_burst: int = Field(default=10, ge=1)

    # The same dotenv file also supplies Docker Compose interpolation values.
    # Ignore those Compose-only keys while keeping application fields validated.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
