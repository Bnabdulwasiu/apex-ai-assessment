"""Health and configuration tests used by container orchestration."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as api_main
from src.config import Settings
from src.health import database_is_ready


def test_settings_ignore_compose_only_values():
    loaded = Settings(
        app_env="test",
        postgres_port=5433,
        docker_app_env="production",
        _env_file=None,
    )

    assert loaded.app_env == "test"


@pytest.mark.asyncio
async def test_database_readiness_executes_probe():
    executed: list[str] = []

    class Connection:
        async def execute(self, statement) -> None:
            executed.append(str(statement))

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return None

    fake_engine = SimpleNamespace(connect=lambda: ConnectionContext())

    assert await database_is_ready(fake_engine) is True
    assert executed == ["SELECT 1"]


@pytest.mark.asyncio
async def test_readiness_returns_503_when_database_is_unavailable(monkeypatch):
    async def unavailable(_):
        return False

    monkeypatch.setattr(api_main, "database_is_ready", unavailable)
    async with AsyncClient(
        transport=ASGITransport(app=api_main.app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "dependency": "database"}


@pytest.mark.asyncio
async def test_liveness_does_not_depend_on_database():
    async with AsyncClient(
        transport=ASGITransport(app=api_main.app), base_url="http://test"
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
