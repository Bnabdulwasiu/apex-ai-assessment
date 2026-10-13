"""Concurrency coverage for the database-backed idempotency reservation."""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.idempotency import IdempotencyRecord
from src.models.outbox import OutboxEvent
from src.models.referral import Referral
from src.schemas.referral import ReferralCreateRequest
from src.services.referral_service import create_referral


@pytest.mark.asyncio
async def test_concurrent_duplicate_submissions_create_one_workflow():
    configured_url = os.getenv("IDEMPOTENCY_TEST_DATABASE_URL")
    database_file: Path | None = None
    if configured_url:
        engine = create_async_engine(configured_url)
    else:
        database_file = Path(".pytest_cache") / f"idempotency-{uuid.uuid4().hex}.db"
        database_file.parent.mkdir(exist_ok=True)
        database_path = database_file.resolve().as_posix()
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            connect_args={"timeout": 30},
        )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    payload = ReferralCreateRequest.model_validate(
        {
            "external_reference": "CONCURRENT-REF-001",
            "patient": {"id": "PAT-SYN-CONCURRENT"},
            "specialty": "cardiology",
            "reason": "Concurrent submission test",
            "preferred_hospital": "RIYADH-01",
        }
    )
    start = asyncio.Event()

    async def submit(request_id: str) -> tuple[str, bool]:
        await start.wait()
        async with factory() as session:
            referral, is_new = await create_referral(
                session=session,
                payload=payload,
                client_id="concurrent-client",
                idempotency_key="concurrent-key-001",
                correlation_id=request_id,
            )
            await session.commit()
            return referral.referral_id, is_new

    tasks = [
        asyncio.create_task(submit("request-concurrent-1")),
        asyncio.create_task(submit("request-concurrent-2")),
    ]
    start.set()
    results = await asyncio.gather(*tasks)

    assert results[0][0] == results[1][0]
    assert sorted(is_new for _, is_new in results) == [False, True]

    async with factory() as session:
        referral_count = await session.scalar(select(func.count()).select_from(Referral))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
        idempotency_count = await session.scalar(
            select(func.count()).select_from(IdempotencyRecord)
        )

    assert referral_count == 1
    assert outbox_count == 1
    assert idempotency_count == 1
    await engine.dispose()
    if database_file is not None:
        database_file.unlink(missing_ok=True)
