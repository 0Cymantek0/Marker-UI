"""PR67B integration tests: the live conversion path through kernel authority.

These tests prove the PRODUCTION bridge uses the PR66/PR67 contracts
correctly — not that the kernel modules work in isolation (their own
suites own that). Evidence matrix from the PR67B plan:

* 10.1 happy-path authority trace (submission -> one work item -> fair
  claim -> liveness -> fenced acceptance -> completed-only-after-accept);
* 10.2 idempotent duplicate submission;
* 10.3 stale-worker late success;
* 10.4 divergent duplicate result;
* 10.5 cancellation race;
* 10.6 crash/restart boundary matrix;
* 10.7 ArtifactHandle result crosses acceptance after resolution;
* 10.8 durable progress/lifecycle reconstruction after restart;
* 10.9 slow SSE client cannot block execution;
* 10.10 fair dispatch and hard fan-out cap through the integration.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel import fencing, liveness, scheduler
from app.kernel.commit import KernelCommitService
from app.kernel.errors import PublicationConflictError, StaleFenceError
from app.kernel.models import (
    KernelEvent,
    KernelWorkLease,
)
from app.kernel.outbox import list_outbox
from app.models.job import ConversionJob
from app.services import artifact_handles
from app.services.kernel_runtime import (
    WORK_KIND,
    ActiveClaim,
    build_result_descriptor,
)

pytestmark = pytest.mark.asyncio

FAKE_RESULT = {
    "text": "# Kernel Markdown\n\nConverted through the runtime authority.",
    "extension": "md",
    "images": [],
    "metadata": {"pages": 1},
}


class FakeConversionService:
    """Thread-backend conversion service with controllable blocking/failure."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or dict(FAKE_RESULT)
        self.block = threading.Event()
        self.block.set()  # non-blocking by default
        self.fail_calls = 0
        self.calls = 0

    def plan(self, filepath: str, config: dict[str, Any]) -> Any:
        return SimpleNamespace(execution_backend="cpu_thread")

    def supports_multiple_formats(self, filepath: str, config: dict[str, Any]) -> bool:
        return False

    def convert_file(self, filepath: str, config: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.fail_calls and self.calls <= self.fail_calls:
            raise RuntimeError("injected conversion failure")
        self.block.wait(timeout=60)
        return json.loads(json.dumps(self.result))


async def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if asyncio.iscoroutine(last):
            last = await last
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def _as_utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@pytest_asyncio.fixture
async def runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Migrated kernel DB + real TaskManager + wired runtime coordinator."""
    from app.db_migration import upgrade_database
    import app.services.task_manager as tm_module
    from app.services.task_manager import TaskManager

    url = f"sqlite+aiosqlite:///{(tmp_path / 'runtime.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(tm_module, "async_session_factory", factory)

    service = FakeConversionService()
    manager = TaskManager()
    coordinator = manager.start_kernel_runtime(
        service,
        session_factory=factory,
        commit_service=KernelCommitService(factory),
        workspace_id="t",
        owner_id="test-runtime",
        lease_seconds=60.0,
        renew_interval_seconds=0.05,
        dispatch_poll_seconds=0.05,
        watchdog_interval_seconds=0.1,
        max_in_flight=4,
    )
    try:
        yield SimpleNamespace(
            manager=manager,
            coordinator=coordinator,
            factory=factory,
            service=service,
            tmp_path=tmp_path,
        )
    finally:
        coordinator.stop()
        service.block.set()
        manager.shutdown()
        await engine.dispose()


async def _make_job(env, job_id: str, config_extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    src_dir = env.tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    src = src_dir / f"{job_id}.pdf"
    src.write_text("source document bytes", encoding="utf-8")
    out_dir = env.tmp_path / "out" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "local_filepath": str(src),
        "output_dir": str(out_dir),
        "original_name": f"{job_id}.pdf",
        "output_format": "markdown",
    }
    config.update(config_extra or {})
    async with env.factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename=f"{job_id}.pdf",
                original_name=f"{job_id}.pdf",
                status="pending",
                input_format="pdf",
                output_format="markdown",
                config_json=json.dumps(config),
                queue_backend="kernel",
            )
        )
        await session.commit()
    return str(src), config


async def _row(env, job_id: str) -> ConversionJob | None:
    async with env.factory() as session:
        return await session.get(ConversionJob, job_id)


async def _outbox_rows(env):
    return await list_outbox(env.factory, workspace_id="t")


async def _publication(env, work_id: int) -> fencing.Publication | None:
    return await fencing.get_publication(env.factory, work_id=work_id)


async def _lease(env, work_id: int) -> fencing.WorkLease | None:
    return await fencing.get_lease(env.factory, work_id)


async def _events(env, event_type: str | None = None) -> list[SimpleNamespace]:
    async with env.factory() as session:
        stmt = select(KernelEvent).where(KernelEvent.workspace_id == "t")
        if event_type is not None:
            stmt = stmt.where(KernelEvent.event_type == event_type)
        rows = (
            await session.execute(stmt.order_by(KernelEvent.semantic_sequence.asc()))
        ).scalars().all()
    events = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            payload = {}
        events.append(
            SimpleNamespace(
                event_type=row.event_type,
                payload=payload if isinstance(payload, dict) else {},
                seq=row.semantic_sequence,
            )
        )
    return events


async def _submit(env, job_id: str, config_extra: dict[str, Any] | None = None) -> int:
    _src, config = await _make_job(env, job_id, config_extra)
    return await env.manager.submit_conversion(job_id, _src, config, env.service)


async def _wait_for_row_status(env, job_id: str, *statuses: str, timeout: float = 25.0):
    async def _poll():
        while True:
            r = await _row(env, job_id)
            if r and r.status in statuses:
                return r
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _run_to_completion(env, job_id: str, timeout: float = 25.0) -> ConversionJob:
    env.coordinator.start()
    return await _wait_for_row_status(
        env, job_id, "completed", "failed", "cancelled", timeout=timeout
    )


class TestHappyPathAuthority:
    async def test_upload_shaped_config_resolves_source_at_launch(self, runtime_env):
        """Uploads persist only ``durable_filepath`` (no local_filepath).

        The dispatcher must resolve the source from the durable path —
        the exact shape REST/agent uploads produce after PR67B removed
        the legacy enqueue that used to inject it.
        """
        env = runtime_env
        job_id = "job-upload-1"
        src_dir = env.tmp_path / "up"
        src_dir.mkdir(exist_ok=True)
        src = src_dir / "uploaded.pdf"
        src.write_text("uploaded bytes", encoding="utf-8")
        out_dir = env.tmp_path / "out" / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "durable_filepath": str(src),
            "output_dir": str(out_dir),
            "original_name": "uploaded.pdf",
            "output_format": "markdown",
        }
        async with env.factory() as session:
            session.add(
                ConversionJob(
                    id=job_id,
                    filename="uploaded.pdf",
                    original_name="uploaded.pdf",
                    status="pending",
                    input_format="pdf",
                    output_format="markdown",
                    config_json=json.dumps(config),
                    queue_backend="kernel",
                )
            )
            await session.commit()
        await env.manager.submit_conversion(job_id, str(src), config, env.service)
        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"

    async def test_completed_only_after_fenced_acceptance(self, runtime_env):
        env = runtime_env
        job_id = "job-happy-1"
        work_id = await _submit(env, job_id)
        assert isinstance(work_id, int)

        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"
        assert row.result_text and "Kernel Markdown" in row.result_text
        assert row.result_path and Path(row.result_path).is_file()
        assert row.progress == 100

        rows = await _outbox_rows(env)
        assert len(rows) == 1
        assert rows[0].state == "done"
        assert rows[0].work_kind == WORK_KIND

        publication = await _publication(env, work_id)
        assert publication is not None
        descriptor = publication.result
        assert descriptor["kind"] == "conversion.result"
        assert descriptor["result_path"] == row.result_path
        assert len(descriptor["result_text"]["sha256"]) == 64
        assert descriptor["result_file"]["bytes"] > 0

        lease = await _lease(env, work_id)
        assert lease is not None and lease.state == "accepted"

        claimed = [e for e in await _events(env, "work.claimed") if e.payload.get("work_id") == work_id]
        accepted = [e for e in await _events(env, "work.accepted") if e.payload.get("work_id") == work_id]
        assert len(claimed) == 1
        assert len(accepted) == 1

        # Authority ordering: acceptance linearized before the row could
        # say completed.
        assert _as_utc(publication.accepted_at) <= _as_utc(row.completed_at)

    async def test_processing_projection_requires_live_lease(self, runtime_env):
        env = runtime_env
        env.service.block.clear()  # hold the conversion open
        job_id = "job-live-1"
        work_id = await _submit(env, job_id)
        env.coordinator.start()

        await _wait_for_row_status(env, job_id, "processing")
        lease = await _lease(env, work_id)
        assert lease is not None and lease.state == "leased"
        assert await _publication(env, work_id) is None  # no completion truth yet

        env.service.block.set()
        final = await _wait_for_row_status(env, job_id, "completed", timeout=15)
        assert final.status == "completed"
        assert await _resolve(env, job_id) == work_id


async def _resolve(env, job_id: str) -> int | None:
    return await env.coordinator.resolve_work_for_job(job_id)


class TestIdempotentSubmission:
    async def test_duplicate_submission_single_work_item(self, runtime_env):
        env = runtime_env
        job_id = "job-idem-1"
        _src, config = await _make_job(env, job_id)
        first = await env.manager.submit_conversion(job_id, _src, config, env.service)
        second = await env.manager.submit_conversion(job_id, _src, config, env.service)
        third = await env.coordinator.authorize(job_id, config)
        assert first == second == third
        rows = await _outbox_rows(env)
        assert len(rows) == 1

        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"
        assert len(await _outbox_rows(env)) == 1  # no duplicate accepted work

    async def test_duplicate_same_result_converges(self, runtime_env):
        env = runtime_env
        job_id = "job-idem-2"
        work_id = await _submit(env, job_id)
        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"

        lease = await _lease(env, work_id)
        publication = await _publication(env, work_id)
        outcome, appended = await scheduler.accept_work(
            env.factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result=publication.result,
        )
        assert outcome.already_accepted is True
        assert appended is False  # no second work.accepted event
        events = [e for e in await _events(env, "work.accepted") if e.payload.get("work_id") == work_id]
        assert len(events) == 1


class TestStaleWorker:
    async def test_late_success_cannot_complete_or_renew(self, runtime_env):
        env = runtime_env
        env.service.block.clear()  # worker A never finishes its conversion
        job_id = "job-stale-1"
        work_id = await _submit(env, job_id)
        env.coordinator.start()
        await _wait_until(lambda: env.manager._kernel_claims.get(job_id) is not None)
        # Simulate worker A losing the machine: hard-stop everything,
        # then a fresh generation (worker B) takes over via watchdog.
        claim_a = env.manager._kernel_claims[job_id]
        token_a = claim_a.fencing_token
        nonce_a = claim_a.challenge_nonce
        env.coordinator.stop()
        env.manager.shutdown()
        # Expire A's lease instantly by rewinding it in the DB is not
        # available; instead drive the watchdog of a fresh coordinator.
        from app.services.task_manager import TaskManager

        service_b = FakeConversionService()
        manager_b = TaskManager()
        coordinator_b = manager_b.start_kernel_runtime(
            service_b,
            session_factory=env.factory,
            commit_service=KernelCommitService(env.factory),
            workspace_id="t",
            owner_id="test-runtime",
            lease_seconds=60.0,
            renew_interval_seconds=0.05,
            dispatch_poll_seconds=0.05,
            watchdog_interval_seconds=0.1,
            max_in_flight=4,
        )
        try:
            # Force A's lease to be lapsed so the watchdog takes over
            # without waiting a real minute.
            async with env.factory() as session:
                from sqlalchemy import update as sa_update
                from datetime import timedelta

                await session.execute(
                    sa_update(KernelWorkLease)
                    .where(KernelWorkLease.work_id == work_id)
                    .values(
                        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            coordinator_b.start()
            row = await _wait_for_row_status(env, job_id, "completed")
            assert row.status == "completed"

            lease = await _lease(env, work_id)
            assert lease.fencing_token > token_a  # takeover advanced the fence

            # Worker A wakes up late with its stale evidence:
            with pytest.raises(StaleFenceError):
                await scheduler.accept_work(
                    env.factory,
                    work_id=work_id,
                    fencing_token=token_a,
                    result={"kind": "conversion.result", "schema": 1, "job_id": job_id, "stale": True},
                )
            with pytest.raises((StaleFenceError, Exception)):
                await liveness.renew_lease(
                    env.factory,
                    work_id=work_id,
                    owner_id=claim_a.owner_id,
                    fencing_token=token_a,
                    challenge_nonce=nonce_a,
                    progress=999,
                    active_request_id=claim_a.active_request_id,
                )
            publications = [
                p
                for p in (
                    await _publication(env, work_id),
                )
                if p is not None
            ]
            assert len(publications) == 1
            assert "stale" not in (publications[0].result or {})
        finally:
            coordinator_b.stop()
            service_b.block.set()
            manager_b.shutdown()
            # restore the fixture manager reference bookkeeping
            env.manager = manager_b


    async def test_stale_generation_failure_cannot_kill_successor(self, runtime_env):
        """Regression: a lapsed generation's late failure must not consume
        the successor's retry budget or terminal-fail its live work."""
        env = runtime_env
        env.service.block.clear()
        job_id = "job-stale-2"
        work_id = await _submit(env, job_id)
        env.coordinator.start()
        await _wait_until(lambda: env.manager._kernel_claims.get(job_id) is not None)
        claim_gen1 = env.manager._kernel_claims[job_id]

        # Generation 1 lapses; a fresh coordinator takes over as gen 2.
        env.coordinator.stop()
        from datetime import timedelta
        from sqlalchemy import update as sa_update

        async with env.factory() as session:
            await session.execute(
                sa_update(KernelWorkLease)
                .where(KernelWorkLease.work_id == work_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
                .execution_options(synchronize_session=False)
            )
            await session.commit()

        from app.services.task_manager import TaskManager

        service_b = FakeConversionService()
        service_b.block.clear()
        manager_b = TaskManager()
        coordinator_b = manager_b.start_kernel_runtime(
            service_b,
            session_factory=env.factory,
            commit_service=KernelCommitService(env.factory),
            workspace_id="t",
            owner_id="test-runtime",
            lease_seconds=60.0,
            renew_interval_seconds=0.05,
            dispatch_poll_seconds=0.05,
            watchdog_interval_seconds=0.1,
        )
        try:
            coordinator_b.start()
            await _wait_until(lambda: manager_b._kernel_claims.get(job_id) is not None)
            claim_gen2 = manager_b._kernel_claims[job_id]
            assert claim_gen2.fencing_token > claim_gen1.fencing_token

            # Generation 1 finally raises, carrying its own stale claim.
            await env.manager._fail_job(job_id, "generation-1 crash", claim=claim_gen1)
            row = await _row(env, job_id)
            assert row.status == "processing"  # successor untouched
            failed_events = [
                e for e in await _events(env, "work.failed")
                if e.payload.get("work_id") == work_id
            ]
            assert failed_events == []
            rows = await _outbox_rows(env)
            assert rows[0].state == "in_flight"

            # Generation 2 completes normally.
            service_b.block.set()
            row = await _wait_for_row_status(env, job_id, "completed")
            assert row.status == "completed"
            publication = await _publication(env, work_id)
            assert publication is not None
            assert publication.fencing_token == claim_gen2.fencing_token
        finally:
            coordinator_b.stop()
            service_b.block.set()
            manager_b.shutdown()
            env.manager = manager_b


class TestDivergentResult:
    async def test_divergent_duplicate_surfaced_as_conflict(self, runtime_env):
        env = runtime_env
        job_id = "job-div-1"
        work_id = await _submit(env, job_id)
        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"
        publication = await _publication(env, work_id)
        lease = await _lease(env, work_id)

        divergent = dict(publication.result)
        divergent["result_text"] = {"bytes": 1, "sha256": "0" * 64}
        with pytest.raises(PublicationConflictError):
            await scheduler.accept_work(
                env.factory,
                work_id=work_id,
                fencing_token=lease.fencing_token,
                result=divergent,
            )
        # Accepted state unchanged; row still completed with original truth.
        after = await _publication(env, work_id)
        assert after.result_hash == publication.result_hash
        row_after = await _row(env, job_id)
        assert row_after.status == "completed"
        assert row_after.result_text == row.result_text


class TestCancellation:
    async def test_cancel_during_execution_beats_late_result(self, runtime_env):
        env = runtime_env
        env.service.block.clear()
        job_id = "job-cancel-1"
        work_id = await _submit(env, job_id)
        env.coordinator.start()
        await _wait_until(lambda: env.manager._kernel_claims.get(job_id) is not None)

        cancelled = await env.manager.cancel_job(job_id)
        assert cancelled is True
        row = await _row(env, job_id)
        assert row.status == "cancelled"

        cancel_requested = [
            e for e in await _events(env, "work.cancel_requested")
            if e.payload.get("work_id") == work_id
        ]
        cancelled_events = [
            e for e in await _events(env, "work.cancelled")
            if e.payload.get("work_id") == work_id
        ]
        assert len(cancel_requested) == 1
        assert len(cancelled_events) == 1
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"
        assert await _publication(env, work_id) is None

        # The worker finally produces its result late.
        env.service.block.set()
        await asyncio.sleep(0.5)
        row = await _row(env, job_id)
        assert row.status == "cancelled"  # late terminal cannot rewrite it
        assert await _publication(env, work_id) is None

    async def test_cancel_pending_work_stops_dispatch(self, runtime_env):
        env = runtime_env
        job_id = "job-cancel-2"
        work_id = await _submit(env, job_id)  # authorized, never dispatched

        cancelled = await env.manager.cancel_job(job_id)
        assert cancelled is True
        row = await _row(env, job_id)
        assert row.status == "cancelled"
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"
        cancelled_events = [
            e for e in await _events(env, "work.cancelled")
            if e.payload.get("work_id") == work_id
        ]
        assert len(cancelled_events) == 1

        env.coordinator.start()
        await asyncio.sleep(0.5)
        row = await _row(env, job_id)
        assert row.status == "cancelled"  # done work is never dispatched


    async def test_cancel_after_completion_cannot_overwrite(self, runtime_env):
        env = runtime_env
        job_id = "job-cancel-3"
        work_id = await _submit(env, job_id)
        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"

        # A cancel whose row read raced the accepted completion must not
        # flip durable accepted truth back to cancelled.
        result = await env.manager.cancel_job(job_id)
        assert result is False
        row = await _row(env, job_id)
        assert row.status == "completed"
        assert await _publication(env, work_id) is not None
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"
        cancelled_events = [
            e for e in await _events(env, "work.cancelled")
            if e.payload.get("work_id") == work_id
        ]
        assert cancelled_events == []


class TestExecutionFailure:
    async def test_failure_is_terminal_with_durable_event(self, runtime_env):
        env = runtime_env
        env.service.fail_calls = 5
        job_id = "job-fail-1"
        work_id = await _submit(env, job_id)

        row = await _run_to_completion(env, job_id)
        assert row.status == "failed"
        assert row.error_message and "injected conversion failure" in row.error_message
        failed_events = [
            e for e in await _events(env, "work.failed")
            if e.payload.get("work_id") == work_id
        ]
        assert len(failed_events) == 1
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"
        assert await _publication(env, work_id) is None
        lease = await _lease(env, work_id)
        assert lease.state == "released"

    async def test_failure_within_retry_budget_requeues(self, runtime_env):
        env = runtime_env
        env.service.fail_calls = 1  # first attempt fails, second succeeds
        job_id = "job-retry-1"
        work_id = await _submit(env, job_id, {"max_retries": 2})

        row = await _run_to_completion(env, job_id, timeout=30)
        assert row.status == "completed"
        assert env.service.calls >= 2
        retry_events = [
            e for e in await _events(env, "work.retry")
            if e.payload.get("work_id") == work_id
        ]
        assert len(retry_events) >= 1
        assert (await _publication(env, work_id)) is not None


class TestCrashRestartMatrix:
    async def test_boundary_row_without_authorization_is_adopted(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-1"
        await _make_job(env, job_id)  # row committed, authorize never ran

        report = await env.coordinator.recover()
        assert job_id in report["adopted"]
        rows = await _outbox_rows(env)
        assert len(rows) == 1 and rows[0].state == "pending"

        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"
        assert len(await _outbox_rows(env)) == 1

    async def test_boundary_authorized_but_not_dispatched(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-2"
        work_id = await _submit(env, job_id)

        report = await env.coordinator.recover()
        rows = await _outbox_rows(env)
        assert len(rows) == 1 and rows[0].state == "pending"
        assert report["requeued"] == []

        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"
        assert await _publication(env, work_id) is not None

    async def test_boundary_claimed_then_owner_died_takeover(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-3"
        work_id = await _submit(env, job_id)
        # Worker A claims and dies instantly (short lease, no liveness).
        claimed = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
            lease_seconds=60.0,
        )
        assert claimed is not None and claimed.work_id == work_id
        token_a = claimed.lease.fencing_token

        from datetime import timedelta
        from sqlalchemy import update as sa_update

        async with env.factory() as session:
            await session.execute(
                sa_update(KernelWorkLease)
                .where(KernelWorkLease.work_id == work_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
                .execution_options(synchronize_session=False)
            )
            await session.commit()

        env.coordinator.start()  # watchdog requeues; dispatch takes over
        row = await _wait_for_row_status(env, job_id, "completed", "failed", timeout=30)
        assert row.status == "completed"
        lease = await _lease(env, work_id)
        assert lease.fencing_token > token_a
        assert lease.state == "accepted"
        retry_events = [
            e for e in await _events(env, "work.retry")
            if e.payload.get("work_id") == work_id
        ]
        assert len(retry_events) >= 1  # lapse was a durable, honest retry

    async def test_boundary_accepted_before_projection(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-4"
        work_id = await _submit(env, job_id)
        claimed = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
        )
        assert claimed is not None

        # Worker produced durable output and crossed acceptance, then the
        # process died before the row projection.
        out_file = env.tmp_path / "out" / job_id / "doc.md"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("# Recovered Markdown\n", encoding="utf-8")
        descriptor = build_result_descriptor(
            job_id=job_id,
            output_format="markdown",
            result_text="# Recovered Markdown\n",
            formats_json=None,
            result_metadata_json=None,
            final_path=out_file,
            manifest_path=out_file.with_name("manifest.json"),
            asset_count=0,
            formats=["markdown"],
        )
        outcome, _ = await scheduler.accept_work(
            env.factory,
            work_id=work_id,
            fencing_token=claimed.lease.fencing_token,
            result=descriptor,
        )
        assert outcome.already_accepted is False
        row = await _row(env, job_id)
        assert row.status in ("pending", "processing")  # projection lost

        report = await env.coordinator.recover()
        assert job_id in report["projected_completed"]
        row = await _row(env, job_id)
        assert row.status == "completed"
        assert row.result_text == "# Recovered Markdown\n"
        assert row.result_path == str(out_file)
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"

    async def test_boundary_cancel_event_without_projection(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-5"
        work_id = await _submit(env, job_id)
        claimed = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
        )
        assert claimed is not None
        observed = await liveness.report_cancellation(
            env.factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=claimed.lease.fencing_token,
            reason="cancelled by user",
        )
        assert observed is True
        await fencing.release(
            env.factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=claimed.lease.fencing_token,
        )
        from app.kernel import events as kernel_events

        await kernel_events.append(
            env.factory,
            workspace_id="t",
            stream="work",
            event_type="work.cancelled",
            payload={"work_id": work_id, "job_id": job_id, "reason": "cancelled by user"},
        )

        report = await env.coordinator.recover()
        assert job_id in report["projected_terminal"]
        row = await _row(env, job_id)
        assert row.status == "cancelled"
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"

    async def test_boundary_lapse_budget_exhausted_fails_terminal(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-6"
        _src, config = await _make_job(env, job_id)
        # Burn the single lapse-retry budget before the crash.
        work_id = await env.coordinator.authorize(job_id, config)
        first = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
        )
        assert first is not None
        await fencing.release(
            env.factory, work_id=work_id, owner_id="worker-a",
            fencing_token=first.lease.fencing_token,
        )
        from app.kernel import outbox as outbox_mod

        await outbox_mod.release(env.factory, work_id)  # attempts -> 1

        second = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
        )
        assert second is not None
        from datetime import timedelta
        from sqlalchemy import update as sa_update

        async with env.factory() as session:
            await session.execute(
                sa_update(KernelWorkLease)
                .where(KernelWorkLease.work_id == work_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
                .execution_options(synchronize_session=False)
            )
            await session.commit()

        env.coordinator.start()
        row = await _wait_for_row_status(env, job_id, "failed", timeout=30)
        assert "lease expired" in (row.error_message or "")
        failed_events = [
            e for e in await _events(env, "work.failed")
            if e.payload.get("work_id") == work_id
        ]
        assert len(failed_events) == 1
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"

    async def test_boundary_failure_ack_before_projection(self, runtime_env):
        env = runtime_env
        job_id = "job-crash-7"
        _src, config = await _make_job(env, job_id)
        work_id = await env.coordinator.authorize(job_id, config)
        claimed = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
        )
        assert claimed is not None
        # The failing generation durably recorded its terminal event and
        # acked the delivery, then died before the row projection.
        await fencing.release(
            env.factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=claimed.lease.fencing_token,
        )
        from app.kernel import events as kernel_events
        from app.kernel import outbox as outbox_mod

        await kernel_events.append(
            env.factory,
            workspace_id="t",
            stream="work",
            event_type="work.failed",
            payload={"work_id": work_id, "job_id": job_id, "error": "boom", "attempts": 0},
        )
        await outbox_mod.ack(env.factory, work_id)
        row = await _row(env, job_id)
        assert row.status == "pending"  # projection lost in the crash

        report = await env.coordinator.recover()
        assert job_id in report["projected_terminal"]
        row = await _row(env, job_id)
        assert row.status == "failed"
        assert "boom" in (row.error_message or "")
        rows = await _outbox_rows(env)
        assert rows[0].state == "done"

    async def test_legacy_durable_rows_are_adopted_not_resubmitted(self, runtime_env):
        env = runtime_env
        job_id = "job-legacy-1"
        _src, config = await _make_job(env, job_id)
        async with env.factory() as session:
            from sqlalchemy import update as sa_update

            await session.execute(
                sa_update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .values(queue_backend="sqlite")
            )
            await session.commit()

        report = await env.coordinator.recover()
        assert job_id in report["adopted"]
        rows = await _outbox_rows(env)
        assert len(rows) == 1  # adopted into kernel authority, exactly once

        row = await _run_to_completion(env, job_id)
        assert row.status == "completed"

    async def test_non_durable_stale_rows_swept(self, runtime_env):
        env = runtime_env
        job_id = "job-sweep-1"
        await _make_job(env, job_id)
        async with env.factory() as session:
            from sqlalchemy import update as sa_update

            await session.execute(
                sa_update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .values(queue_backend=None, status="processing")
            )
            await session.commit()

        report = await env.coordinator.recover()
        assert job_id in report["swept"]
        row = await _row(env, job_id)
        assert row.status == "failed"
        assert "Interrupted by server restart" in (row.error_message or "")


class TestArtifactHandleAcceptance:
    async def test_resolved_handle_result_crosses_acceptance(self, runtime_env, tmp_path):
        env = runtime_env
        job_id = "job-handle-1"
        big_text = "# Big Kernel Document\n\n" + ("payload " * 200_000)  # > 1 MiB
        result = {
            "text": big_text,
            "extension": "md",
            "images": [],
            "metadata": {"pages": 1},
        }
        store = artifact_handles.ArtifactHandleStore(tmp_path / "handles")
        env.manager._artifact_store = store
        _src, config = await _make_job(env, job_id)

        work_id = await env.coordinator.authorize(job_id, config)
        claimed = await scheduler.claim_fair(
            env.factory,
            owner_id="worker-a",
            resource_class="conversion",
            workspace_id="t",
        )
        assert claimed is not None and claimed.work_id == work_id
        claim = ActiveClaim(
            work_id=work_id,
            job_id=job_id,
            owner_id="worker-a",
            fencing_token=claimed.lease.fencing_token,
            challenge_nonce=claimed.challenge_nonce,
            active_request_id=f"exec:{job_id}:{claimed.lease.fencing_token}",
        )
        env.manager._kernel_claims[job_id] = claim
        env.coordinator._active_by_job[job_id] = claim
        env.coordinator._active_by_work[work_id] = claim

        # Worker side: stage the oversized field into a verified handle.
        envelope = artifact_handles.stage_worker_payload(
            result, store=store, job_id=job_id, inline_limit=1024
        )
        assert artifact_handles.is_handle_envelope(envelope)
        assert len(envelope["artifact_handles"]["handles"]) == 1

        # Parent side: strict resolution before finalization.
        resolved = env.manager._resolve_artifact_payload(job_id, envelope)
        assert resolved is not None
        assert resolved["text"] == big_text

        projected = await env.manager._finalize_job(job_id, resolved, config, None)
        assert projected is True

        publication = await _publication(env, work_id)
        assert publication is not None
        import hashlib

        expected_sha = hashlib.sha256(big_text.encode("utf-8")).hexdigest()
        assert publication.result["result_text"]["sha256"] == expected_sha
        assert "artifact_handles" not in publication.result  # transport never truth
        row = await _row(env, job_id)
        assert row.status == "completed"
        # Consumed blob: no live handle requirement after completion.
        blobs = list((tmp_path / "handles" / "blobs").glob("*"))
        assert blobs == []

    async def test_corrupt_handle_fails_closed(self, runtime_env, tmp_path):
        env = runtime_env
        job_id = "job-handle-2"
        big_text = "x" * 5000
        result = {"text": big_text, "extension": "md", "images": [], "metadata": {}}
        store = artifact_handles.ArtifactHandleStore(tmp_path / "handles")
        env.manager._artifact_store = store
        _src, config = await _make_job(env, job_id)
        work_id = await env.coordinator.authorize(job_id, config)
        claimed = await scheduler.claim_fair(
            env.factory, owner_id="worker-a", resource_class="conversion", workspace_id="t",
        )
        claim = ActiveClaim(
            work_id=work_id,
            job_id=job_id,
            owner_id="worker-a",
            fencing_token=claimed.lease.fencing_token,
            challenge_nonce=claimed.challenge_nonce,
            active_request_id=f"exec:{job_id}:1",
        )
        env.manager._kernel_claims[job_id] = claim

        # Worker side: stage the oversized field into a verified handle.
        envelope = artifact_handles.stage_worker_payload(
            result, store=store, job_id=job_id, inline_limit=1024
        )
        assert artifact_handles.is_handle_envelope(envelope)
        handle = envelope["artifact_handles"]["handles"][0]
        blob_path = tmp_path / "handles" / "blobs" / f"{handle['name']}.bin"
        blob_path.write_bytes(b"corrupted bytes")  # tamper after staging

        # Resolution fails closed: strict rejection of corrupted bytes.
        # (TaskManager._resolve_artifact_payload wraps this and triggers
        # the failure path; its drain-thread async write is covered by the
        # process-path suites, so here the kernel failure is driven
        # directly on the running loop.)
        with pytest.raises(artifact_handles.ArtifactHandleError):
            artifact_handles.resolve_worker_payload(envelope, store=store, job_id=job_id)
        await env.manager._fail_job(job_id, "artifact handoff failed: corrupt blob")
        row = await _row(env, job_id)
        assert row.status == "failed"
        assert "artifact handoff" in (row.error_message or "")
        assert await _publication(env, work_id) is None


class TestDurableProgressRestart:
    async def test_lifecycle_truth_survives_manager_recreation(self, runtime_env):
        env = runtime_env
        env.service.block.clear()
        job_id = "job-restart-1"
        work_id = await _submit(env, job_id)
        env.coordinator.start()
        await _wait_until(lambda: env.manager._kernel_claims.get(job_id) is not None)

        # Real control-loop activity -> durable coalesced progress.
        for i in range(5):
            env.manager.report_stage_progress(job_id, 10 + i, f"stage {i}")
        from app.kernel import events as kernel_events

        await _wait_until(
            lambda: kernel_events.get_progress(env.factory, workspace_id="t", work_id=work_id)
        )
        progress = await kernel_events.get_progress(
            env.factory, workspace_id="t", work_id=work_id
        )
        assert progress.counter >= 1

        # First process finishes its work normally, then "restart".
        env.service.block.set()
        await _wait_for_row_status(env, job_id, "completed")
        env.coordinator.stop()
        env.manager.shutdown()

        from app.services.task_manager import TaskManager

        manager_b = TaskManager()
        coordinator_b = manager_b.start_kernel_runtime(
            FakeConversionService(),
            session_factory=env.factory,
            commit_service=KernelCommitService(env.factory),
            workspace_id="t",
            owner_id="test-runtime",
            lease_seconds=60.0,
            renew_interval_seconds=0.05,
            dispatch_poll_seconds=0.05,
            watchdog_interval_seconds=0.1,
        )
        try:
            await coordinator_b.recover()
            # Completed truth needs no invention; durable state proves it.
            row = await _row(env, job_id)
            assert row.status == "completed"
            claimed = [
                e for e in await _events(env, "work.claimed")
                if e.payload.get("work_id") == work_id
            ]
            accepted = [
                e for e in await _events(env, "work.accepted")
                if e.payload.get("work_id") == work_id
            ]
            assert len(claimed) == 1 and len(accepted) == 1
            snapshot = await kernel_events.get_progress(
                env.factory, workspace_id="t", work_id=work_id
            )
            assert snapshot is not None  # progress resumed from latest snapshot

            # A job submitted by the dead process but never dispatched
            # still completes under the new manager.
            job2 = "job-restart-2"
            await _submit(env, job2)
            coordinator_b.start()
            row2 = await _wait_for_row_status(env, job2, "completed", timeout=25)
            assert row2.status == "completed"
            assert manager_b.get_status(job2)["status"] in ("completed", "pending")
        finally:
            coordinator_b.stop()
            manager_b.shutdown()
            env.manager = manager_b


class TestSlowClient:
    async def test_slow_sse_consumer_does_not_block_execution(self, runtime_env):
        env = runtime_env
        env.service.block.clear()
        job_id = "job-slow-1"
        await _submit(env, job_id)
        env.coordinator.start()
        await _wait_until(lambda: env.manager._kernel_claims.get(job_id) is not None)

        class _SlowRequest:
            async def is_disconnected(self) -> bool:
                return False

        consumed: list[Any] = []

        async def _consume_slowly():
            async for event in env.manager.job_events(_SlowRequest(), job_id):
                consumed.append(event)
                await asyncio.sleep(0.5)  # slower than the job's cadence

        reader = asyncio.create_task(_consume_slowly())
        await asyncio.sleep(0.3)
        started = time.monotonic()
        env.service.block.set()
        row = await _wait_for_row_status(env, job_id, "completed", timeout=20)
        elapsed = time.monotonic() - started
        assert row.status == "completed"
        # Completion did not wait for the slow consumer to catch up.
        assert elapsed < 5.0
        assert not reader.done()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader


class TestFairDispatch:
    async def test_execution_only_through_claim_fair_with_cap(self, runtime_env):
        env = runtime_env
        from sqlalchemy import func

        calls = {"n": 0}
        original = scheduler.claim_fair

        async def _spied(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        import app.services.kernel_runtime as kr_module

        kr_module.scheduler.claim_fair = _spied
        try:
            # Hard fan-out cap of one for the "cap" group.
            await scheduler.set_group_policy(
                env.factory,
                resource_class="conversion",
                group_id="cap",
                policy=scheduler.GroupPolicy(max_in_flight=1),
            )
            env.service.block.clear()
            job_a = "job-fair-1"
            job_b = "job-fair-2"
            await _submit(env, job_a, {"scheduling_group": "cap"})
            await _submit(env, job_b, {"scheduling_group": "cap"})
            env.coordinator.start()

            await _wait_until(lambda: env.manager._kernel_claims.get(job_a) is not None)
            await asyncio.sleep(0.5)
            # Cap holds: second job not claimed while the first is leased.
            assert env.manager._kernel_claims.get(job_b) is None
            async with env.factory() as session:
                live = (
                    await session.execute(
                        select(func.count())
                        .select_from(KernelWorkLease)
                        .where(KernelWorkLease.state == "leased")
                    )
                ).scalar_one()
            assert live == 1

            env.service.block.set()
            row_a = await _wait_for_row_status(env, job_a, "completed")
            row_b = await _wait_for_row_status(env, job_b, "completed", timeout=25)
            assert row_a.status == "completed"
            assert row_b.status == "completed"
        finally:
            kr_module.scheduler.claim_fair = original
        # Every execution generation went through the fair scheduler.
        assert calls["n"] >= 2
        claimed_events = [e for e in await _events(env, "work.claimed")]
        assert len(claimed_events) >= 2
