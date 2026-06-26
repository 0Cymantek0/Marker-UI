"""Tests for SQLite durable queue metadata and recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.task_manager as task_manager_module
from app.database import Base
from app.models.audit import AuditEvent  # noqa: F401
from app.models.job import ConversionJob
from app.models.job_event import JobEvent
from app.models.settings import Setting  # noqa: F401
from app.services.queue_backends import SQLiteDurableQueueBackend, queue_backend_from_env
from app.services.task_manager import TaskManager


@pytest.mark.asyncio
async def test_sqlite_durable_queue_recovers_queued_job_after_restart_simulation(
    tmp_path: Path,
):
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    job_id = "11111111-1111-4111-8111-111111111111"

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="queued.tsv",
                original_name="queued.tsv",
                status="pending",
                input_format="tsv",
                output_format="markdown",
                config_json="{}",
            )
        )
        await session.flush()
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(tmp_path / "queued.tsv"),
            config={"output_format": "markdown", "original_name": "queued.tsv"},
            idempotency_key="idem-1",
            max_retries=2,
        )
        await session.commit()

    restarted_queue = SQLiteDurableQueueBackend()
    async with session_factory() as session:
        recovered = await restarted_queue.recover_queued(session)

    assert len(recovered) == 1
    item = recovered[0]
    assert item.job_id == job_id
    assert item.filepath == str(tmp_path / "queued.tsv")
    assert item.config["output_format"] == "markdown"
    assert item.retry_count == 0
    assert item.max_retries == 2
    assert item.idempotency_key == "idem-1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_durable_queue_recovers_expired_processing_lease(tmp_path: Path):
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    job_id = "22222222-2222-4222-8222-222222222222"

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="leased.tsv",
                original_name="leased.tsv",
                status="processing",
                input_format="tsv",
                output_format="markdown",
                config_json=json.dumps({"durable_filepath": str(tmp_path / "leased.tsv")}),
                queue_backend="sqlite",
                queued_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                started_at=datetime.now(timezone.utc) - timedelta(minutes=4),
                lease_owner="dead-worker",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                retry_count=1,
                max_retries=3,
            )
        )
        await session.commit()

    async with session_factory() as session:
        recovered = await queue.recover_queued(session)

    assert [item.job_id for item in recovered] == [job_id]
    assert recovered[0].retry_count == 1
    assert recovered[0].filepath == str(tmp_path / "leased.tsv")

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_durable_queue_records_start_terminal_and_event_log(tmp_path: Path):
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    job_id = "33333333-3333-4333-8333-333333333333"

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="events.tsv",
                original_name="events.tsv",
                status="pending",
                input_format="tsv",
                output_format="markdown",
            )
        )
        await session.flush()
        await queue.enqueue(session, job_id=job_id, filepath="/tmp/events.tsv", config={}, max_retries=1)
        await queue.mark_started(session, job_id=job_id, lease_owner="worker-a", lease_seconds=60)
        await queue.mark_terminal(session, job_id=job_id, status="failed", message="boom")
        await session.commit()

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        assert job.status == "failed"
        assert job.lease_owner is None
        assert job.lease_expires_at is None
        assert job.error_message == "boom"
        events = (
            await session.execute(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at))
        ).scalars().all()

    assert [event.event_type for event in events] == [
        "queue.enqueued",
        "queue.started",
        "queue.failed",
    ]
    assert json.loads(events[0].payload_json)["max_retries"] == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_manager_recover_durable_jobs_uses_configured_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)
    queue = SQLiteDurableQueueBackend()
    job_id = "44444444-4444-4444-8444-444444444444"

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="tm.tsv",
                original_name="tm.tsv",
                status="pending",
                input_format="tsv",
                output_format="markdown",
            )
        )
        await session.flush()
        await queue.enqueue(session, job_id=job_id, filepath="/tmp/tm.tsv", config={"output_format": "markdown"})
        await session.commit()

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        recovered = await manager.recover_durable_jobs()
    finally:
        manager.shutdown(wait=False)

    assert [item.job_id for item in recovered] == [job_id]
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_manager_enqueue_durable_job_uses_caller_transaction(tmp_path: Path):
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    manager = TaskManager(max_workers=1, durable_queue=queue)
    job_id = "55555555-5555-4555-8555-555555555555"

    try:
        async with session_factory() as session:
            session.add(
                ConversionJob(
                    id=job_id,
                    filename="same-session.tsv",
                    original_name="same-session.tsv",
                    status="pending",
                    input_format="tsv",
                    output_format="markdown",
                )
            )
            await session.flush()
            persisted = await manager.enqueue_durable_job(
                session,
                job_id=job_id,
                filepath="/tmp/same-session.tsv",
                config={"output_format": "markdown"},
                idempotency_key="same-session",
                max_retries=4,
            )
            assert persisted is True
            await session.commit()

        async with session_factory() as session:
            job = await session.get(ConversionJob, job_id)
            assert job.queue_backend == "sqlite"
            assert job.idempotency_key == "same-session"
            assert job.max_retries == 4
            events = (
                await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))
            ).scalars().all()
            assert [event.event_type for event in events] == ["queue.enqueued"]
    finally:
        manager.shutdown(wait=False)
        await engine.dispose()


def test_queue_backend_from_env_selects_sqlite(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKER_QUEUE_BACKEND", "sqlite")
    assert isinstance(queue_backend_from_env(), SQLiteDurableQueueBackend)

    monkeypatch.setenv("MARKER_QUEUE_BACKEND", "memory")
    assert queue_backend_from_env() is None


async def _session_factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'durable.db'}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
