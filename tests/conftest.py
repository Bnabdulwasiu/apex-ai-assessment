"""Shared test fixtures.

Uses an in-memory SQLite database via aiosqlite so tests run without
a PostgreSQL server. The async engine and tables are created fresh for
each test session.
"""

import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth import create_mock_token
from src.database import get_session
from src.main import app
from src.models.base import Base
from src.rate_limit import get_rate_limiter
from src.services.rate_limiter import AllowAllRateLimiter


# ---------------------------------------------------------------------------
# In-memory SQLite engine (no PostgreSQL required for unit/API tests)
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# pyrefly: ignore [bad-specialization]
@pytest_asyncio.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    """Fresh session with table cleanup after each test."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()

    async with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            await connection.execute(table.delete())


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """AsyncClient wired to the FastAPI app with the test DB session."""

    async def override_get_session():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_rate_limiter] = lambda: AllowAllRateLimiter()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_riyadh01_token() -> str:
    return create_mock_token("agent_riyadh01")


@pytest.fixture
def agent_riyadh02_token() -> str:
    return create_mock_token("agent_riyadh02")


@pytest.fixture
def ops_admin_token() -> str:
    return create_mock_token("ops_admin")


@pytest.fixture
def integration_svc_token() -> str:
    return create_mock_token("integration_svc")
