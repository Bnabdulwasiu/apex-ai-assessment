"""Audit repository.

Inserts immutable audit records per api-contract §18 / FR-15.
Audit records must NOT contain sensitive patient data.
"""

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit import AuditLog


class AuditRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Create an immutable audit record.

        Parameters
        ----------
        details
            Optional dictionary serialized to JSON.  Must not contain
            patient-level data — only operational codes and identifiers.
        """
        record = AuditLog(
            timestamp=datetime.now(timezone.utc),
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            request_id=request_id,
            details=json.dumps(details) if details else None,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_by_referral(self, referral_id: str) -> list[AuditLog]:
        """Retrieve audit records for a referral in chronological order."""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.resource_type == "referral",
                AuditLog.resource_id == referral_id,
            )
            .order_by(AuditLog.id.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
