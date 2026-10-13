"""Idempotency repository.

Implements database-level uniqueness enforcement for
(client_id, idempotency_key) as required by FR-05.

Application-level existence checks alone are insufficient
because concurrent requests could pass the check simultaneously.
The DB unique constraint is the final safety net.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.idempotency import IdempotencyRecord
from src.config import settings


class IdempotencyConflict(Exception):
    """Raised when the same key is reused with a different request hash."""

    def __init__(self, existing_referral_id: str) -> None:
        self.existing_referral_id = existing_referral_id
        super().__init__(f"Idempotency conflict for referral {existing_referral_id}")


class IdempotencyRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(
        self,
        client_id: str,
        idempotency_key: str,
        request_hash: str,
        referral_id: str,
    ) -> IdempotencyRecord | None:
        """Atomically reserve a key or return its active existing record.

        Returns
        -------
        IdempotencyRecord | None
            ``None`` if a *new* record was successfully inserted
            (first request).  Otherwise returns the *existing* record
            so the caller can decide whether to return the cached
            result or raise a conflict.

        Raises
        ------
        IdempotencyConflict
            If the key exists but the request hash differs
            (api-contract §7 — 409 Conflict).
        """
        now = datetime.now(timezone.utc)
        await self._session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.client_id == client_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
                IdempotencyRecord.expires_at <= now,
            )
        )

        values = {
            "client_id": client_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "referral_id": referral_id,
            "created_at": now,
            "expires_at": now + timedelta(hours=settings.idempotency_ttl_hours),
        }
        dialect_name = self._session.get_bind().dialect.name

        if dialect_name == "postgresql":
            statement = (
                postgresql_insert(IdempotencyRecord)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["client_id", "idempotency_key"]
                )
                .returning(IdempotencyRecord.id)
            )
            result = await self._session.execute(statement)
            created = result.scalar_one_or_none() is not None
        elif dialect_name == "sqlite":
            statement = (
                sqlite_insert(IdempotencyRecord)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["client_id", "idempotency_key"]
                )
                .returning(IdempotencyRecord.id)
            )
            result = await self._session.execute(statement)
            created = result.scalar_one_or_none() is not None
        else:
            created = await self._reserve_with_savepoint(values)

        if created:
            return None

        existing = await self._find(client_id, idempotency_key)
        if existing is None:
            raise RuntimeError("Idempotency reservation conflict could not be resolved")
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(existing.referral_id)
        return existing

    async def _reserve_with_savepoint(self, values: dict) -> bool:
        """Fallback for database engines without native conflict handling."""
        try:
            async with self._session.begin_nested():
                self._session.add(IdempotencyRecord(**values))
                await self._session.flush()
            return True
        except IntegrityError:
            return False

    async def _find(
        self, client_id: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.client_id == client_id,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
