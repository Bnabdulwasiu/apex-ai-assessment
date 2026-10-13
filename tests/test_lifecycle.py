"""Process lifecycle tests for graceful API and worker shutdown."""

import asyncio
from types import SimpleNamespace

import pytest

import src.main as api_main
import src.worker_main as worker_main


@pytest.mark.asyncio
async def test_api_lifespan_disposes_database_engine(monkeypatch):
    disposed = False
    log_records: list[tuple[str, dict | None]] = []

    async def dispose() -> None:
        nonlocal disposed
        disposed = True

    def log_info(message: str, *args, extra=None) -> None:
        log_records.append((message, extra))

    monkeypatch.setattr(api_main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(api_main, "configure_logging", lambda **_: None)
    monkeypatch.setattr(api_main, "logger", SimpleNamespace(info=log_info))

    async with api_main.app.router.lifespan_context(api_main.app):
        assert not disposed

    assert disposed
    assert log_records[0][0] == "API service started"
    startup_extra = log_records[0][1]
    assert startup_extra is not None
    assert startup_extra["event"] == "service.started"
    assert startup_extra["version"] == api_main.settings.app_version
    assert startup_extra["environment"] == api_main.settings.app_env


@pytest.mark.asyncio
async def test_worker_cancellation_drains_in_flight_work(monkeypatch):
    processing_started = asyncio.Event()
    allow_processing_to_finish = asyncio.Event()
    processing_finished = False
    engine_disposed = False
    handlers_restored = False

    async def publish_loop(stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def processing_loop(
        _: asyncio.Event,
        *,
        worker,
        worker_id: str,
    ) -> None:
        nonlocal processing_finished
        processing_started.set()
        await allow_processing_to_finish.wait()
        processing_finished = True

    async def dispose() -> None:
        nonlocal engine_disposed
        engine_disposed = True

    def install_handlers(_: asyncio.Event):
        def restore() -> None:
            nonlocal handlers_restored
            handlers_restored = True

        return restore

    monkeypatch.setattr(worker_main, "_outbox_publish_loop", publish_loop)
    monkeypatch.setattr(worker_main, "_referral_processing_loop", processing_loop)
    monkeypatch.setattr(worker_main, "_install_signal_handlers", install_handlers)
    monkeypatch.setattr(worker_main, "engine", SimpleNamespace(dispose=dispose))

    run_task = asyncio.create_task(worker_main._run())
    await processing_started.wait()

    run_task.cancel()
    await asyncio.sleep(0)

    assert not processing_finished
    assert not run_task.done()

    allow_processing_to_finish.set()
    await run_task

    assert processing_finished
    assert handlers_restored
    assert engine_disposed
