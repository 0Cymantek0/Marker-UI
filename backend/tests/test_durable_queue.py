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
    """Recovery flows through the configured durable queue and resubmits."""
    from unittest.mock import MagicMock

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)
    queue = SQLiteDurableQueueBackend()
    job_id = "44444444-4444-4444-8444-444444444444"
    src_file = tmp_path / "tm.tsv"
    src_file.write_text("col\tval\n", encoding="utf-8")

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
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(src_file),
            config={"output_format": "markdown", "original_name": "tm.tsv"},
        )
        await session.commit()

    fake_service = MagicMock()
    fake_service.convert_file.return_value = {
        "text": "# recovered",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data", "label": "Text"}},
    }

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        recovered = await manager.recover_durable_jobs(fake_service)
        for _ in range(100):
            if manager.get_status(job_id)["status"] in {"completed", "failed"}:
                break
            import time as _time
            _time.sleep(0.05)
    finally:
        manager.shutdown(wait=False)

    assert recovered == [job_id]
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


# ---------------------------------------------------------------------------
# A1: Lease lifecycle must be wired into the live submit -> run -> finalize path
#
# Today the durable lease columns (lease_owner, lease_expires_at, started_at)
# are only ever written by SQLiteDurableQueueBackend.mark_started, and that
# method has ZERO production callers. The live run path uses raw UPDATE
# statements that bypass the lease lifecycle entirely. As a result:
#   - recover_queued's expired-lease branch never matches anything real
#   - a crashed processing job has no lease, so it can never be detected
#     as "stuck" and recovered
#
# Fix: TaskManager must call mark_started when dispatching a durable job and
# mark_terminal when finalizing/failing it, so leases reflect reality.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_job_sets_lease_when_work_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """When a durable job is dispatched, mark_started must run (lease set)."""
    from unittest.mock import MagicMock

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    queue = SQLiteDurableQueueBackend()
    job_id = "a1aaaaaa-1111-4111-8111-111111111111"
    src_file = tmp_path / "lease-start.txt"
    src_file.write_text("hello", encoding="utf-8")

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="lease-start.txt",
                original_name="lease-start.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
            )
        )
        await session.flush()
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(src_file),
            config={"output_format": "markdown", "original_name": "lease-start.txt"},
            max_retries=1,
        )
        await session.commit()

    # Instant converter so the worker finishes within the test window.
    fake_service = MagicMock()
    fake_service.convert_file.return_value = {
        "text": "# hello",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data", "label": "Text"}},
    }

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        manager.submit_job(job_id, str(src_file), {"output_format": "markdown"}, fake_service)
        # Wait for the worker to reach terminal state.
        for _ in range(100):
            if manager.get_status(job_id)["status"] in {"completed", "failed"}:
                break
            import time as _time
            _time.sleep(0.05)
    finally:
        manager.shutdown(wait=False)

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        # The lease must have been observed as set at some point. We assert the
        # terminal state cleared it (mark_terminal ran). The most direct proof
        # that mark_started ran is the event log: a queue.started event must
        # exist between queue.enqueued and queue.completed.
        events = (
            await session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
            )
        ).scalars().all()
    event_types = [e.event_type for e in events]

    assert "queue.started" in event_types, (
        f"mark_started was never called during submit; events were {event_types}"
    )
    assert job.lease_owner is None, "lease must be cleared at terminal state"
    assert job.lease_expires_at is None, "lease must be cleared at terminal state"
    assert job.status == "completed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_durable_job_clears_lease_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """When a durable job fails, mark_terminal must clear the lease."""
    from unittest.mock import MagicMock

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    queue = SQLiteDurableQueueBackend()
    job_id = "a1bbbccc-2222-4222-8222-222222222222"
    src_file = tmp_path / "lease-fail.txt"
    src_file.write_text("boom", encoding="utf-8")

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="lease-fail.txt",
                original_name="lease-fail.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
            )
        )
        await session.flush()
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(src_file),
            config={"output_format": "markdown", "original_name": "lease-fail.txt"},
            max_retries=1,
        )
        await session.commit()

    fake_service = MagicMock()
    fake_service.convert_file.side_effect = RuntimeError("converter exploded")

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        manager.submit_job(job_id, str(src_file), {"output_format": "markdown"}, fake_service)
        for _ in range(100):
            if manager.get_status(job_id)["status"] in {"completed", "failed"}:
                break
            import time as _time
            _time.sleep(0.05)
    finally:
        manager.shutdown(wait=False)

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        events = (
            await session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
            )
        ).scalars().all()
    event_types = [e.event_type for e in events]

    assert "queue.started" in event_types, (
        f"mark_started was never called; events were {event_types}"
    )
    assert "queue.failed" in event_types, (
        f"mark_terminal(failed) was never called; events were {event_types}"
    )
    assert job.status == "failed"
    assert job.lease_owner is None
    assert job.lease_expires_at is None

    await engine.dispose()


# ---------------------------------------------------------------------------
# A2: recover_durable_jobs must actually RECOVER — not just list
#
# Today recover_durable_jobs() only RETURNS a list of recoverable items. It
# does not resubmit them, check the retry budget, verify the source file
# still exists, or mark ineligible jobs failed. The desired behavior:
#   - skip jobs at/over their retry budget and mark them failed
#   - skip jobs whose source file is gone and mark them failed
#   - re-enqueue eligible jobs (within budget, file present) and run them
#   - emit a "queue.recovered" event to distinguish recovery from submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_durable_jobs_resubmits_pending_job_within_retry_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A pending job within its retry budget whose source file exists should be
    re-enqueued by recover_durable_jobs and run to completion."""
    from unittest.mock import MagicMock
    import time as _time

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    queue = SQLiteDurableQueueBackend()
    job_id = "a2aaaaaa-3333-4333-8333-333333333333"
    src_file = tmp_path / "recover.txt"
    src_file.write_text("recover me", encoding="utf-8")

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="recover.txt",
                original_name="recover.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
                queue_backend="sqlite",
                retry_count=0,
                max_retries=3,
                config_json=json.dumps(
                    {"durable_filepath": str(src_file), "output_format": "markdown"}
                ),
            )
        )
        await session.flush()
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(src_file),
            config={"output_format": "markdown", "original_name": "recover.txt"},
            max_retries=3,
        )
        await session.commit()

    fake_service = MagicMock()
    fake_service.convert_file.return_value = {
        "text": "# recovered",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data", "label": "Text"}},
    }

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        await manager.recover_durable_jobs(fake_service)
        # Poll until the worker reaches a terminal state.
        for _ in range(100):
            status = manager.get_status(job_id)
            if status["status"] in {"completed", "failed"}:
                break
            _time.sleep(0.05)
    finally:
        manager.shutdown(wait=False)

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        events = (
            await session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
            )
        ).scalars().all()
    event_types = [e.event_type for e in events]

    assert job.status == "completed"
    assert job.result_text == "# recovered"
    assert "queue.recovered" in event_types, (
        f"recovery event missing; events were {event_types}"
    )

    await engine.dispose()


@pytest.mark.asyncio
async def test_recover_durable_jobs_skips_job_over_retry_budget_and_marks_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A job that has exhausted its retry budget must NOT be resubmitted; it
    should be marked failed with a retry-related message."""
    from unittest.mock import MagicMock

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    queue = SQLiteDurableQueueBackend()
    job_id = "a2bbbbbb-4444-4444-8444-444444444444"
    src_file = tmp_path / "over-budget.txt"
    src_file.write_text("over budget", encoding="utf-8")

    # Simulate a job that already exhausted its retry budget in a prior session
    # and was left pending when the process crashed. We do NOT call enqueue()
    # here because enqueue() resets retry_count=0; we want retry_count=3 to
    # reflect a real post-crash state that recovery must reject.
    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="over-budget.txt",
                original_name="over-budget.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
                queue_backend="sqlite",
                retry_count=3,
                max_retries=3,
                config_json=json.dumps(
                    {"durable_filepath": str(src_file), "output_format": "markdown"}
                ),
            )
        )
        await session.commit()

    fake_service = MagicMock()
    fake_service.convert_file.return_value = {
        "text": "# should-not-run",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data", "label": "Text"}},
    }

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        await manager.recover_durable_jobs(fake_service)
    finally:
        manager.shutdown(wait=False)

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        events = (
            await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))
        ).scalars().all()
    event_types = [e.event_type for e in events]

    assert job.status == "failed"
    assert job.error_message is not None
    assert "retry" in job.error_message.lower(), (
        f"error_message should mention retry; got {job.error_message!r}"
    )
    assert "queue.recovered" not in event_types, (
        f"over-budget job must not be recovered; events were {event_types}"
    )
    assert fake_service.convert_file.called is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_recover_durable_jobs_marks_missing_source_file_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A recoverable job whose source file no longer exists must be marked
    failed with a source-file message and must not be resubmitted."""
    from unittest.mock import MagicMock

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    queue = SQLiteDurableQueueBackend()
    job_id = "a2cccccc-5555-4555-8555-555555555555"
    missing_file = tmp_path / "does-not-exist.txt"

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="does-not-exist.txt",
                original_name="does-not-exist.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
                queue_backend="sqlite",
                retry_count=0,
                max_retries=3,
                config_json=json.dumps(
                    {"durable_filepath": str(missing_file), "output_format": "markdown"}
                ),
            )
        )
        await session.flush()
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(missing_file),
            config={"output_format": "markdown", "original_name": "does-not-exist.txt"},
            max_retries=3,
        )
        await session.commit()

    fake_service = MagicMock()
    fake_service.convert_file.return_value = {
        "text": "# should-not-run",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data", "label": "Text"}},
    }

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        await manager.recover_durable_jobs(fake_service)
    finally:
        manager.shutdown(wait=False)

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        events = (
            await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))
        ).scalars().all()
    event_types = [e.event_type for e in events]

    assert job.status == "failed"
    assert job.error_message is not None
    assert "source file" in job.error_message.lower(), (
        f"error_message should mention source file; got {job.error_message!r}"
    )
    assert "queue.recovered" not in event_types, (
        f"missing-file job must not be recovered; events were {event_types}"
    )
    assert fake_service.convert_file.called is False

    await engine.dispose()


# ---------------------------------------------------------------------------
# A3: Startup recovery + sweeper must cooperate, not conflict.
#
# Today lifespan runs an UNCONDITIONAL UPDATE that marks every pending/
# processing job failed BEFORE any recovery could run, with no queue_backend
# filter. So durable rows are destroyed on every restart. The fix splits the
# startup work into one cooperative pass: recover durable rows first, then
# sweep only NON-durable stale rows (queue_backend IS NULL), so the two never
# fight over the same row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_and_sweep_preserves_durable_and_fails_non_durable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Startup recovery must reclaim durable rows and sweep only non-durable ones."""
    from unittest.mock import MagicMock

    engine, session_factory = await _session_factory(tmp_path)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    queue = SQLiteDurableQueueBackend()
    durable_id = "a3aaaaaa-1111-4111-8111-111111111111"
    nondurable_id = "a3bbbbbb-2222-4222-8222-222222222222"
    durable_file = tmp_path / "durable-start.txt"
    durable_file.write_text("durable", encoding="utf-8")

    async with session_factory() as session:
        # Durable job: pending, enqueued, source file present -> must be recovered.
        session.add(
            ConversionJob(
                id=durable_id,
                filename="durable-start.txt",
                original_name="durable-start.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
            )
        )
        await session.flush()
        await queue.enqueue(
            session,
            job_id=durable_id,
            filepath=str(durable_file),
            config={"output_format": "markdown", "original_name": "durable-start.txt"},
            max_retries=2,
        )
        # Non-durable job: pending, no queue_backend -> must be swept to failed.
        session.add(
            ConversionJob(
                id=nondurable_id,
                filename="stale.txt",
                original_name="stale.txt",
                status="pending",
                input_format="txt",
                output_format="markdown",
            )
        )
        await session.commit()

    fake_service = MagicMock()
    fake_service.convert_file.return_value = {
        "text": "# durable recovered",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data", "label": "Text"}},
    }

    manager = TaskManager(max_workers=1, durable_queue=queue)
    try:
        await manager.recover_and_sweep_durable_jobs(fake_service)
        # Wait for the recovered durable job to reach a terminal state.
        for _ in range(100):
            if manager.get_status(durable_id)["status"] in {"completed", "failed"}:
                break
            import time as _time
            _time.sleep(0.05)
    finally:
        manager.shutdown(wait=False)

    async with session_factory() as session:
        durable = await session.get(ConversionJob, durable_id)
        nondurable = await session.get(ConversionJob, nondurable_id)

    assert durable.status == "completed", (
        f"durable job should have been recovered + completed; got {durable.status}"
    )
    assert durable.result_text == "# durable recovered"
    assert nondurable.status == "failed", (
        f"non-durable stale job should be swept to failed; got {nondurable.status}"
    )
    assert nondurable.error_message is not None

    await engine.dispose()


# ---------------------------------------------------------------------------
# A5: recover_queued must catch processing rows with no lease (crashed before
# mark_started landed, or migrated from an older schema). Without the lease-less
# branch these rows sit in "processing" forever and are invisible to recovery.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_queued_catches_processing_row_with_no_lease(tmp_path: Path):
    """A durable job stuck in 'processing' with no lease must be recoverable."""
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    job_id = "a5aaaaaa-5555-4555-8555-555555555555"

    # Simulate a job that crashed mid-run before mark_started set a lease.
    # started_at is old (well past the grace window) so recovery should catch it.
    from datetime import datetime, timedelta, timezone
    old_started = datetime.now(timezone.utc) - timedelta(minutes=30)
    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="stuck.tsv",
                original_name="stuck.tsv",
                status="processing",
                input_format="tsv",
                output_format="markdown",
                queue_backend="sqlite",
                started_at=old_started,
                lease_owner=None,
                lease_expires_at=None,
                retry_count=0,
                max_retries=2,
                config_json=json.dumps({"durable_filepath": str(tmp_path / "stuck.tsv")}),
            )
        )
        await session.commit()

    async with session_factory() as session:
        recovered = await queue.recover_queued(session)

    assert [item.job_id for item in recovered] == [job_id], (
        f"lease-less processing job must be recovered; got {[i.job_id for i in recovered]}"
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_recover_queued_skips_fresh_processing_row_with_no_lease(tmp_path: Path):
    """A processing row that JUST started (within grace) must NOT be recovered.

    Defense against double-submit: if a worker is genuinely mid-run (started
    recently but lease not yet written due to timing), recovery must leave it
    alone so we don't double-dispatch a live job.
    """
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    job_id = "a5bbcccc-6666-4666-8666-666666666666"

    from datetime import datetime, timezone
    fresh_started = datetime.now(timezone.utc)  # just now, within grace window
    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="live.tsv",
                original_name="live.tsv",
                status="processing",
                input_format="tsv",
                output_format="markdown",
                queue_backend="sqlite",
                started_at=fresh_started,
                lease_owner=None,
                lease_expires_at=None,
                retry_count=0,
                max_retries=2,
                config_json=json.dumps({"durable_filepath": str(tmp_path / "live.tsv")}),
            )
        )
        await session.commit()

    async with session_factory() as session:
        recovered = await queue.recover_queued(session)

    assert recovered == [], (
        f"fresh processing job within grace must NOT be recovered; got {[i.job_id for i in recovered]}"
    )
    await engine.dispose()


# ---------------------------------------------------------------------------
# A6: enqueue must not resurrect a terminal job. A completed/failed/cancelled
# row re-enqueued by accident (or a buggy recovery loop) must be a no-op,
# never flip back to 'pending'.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_does_not_resurrect_completed_job(tmp_path: Path):
    """Enqueuing a job that is already completed must not flip it to pending."""
    engine, session_factory = await _session_factory(tmp_path)
    queue = SQLiteDurableQueueBackend()
    job_id = "a6aaaaaa-7777-4777-8777-777777777777"

    async with session_factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename="done.tsv",
                original_name="done.tsv",
                status="completed",
                input_format="tsv",
                output_format="markdown",
                result_text="# already done",
            )
        )
        await session.commit()

    async with session_factory() as session:
        await queue.enqueue(
            session,
            job_id=job_id,
            filepath=str(tmp_path / "done.tsv"),
            config={"output_format": "markdown"},
            max_retries=1,
        )
        await session.commit()

    async with session_factory() as session:
        job = await session.get(ConversionJob, job_id)
    assert job.status == "completed", (
        f"completed job must not be resurrected by enqueue; got {job.status}"
    )
    assert job.result_text == "# already done"
    await engine.dispose()
