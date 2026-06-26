"""Durable queue backend primitives for conversion jobs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import ConversionJob
from app.models.job_event import JobEvent


@dataclass(frozen=True)
class DurableQueueItem:
    job_id: str
    filepath: str | None
    config: dict[str, Any]
    retry_count: int
    max_retries: int
    idempotency_key: str | None


class DurableQueueBackend(Protocol):
    name: str

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        filepath: str | None,
        config: dict[str, Any],
        idempotency_key: str | None = None,
        max_retries: int = 0,
    ) -> None:
        ...

    async def recover_queued(self, session: AsyncSession) -> list[DurableQueueItem]:
        ...

    async def mark_started(self, session: AsyncSession, *, job_id: str, lease_owner: str, lease_seconds: int) -> None:
        ...

    async def mark_terminal(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        status: str,
        message: str | None = None,
    ) -> None:
        ...


class InMemoryQueueBackend:
    """Null durable backend that preserves existing in-memory behavior."""

    name = "memory"

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        filepath: str | None,
        config: dict[str, Any],
        idempotency_key: str | None = None,
        max_retries: int = 0,
    ) -> None:
        return None

    async def recover_queued(self, session: AsyncSession) -> list[DurableQueueItem]:
        return []

    async def mark_started(self, session: AsyncSession, *, job_id: str, lease_owner: str, lease_seconds: int) -> None:
        return None

    async def mark_terminal(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        status: str,
        message: str | None = None,
    ) -> None:
        return None


class SQLiteDurableQueueBackend:
    """SQLite-backed durable queue metadata and recovery scanner."""

    name = "sqlite"

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        filepath: str | None,
        config: dict[str, Any],
        idempotency_key: str | None = None,
        max_retries: int = 0,
    ) -> None:
        now = _utcnow()
        stored_config = dict(config)
        if filepath:
            stored_config.setdefault("durable_filepath", filepath)
        await session.execute(
            update(ConversionJob)
            .where(ConversionJob.id == job_id)
            .values(
                status="pending",
                queue_backend=self.name,
                queued_at=now,
                started_at=None,
                lease_owner=None,
                lease_expires_at=None,
                retry_count=0,
                max_retries=max(0, max_retries),
                idempotency_key=idempotency_key,
                config_json=json.dumps(stored_config),
            )
        )
        await append_job_event(
            session,
            job_id=job_id,
            event_type="queue.enqueued",
            status="pending",
            payload={"filepath": filepath, "idempotency_key": idempotency_key, "max_retries": max(0, max_retries)},
        )

    async def recover_queued(self, session: AsyncSession) -> list[DurableQueueItem]:
        now = _utcnow()
        stmt = (
            select(ConversionJob)
            .where(ConversionJob.queue_backend == self.name)
            .where(
                or_(
                    ConversionJob.status == "pending",
                    and_(
                        ConversionJob.status == "processing",
                        ConversionJob.lease_expires_at.is_not(None),
                        ConversionJob.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(ConversionJob.queued_at.asc(), ConversionJob.created_at.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        items: list[DurableQueueItem] = []
        for row in rows:
            config = _config(row.config_json)
            filepath = config.get("durable_filepath") or config.get("local_filepath")
            items.append(
                DurableQueueItem(
                    job_id=row.id,
                    filepath=str(filepath) if filepath else None,
                    config=config,
                    retry_count=row.retry_count,
                    max_retries=row.max_retries,
                    idempotency_key=row.idempotency_key,
                )
            )
        return items

    async def mark_started(self, session: AsyncSession, *, job_id: str, lease_owner: str, lease_seconds: int) -> None:
        now = _utcnow()
        await session.execute(
            update(ConversionJob)
            .where(ConversionJob.id == job_id)
            .values(
                status="processing",
                started_at=now,
                lease_owner=lease_owner,
                lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
            )
        )
        await append_job_event(
            session,
            job_id=job_id,
            event_type="queue.started",
            status="processing",
            payload={"lease_owner": lease_owner, "lease_seconds": max(1, lease_seconds)},
        )

    async def mark_terminal(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        status: str,
        message: str | None = None,
    ) -> None:
        await session.execute(
            update(ConversionJob)
            .where(ConversionJob.id == job_id)
            .values(
                status=status,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=_utcnow(),
                error_message=message if status == "failed" else None,
            )
        )
        await append_job_event(
            session,
            job_id=job_id,
            event_type=f"queue.{status}",
            status=status,
            message=message,
        )


async def append_job_event(
    session: AsyncSession,
    *,
    job_id: str,
    event_type: str,
    status: str | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> JobEvent:
    event = JobEvent(
        job_id=job_id,
        event_type=event_type,
        status=status,
        message=message,
        payload_json=json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
    )
    session.add(event)
    await session.flush()
    return event


def _config(config_json: str | None) -> dict[str, Any]:
    if not config_json:
        return {}
    try:
        parsed = json.loads(config_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def queue_backend_from_env() -> DurableQueueBackend | None:
    raw = os.getenv("MARKER_QUEUE_BACKEND", "").strip().lower()
    if raw in {"sqlite", "sqlite_durable", "durable"}:
        return SQLiteDurableQueueBackend()
    return None
