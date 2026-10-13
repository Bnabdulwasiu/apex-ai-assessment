"""Runtime supervision tests for the standalone worker process."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import src.worker_main as worker_main
from src.config import Settings, settings


@pytest.mark.parametrize("concurrency", [0, 9])
def test_worker_concurrency_must_stay_within_safe_bounds(concurrency: int):
    with pytest.raises(ValidationError):
        Settings(worker_concurrency=concurrency, _env_file=None)


@pytest.mark.asyncio
async def test_worker_starts_configured_number_of_consumer_slots(monkeypatch):
    configured_concurrency = 3
    sentinel_worker = object()
    consumer_ids: list[str] = []
    consumer_workers: list[object] = []
    engine_disposed = False
    log_records: list[tuple[str, dict | None]] = []

    async def publish_loop(stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def processing_loop(
        stop_event: asyncio.Event,
        *,
        worker,
        worker_id: str,
    ) -> None:
        consumer_ids.append(worker_id)
        consumer_workers.append(worker)
        if len(consumer_ids) == configured_concurrency:
            stop_event.set()
        await stop_event.wait()

    async def dispose() -> None:
        nonlocal engine_disposed
        engine_disposed = True

    def log_info(message: str, *args, extra=None) -> None:
        log_records.append((message, extra))

    monkeypatch.setattr(settings, "worker_concurrency", configured_concurrency)
    monkeypatch.setattr(worker_main, "_build_referral_worker", lambda: sentinel_worker)
    monkeypatch.setattr(worker_main, "_outbox_publish_loop", publish_loop)
    monkeypatch.setattr(worker_main, "_referral_processing_loop", processing_loop)
    monkeypatch.setattr(worker_main, "_install_signal_handlers", lambda _: lambda: None)
    monkeypatch.setattr(worker_main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(worker_main, "configure_logging", lambda **_: None)
    monkeypatch.setattr(worker_main, "logger", SimpleNamespace(info=log_info))

    await worker_main._run()

    assert len(consumer_ids) == configured_concurrency
    assert len(set(consumer_ids)) == configured_concurrency
    assert consumer_workers == [sentinel_worker] * configured_concurrency
    assert engine_disposed
    assert log_records[0][0] == "Worker service started"
    startup_extra = log_records[0][1]
    assert startup_extra is not None
    assert startup_extra["event"] == "service.started"
    assert startup_extra["worker_concurrency"] == configured_concurrency
