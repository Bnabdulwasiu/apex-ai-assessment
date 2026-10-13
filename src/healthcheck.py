"""Container health probe for non-HTTP application processes."""

import asyncio

from src.database import engine
from src.health import database_is_ready


async def _run() -> int:
    try:
        return 0 if await database_is_ready(engine) else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
