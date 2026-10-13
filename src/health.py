"""Shared dependency readiness checks."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def database_is_ready(engine: AsyncEngine) -> bool:
    """Return whether the database accepts a minimal query."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        logger.warning(
            "Database readiness check failed",
            extra={"event": "dependency.not_ready", "dependency": "database"},
        )
        return False
    return True
