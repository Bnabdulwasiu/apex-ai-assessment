"""Retry policy and classification for worker integration steps."""

import asyncio
from typing import Any, Optional

from src.adapters.base import IntegrationError, IntegrationNonRetryableError, IntegrationRetryableError


class RetryPolicy:
    """Configurable integration retry policy conforming to api-contract §19.
    
    Default:
    - Maximum attempts: 3
    - Backoff: 500ms → 1000ms → 2000ms
    """

    RETRYABLE_STATUS_CODES: set[int] = {429, 500, 502, 503, 504}
    NON_RETRYABLE_STATUS_CODES: set[int] = {400, 401, 403, 404, 422}

    def __init__(
        self,
        max_attempts: int = 3,
        backoff_intervals: Optional[list[float]] = None,
        apply_backoff: bool = True,
    ) -> None:
        self.max_attempts = max_attempts
        self.backoff_intervals = backoff_intervals if backoff_intervals is not None else [0.5, 1.0, 2.0]
        self.apply_backoff = apply_backoff

    def is_retryable(self, exc: Exception) -> bool:
        """Classify whether an exception is transient and suitable for retry."""
        if isinstance(exc, IntegrationNonRetryableError):
            return False

        if isinstance(exc, IntegrationRetryableError):
            return True

        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True

        if isinstance(exc, IntegrationError):
            if exc.status_code is not None:
                if exc.status_code in self.RETRYABLE_STATUS_CODES:
                    return True
                if exc.status_code in self.NON_RETRYABLE_STATUS_CODES:
                    return False
            return exc.is_retryable

        # Unknown exceptions default to non-retryable to prevent infinite poison pills
        return False

    def get_backoff(self, attempt: int) -> float:
        """Get the sleep duration for the given 1-based attempt index."""
        if not self.apply_backoff or not self.backoff_intervals:
            return 0.0
        index = min(attempt - 1, len(self.backoff_intervals) - 1)
        return self.backoff_intervals[max(0, index)]

    async def wait(self, attempt: int) -> None:
        """Wait according to the backoff schedule."""
        duration = self.get_backoff(attempt)
        if duration > 0:
            await asyncio.sleep(duration)
