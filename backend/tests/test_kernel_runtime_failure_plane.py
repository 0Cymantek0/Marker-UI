"""Invariant 38 failure-plane suite: the live kernel runtime under
destination failures, crashes, cancellation races, and pressure.

Every test drives the PRODUCTION bridge (TaskManager -> kernel
authorization -> claim_fair dispatch -> fenced acceptance -> projection)
with faults injected at the real boundaries:

* the external destination (``write_conversion_output``) is wrapped by a
  controller that delegates to the real writer and can fail, hang, or
  observe calls per job;
* the acceptance boundary can lose the database for a bounded window;
* the model service can run in a REAL child process that is killed
  mid-work (literal model-service crash / OOM-kill analog);
* shared-memory exhaustion surfaces as MemoryError from the converter;
* pressure tests exceed the fan-out cap and mix fast/slow/failing/
  cancelled work under one coordinator.

Semantic assertions only — no wall-clock SLOs. Timing-sensitive cases
use monotonic-deadline polling (``_wait_until``) and hold/release gates
instead of sleeps where possible.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from app.kernel.commit import KernelCommitService
from app.kernel.models import KernelWorkLease
from app.kernel.outbox import OUTBOX_STATE_DONE, OUTBOX_STATE_IN_FLIGHT
from app.services import task_manager as tm_module
from app.services.task_manager import TaskManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)
from tests.test_kernel_runtime import (
    FAKE_RESULT,
    _events,
    _outbox_rows,
    _publication,
    _row,
    _run_to_completion,
    _submit,
    _wait_for_row_status,
    _wait_until,
)

WORKSPACE = "t"
_COORDINATOR_DEFAULTS = {
    "workspace_id": WORKSPACE,
    "owner_id": "test-failure-plane",
    "lease_seconds": 60.0,
    "renew_interval_seconds": 0.05,
    "dispatch_poll_seconds": 0.05,
    "watchdog_interval_seconds": 0.1,
    "max_in_flight": 4,
}


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class DestinationController:
    """Wraps the real ``write_conversion_output`` with per-job faults.

    Unfaulted jobs hit the real production writer unchanged; faulted
    jobs get deterministic failure or a held hang gate before the real
    write runs.
    """

    def __init__(self) -> None:
        self._real = tm_module.write_conversion_output
        self.fail_calls: dict[str, int] = {}
        self.fail_forever: set[str] = set()
        self.hang_calls: dict[str, int] = {}
        self.hang_gate = threading.Event()
        self.hang_gate.set()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, result: dict, **kwargs: Any):
        job = str(kwargs.get("job_id") or "")
        hang = False
        with self._lock:
            self.calls.append(job)
            remaining = self.fail_calls.get(job, 0)
            if remaining > 0:
                self.fail_calls[job] = remaining - 1
                raise OSError(f"injected destination failure for {job}")
            if job in self.fail_forever:
                raise OSError(f"injected destination failure for {job}")
            hang_remaining = self.hang_calls.get(job, 0)
            if hang_remaining > 0:
                self.hang_calls[job] = hang_remaining - 1
                hang = True
        if hang:
            self.hang_gate.wait(timeout=30.0)
        return self._real(result, **kwargs)

    def call_count(self, job: str) -> int:
        with self._lock:
            return self.calls.count(job)


class ScriptedConversionService:
    """Thread-backend service with per-job scripted behavior.

    Job identity is the source filename stem (``_make_job`` names each
    source ``<job_id>.pdf``). Behaviors compose in priority order:
    always-raise, fail-first-N, held gate, slow activity loop.
    """

    def __init__(self) -> None:
        self.gates: dict[str, threading.Event] = {}
        self.fail_first: dict[str, int] = {}
        self.raise_always: dict[str, BaseException] = {}
        self.slow_seconds: dict[str, float] = {}
        self.calls: dict[str, int] = {}
        self.activity: Any = None  # optional callable(job_id)
        self._lock = threading.Lock()

    def plan(self, filepath: str, config: dict) -> Any:
        return SimpleNamespace(execution_backend="cpu_thread")

    def supports_multiple_formats(self, filepath: str, config: dict) -> bool:
        return False

    def gate(self, job_id: str) -> threading.Event:
        with self._lock:
            event = self.gates.get(job_id)
            if event is None:
                event = threading.Event()
                self.gates[job_id] = event
            return event

    def convert_file(self, filepath: str, config: dict) -> dict:
        job = Path(filepath).stem
        with self._lock:
            self.calls[job] = self.calls.get(job, 0) + 1
            error = self.raise_always.get(job)
            fail_remaining = self.fail_first.get(job, 0)
            if fail_remaining > 0:
                self.fail_first[job] = fail_remaining - 1
        if error is not None:
            raise error
        if fail_remaining > 0:
            raise RuntimeError(f"injected conversion failure for {job}")
        gate = self.gates.get(job)
        if gate is not None:
            gate.wait(timeout=30.0)
        slow = self.slow_seconds.get(job)
        if slow:
            deadline = time.monotonic() + slow
            while time.monotonic() < deadline:
                if self.activity is not None:
                    self.activity(job)
                time.sleep(0.05)
        return json.loads(json.dumps(FAKE_RESULT))


def _model_service_child(release: Any, result_path: str, payload: str) -> None:
    """REAL child process standing in for the model service worker."""
    release.wait(timeout=60.0)
    Path(result_path).write_text(payload, encoding="utf-8")


class ModelServiceHarness(ScriptedConversionService):
    """Scripted service whose model jobs run in a real child process.

    ``model_jobs`` route through a spawned process that writes its
    result to disk. The parent side raises on any nonzero exit —
    exactly what a worker wrapper must do when its service dies. For
    ``crash_first`` jobs the first run terminates the child before it
    can produce anything (real process death, not a raised flag).
    """

    def __init__(self) -> None:
        super().__init__()
        self.model_jobs: set[str] = set()
        self.crash_first: set[str] = set()
        self._crashed: set[str] = set()
        self.proc: Any = None
        self._payload = json.dumps(FAKE_RESULT)

    def convert_file(self, filepath: str, config: dict) -> dict:
        job = Path(filepath).stem
        if job not in self.model_jobs:
            return super().convert_file(filepath, config)
        with self._lock:
            crash = job in self.crash_first and job not in self._crashed
            self._crashed.add(job)
        ctx = mp.get_context("spawn")
        release = ctx.Event()
        tmpdir = tempfile.mkdtemp(prefix=f"model-{job}-")
        result_path = str(Path(tmpdir) / "result.json")
        proc = ctx.Process(
            target=_model_service_child,
            args=(release, result_path, self._payload),
        )
        proc.start()
        self.proc = proc
        if crash:
            proc.terminate()
            proc.join(timeout=10.0)
        else:
            release.set()
            proc.join(timeout=30.0)
        if proc.exitcode != 0:
            raise RuntimeError(f"model service worker died (exit={proc.exitcode})")
        return json.loads(Path(result_path).read_text(encoding="utf-8"))


def _release_gates(service: Any) -> None:
    if isinstance(service, ScriptedConversionService):
        for gate in service.gates.values():
            gate.set()


async def _manager_quiesced(env: SimpleNamespace) -> bool:
    """No live kernel claims and no unfinished executor futures."""
    manager = env.manager
    if getattr(manager, "_kernel_claims", None):
        return False
    futures = getattr(manager, "_tasks", None) or {}
    return all(f.done() for f in futures.values())


async def _teardown_env(env: SimpleNamespace) -> None:
    """Quiesce before tearing down.

    Releasing the service gates first lets held workers run to their
    terminal paths; a short bounded wait then drains executor futures
    and kernel claims so ``engine.dispose()`` never races a finalize
    coroutine that is about to check out a connection. The dispose
    itself is bounded: a wedged checkout must delay one test, never
    hang the suite.
    """
    _release_gates(env.service)
    try:
        await _wait_until(lambda: _manager_quiesced(env), timeout=5)
    except AssertionError:
        pass  # bounded best effort; stop() below is the hard barrier
    env.coordinator.stop()
    env.manager.shutdown()
    try:
        await asyncio.wait_for(env.engine.dispose(), timeout=10)
    except TimeoutError:
        pass


@asynccontextmanager
async def failure_plane_stack(
    tmp_path: Path,
    backend: str,
    service: Any,
    monkeypatch: pytest.MonkeyPatch,
    **coordinator_overrides: Any,
):
    """Migrated DB + real TaskManager + coordinator with tunable knobs.

    The whole environment lives inside one ``provisioned_database``
    context (PostgreSQL lanes drop the throwaway database on exit).
    """
    from app.db_migration import upgrade_database

    overrides = {**_COORDINATOR_DEFAULTS, **coordinator_overrides}
    async with provisioned_database(
        backend, (tmp_path / "failure-plane.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        engine_kwargs = engine_kwargs_for(backend)
        if backend == "sqlite":
            engine_kwargs["connect_args"]["timeout"] = 30
        engine = create_async_engine(prov.url, **engine_kwargs)
        assert engine.dialect.name == backend
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(tm_module, "async_session_factory", factory)
        # Executor parallelism aligned with max_in_flight: claimed work
        # must start without a liveness-blind pool queue wait (see the
        # warning in start_kernel_runtime).
        manager = TaskManager(max_workers=4)
        coordinator = manager.start_kernel_runtime(
            service,
            session_factory=factory,
            commit_service=KernelCommitService(factory),
            **overrides,
        )
        env = SimpleNamespace(
            manager=manager,
            coordinator=coordinator,
            factory=factory,
            engine=engine,
            service=service,
            tmp_path=tmp_path,
            backend=backend,
        )
        try:
            yield env
        finally:
            await _teardown_env(env)


@pytest_asyncio.fixture(params=BACKENDS, ids=BACKENDS)
async def failure_plane_env(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async with failure_plane_stack(
        tmp_path, request.param, ScriptedConversionService(), monkeypatch
    ) as env:
        yield env


@pytest_asyncio.fixture(params=BACKENDS, ids=BACKENDS)
async def pressure_env(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Failure-plane environment with calmer loop intervals.

    The single-job lanes run hyper-tuned intervals (50 ms renewals);
    with 8-12 concurrent claims that rate pushes SQLite far past its
    write envelope and the resulting flaps are contention, not
    semantics. Production defaults are 5 s renew / 0.25 s poll / 15 s
    watchdog — these remain an order of magnitude faster while keeping
    the write pressure realistic.
    """
    async with failure_plane_stack(
        tmp_path,
        request.param,
        ScriptedConversionService(),
        monkeypatch,
        renew_interval_seconds=0.2,
        dispatch_poll_seconds=0.1,
        watchdog_interval_seconds=0.25,
    ) as env:
        yield env


def _install_destination_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> DestinationController:
    controller = DestinationController()
    monkeypatch.setattr(tm_module, "write_conversion_output", controller)
    return controller


async def _leases_in_flight(env: SimpleNamespace) -> list[KernelWorkLease]:
    async with env.factory() as session:
        return (
            (
                await session.execute(
                    select(KernelWorkLease).where(
                        KernelWorkLease.workspace_id == WORKSPACE,
                        KernelWorkLease.state == "leased",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _all_rows_terminal(env: SimpleNamespace, *job_ids: str) -> bool:
    for job_id in job_ids:
        row = await _row(env, job_id)
        if row is None or row.status not in ("completed", "failed", "cancelled"):
            return False
    return True


async def _retry_events(env: SimpleNamespace, job_id: str) -> list:
    events = await _events(env, "work.retry")
    return [e for e in events if e.payload.get("job_id") == job_id]


# ---------------------------------------------------------------------------
# destination isolation (external-effect failures)
# ---------------------------------------------------------------------------


class TestDestinationIsolation:
    pytestmark = pytest.mark.asyncio

    async def test_hung_destination_does_not_block_unrelated_work(
        self, failure_plane_env, monkeypatch
    ):
        """Head-of-line blocking contract.

        A destination that hangs mid-write (network-stall analog) may
        delay ITS job, but the shared runtime loop must stay live:
        dispatch, renewal, watchdog, and an unrelated healthy job's
        complete lifecycle (including its own destination write) all
        proceed while the faulty destination is still hung.
        """
        env = failure_plane_env
        controller = _install_destination_controller(monkeypatch)
        controller.hang_calls = {"job-hang": 1}
        controller.hang_gate.clear()

        await _submit(env, "job-hang")
        await _submit(env, "job-healthy")
        env.coordinator.start()

        # The healthy job must complete WHILE the hang gate is still closed.
        healthy = await _wait_for_row_status(env, "job-healthy", "completed", timeout=20)
        assert healthy.status == "completed"
        assert controller.call_count("job-hang") == 1  # hang really entered

        controller.hang_gate.set()
        hung = await _wait_for_row_status(env, "job-hang", "completed", timeout=20)
        assert hung.status == "completed"

        publication = await _publication(env, (await _outbox_rows(env))[0].id)
        assert publication is not None

    async def test_deterministic_destination_failure_is_terminal_truth(
        self, failure_plane_env, monkeypatch
    ):
        """A destination that always fails (budget 0) is an honest
        terminal failure: no publication, no fake completion, and the
        healthy peer is untouched."""
        env = failure_plane_env
        controller = _install_destination_controller(monkeypatch)
        controller.fail_forever = {"job-dead"}

        await _submit(env, "job-dead")
        await _submit(env, "job-healthy")
        env.coordinator.start()

        dead = await _wait_for_row_status(env, "job-dead", "failed", timeout=20)
        assert "injected destination failure" in (dead.error_message or "")

        healthy = await _wait_for_row_status(env, "job-healthy", "completed", timeout=20)
        assert healthy.status == "completed"

        publications = []
        for view in await _outbox_rows(env):
            pub = await _publication(env, view.id)
            if pub is not None:
                publications.append(pub)
        assert len(publications) == 1  # only the healthy job published

    async def test_transient_destination_failure_retries_and_converges(
        self, failure_plane_env, monkeypatch
    ):
        """One failed write inside the retry budget: the redelivery
        re-executes the effect (at-least-once transport, honestly
        observable), and acceptance is still exactly-once."""
        env = failure_plane_env
        controller = _install_destination_controller(monkeypatch)
        controller.fail_calls = {"job-flaky": 1}

        await _submit(env, "job-flaky", {"max_retries": 1})
        env.coordinator.start()

        row = await _run_to_completion(env, "job-flaky")
        assert row.status == "completed", f"err={row.error_message!r}"
        assert controller.call_count("job-flaky") == 2  # redelivery visible

        views = await _outbox_rows(env)
        assert len(views) == 1
        assert views[0].state == OUTBOX_STATE_DONE
        assert views[0].attempts == 1  # durable, inspectable retry accounting

        retry_events = await _retry_events(env, "job-flaky")
        assert len(retry_events) == 1
        assert retry_events[0].payload["attempts"] == 1

        publication = await _publication(env, views[0].id)
        assert publication is not None  # exactly one accepted truth

    async def test_natural_lease_lapse_during_hung_destination(
        self, tmp_path: Path, monkeypatch
    ):
        """A hung destination rides out a REAL lease timeout (no forced
        SQL expiry): renewal stops for lack of activity evidence, the
        watchdog takes over, the re-executed delivery converges, and
        the superseded generation's late finalize can never publish."""
        controller = _install_destination_controller(monkeypatch)
        controller.hang_calls = {"job-lapse": 1}
        controller.hang_gate.clear()

        async with failure_plane_stack(
            tmp_path,
            "sqlite",
            ScriptedConversionService(),
            monkeypatch,
            lease_seconds=0.8,
        ) as env:
            await _submit(env, "job-lapse", {"max_retries": 1})
            env.coordinator.start()

            async def _lapse_observed():
                retries = await _retry_events(env, "job-lapse")
                return any(e.payload.get("error") == "lease lapsed" for e in retries)

            # Takeover happens on the loop WHILE the destination write
            # is still hung — that is the entire point.
            assert await _wait_until(_lapse_observed, timeout=15)

            row = await _wait_for_row_status(
                env, "job-lapse", "completed", "failed", timeout=25
            )
            assert row.status == "completed"

            views = await _outbox_rows(env)
            assert len(views) == 1
            assert views[0].state == OUTBOX_STATE_DONE
            assert views[0].attempts >= 1
            publication = await _publication(env, views[0].id)
            assert publication is not None
            assert publication.fencing_token >= 2  # takeover advanced the fence

            # Release the zombie write: its late finalize is fenced out
            # and can never create a second publication.
            controller.hang_gate.set()
            await asyncio.sleep(0.5)
            pubs = []
            for view in await _outbox_rows(env):
                pub = await _publication(env, view.id)
                if pub is not None:
                    pubs.append(pub)
            assert len(pubs) == 1
            assert pubs[0].publication_id == publication.publication_id


# ---------------------------------------------------------------------------
# acceptance-boundary interruptions
# ---------------------------------------------------------------------------


class TestAcceptanceBoundaryInterruptions:
    pytestmark = pytest.mark.asyncio

    async def test_acceptance_outage_then_recovery_converges(
        self, failure_plane_env, monkeypatch
    ):
        """Database outage exactly at the accept-result boundary (the
        gap between destination success and durable acceptance): the
        attempt fails honestly into the retry budget, the effect is
        redelivered (write happens twice), and exactly one accepted
        publication survives."""
        env = failure_plane_env
        controller = _install_destination_controller(monkeypatch)
        real_accept = env.coordinator.accept_result
        outage = {"remaining": 1}

        async def flaky_accept(claim, descriptor):
            if outage["remaining"] > 0:
                outage["remaining"] -= 1
                raise ConnectionError("kernel database unreachable")
            return await real_accept(claim, descriptor)

        monkeypatch.setattr(env.coordinator, "accept_result", flaky_accept)

        await _submit(env, "job-gap", {"max_retries": 1})
        env.coordinator.start()

        row = await _run_to_completion(env, "job-gap")
        assert row.status == "completed", f"err={row.error_message!r}"
        assert controller.call_count("job-gap") == 2  # at-least-once redelivery

        views = await _outbox_rows(env)
        assert views[0].state == OUTBOX_STATE_DONE
        assert views[0].attempts == 1
        assert await _publication(env, views[0].id) is not None

    async def test_persistent_acceptance_outage_is_terminal_not_fake_success(
        self, failure_plane_env, monkeypatch
    ):
        """An acceptance boundary that never recovers (budget 0) is a
        truthful terminal failure: no publication, no completed row,
        outbox acked behind the terminal event."""
        env = failure_plane_env

        async def dead_accept(claim, descriptor):
            raise ConnectionError("kernel database unreachable")

        monkeypatch.setattr(env.coordinator, "accept_result", dead_accept)

        await _submit(env, "job-outage")
        env.coordinator.start()

        row = await _run_to_completion(env, "job-outage")
        assert row.status == "failed"
        views = await _outbox_rows(env)
        assert views[0].state == OUTBOX_STATE_DONE
        assert await _publication(env, views[0].id) is None


# ---------------------------------------------------------------------------
# cancellation durability
# ---------------------------------------------------------------------------


class TestCancellationDurability:
    pytestmark = pytest.mark.asyncio

    async def test_cancelled_work_survives_manager_recreation(
        self, tmp_path: Path, monkeypatch
    ):
        """Cancellation is durable truth: after a full manager/coordinator
        restart, recover() neither resurrects the work as pending nor
        lets it re-execute; the row and the outbox stay terminal."""
        service = ScriptedConversionService()
        controller = _install_destination_controller(monkeypatch)
        async with failure_plane_stack(
            tmp_path, "sqlite", service, monkeypatch
        ) as env:
            gate = service.gate("job-cancel")
            await _submit(env, "job-cancel")
            env.coordinator.start()
            await _wait_for_row_status(env, "job-cancel", "processing", timeout=20)

            assert await env.manager.cancel_job("job-cancel") is True
            row = await _row(env, "job-cancel")
            assert row.status == "cancelled", f"err={row.error_message!r}"

            # Full runtime death and rebirth on the same durable truth.
            env.coordinator.stop()
            env.manager.shutdown()
            gate.set()

            manager2 = TaskManager()
            coordinator2 = manager2.start_kernel_runtime(
                service,
                session_factory=env.factory,
                commit_service=KernelCommitService(env.factory),
                **_COORDINATOR_DEFAULTS,
            )
            try:
                await coordinator2.recover()
                assert (await _row(env, "job-cancel")).status == "cancelled"

                views = await _outbox_rows(env)
                assert len(views) == 1
                assert views[0].state == OUTBOX_STATE_DONE
                assert await _publication(env, views[0].id) is None

                # No dispatch of the cancelled work after recovery.
                coordinator2.start()
                await asyncio.sleep(0.3)
                assert (await _row(env, "job-cancel")).status == "cancelled"
                assert controller.call_count("job-cancel") == 0
            finally:
                coordinator2.stop()
                manager2.shutdown()


# ---------------------------------------------------------------------------
# model-service crash (literal child-process death)
# ---------------------------------------------------------------------------


class TestModelServiceCrash:
    pytestmark = pytest.mark.asyncio

    async def test_external_model_service_kill_is_truthful_and_isolated(
        self, tmp_path: Path, monkeypatch
    ):
        """A REAL model-service process killed mid-work (OOM-kill
        analog): the wrapper detects the death, the kernel records an
        honest terminal failure, no truth is fabricated, and an
        unrelated healthy job completes normally."""
        service = ModelServiceHarness()
        service.model_jobs = {"job-model"}
        _install_destination_controller(monkeypatch)
        async with failure_plane_stack(
            tmp_path, "sqlite", service, monkeypatch
        ) as env:
            await _submit(env, "job-model")
            await _submit(env, "job-healthy")
            env.coordinator.start()

            # Wait until the child model process is really alive, kill it.
            await _wait_until(lambda: service.proc is not None, timeout=20)
            await _wait_until(
                lambda: service.proc is not None and service.proc.is_alive(),
                timeout=10,
            )
            service.proc.terminate()

            row = await _wait_for_row_status(env, "job-model", "failed", timeout=25)
            assert "model service worker died" in (row.error_message or "")
            healthy = await _wait_for_row_status(
                env, "job-healthy", "completed", timeout=20
            )
            assert healthy.status == "completed"

            for view in await _outbox_rows(env):
                if view.payload.get("job_id") == "job-model":
                    assert view.state == OUTBOX_STATE_DONE
                    assert await _publication(env, view.id) is None

    async def test_crash_then_retry_converges_exactly_once(
        self, tmp_path: Path, monkeypatch
    ):
        """A model-service crash inside the retry budget: the retry
        runs the model again, converges, and exactly one accepted
        publication exists for the work."""
        service = ModelServiceHarness()
        service.model_jobs = {"job-crashy"}
        service.crash_first = {"job-crashy"}
        async with failure_plane_stack(
            tmp_path, "sqlite", service, monkeypatch
        ) as env:
            await _submit(env, "job-crashy", {"max_retries": 1})
            env.coordinator.start()

            row = await _run_to_completion(env, "job-crashy")
            assert row.status == "completed", f"err={row.error_message!r}"

            views = await _outbox_rows(env)
            assert views[0].state == OUTBOX_STATE_DONE
            assert views[0].attempts == 1
            publication = await _publication(env, views[0].id)
            assert publication is not None


# ---------------------------------------------------------------------------
# shared-memory pressure
# ---------------------------------------------------------------------------


class TestSharedMemoryPressure:
    pytestmark = pytest.mark.asyncio

    async def test_shared_memory_exhaustion_fails_truthfully(
        self, failure_plane_env
    ):
        """The shared-memory exhaustion class (allocation failure from
        the converter): honest terminal failure, error recorded, peer
        unaffected, runtime unwedged."""
        env = failure_plane_env
        env.service.raise_always = {
            "job-mem": MemoryError(
                "unable to allocate 512 MiB for layout model: shared memory exhausted"
            )
        }
        await _submit(env, "job-mem")
        await _submit(env, "job-healthy")
        env.coordinator.start()

        row = await _wait_for_row_status(env, "job-mem", "failed", timeout=20)
        assert "shared memory exhausted" in (row.error_message or "")
        healthy = await _wait_for_row_status(env, "job-healthy", "completed", timeout=20)
        assert healthy.status == "completed"


# ---------------------------------------------------------------------------
# pressure and concurrency
# ---------------------------------------------------------------------------


class TestPressure:
    pytestmark = pytest.mark.asyncio

    async def test_max_in_flight_cap_never_oversubscribes(
        self, pressure_env
    ):
        """Twice the fan-out cap in held conversions: active fenced
        ownership never exceeds the cap; the rest stay pending; release
        converges every job to its own single publication."""
        env = pressure_env
        job_ids = [f"job-cap-{i}" for i in range(8)]
        for job_id in job_ids:
            env.service.gate(job_id)  # held
        for job_id in job_ids:
            await _submit(env, job_id)
        env.coordinator.start()

        async def _in_flight_count():
            rows = await _outbox_rows(env)
            return len([r for r in rows if r.state == OUTBOX_STATE_IN_FLIGHT])

        # Cap reached and stable: never above it, settled at it.
        async def _at_cap():
            return (await _in_flight_count()) >= 4

        assert await _wait_until(_at_cap, timeout=20)

        async def _stable_at_cap():
            for _ in range(3):
                if await _in_flight_count() != 4:
                    return False
                await asyncio.sleep(0.15)
            return True

        assert await _wait_until(_stable_at_cap, timeout=15)
        processing = []
        for job_id in job_ids:
            row = await _row(env, job_id)
            if row is not None and row.status == "processing":
                processing.append(job_id)
        assert len(processing) == 4
        assert len(await _leases_in_flight(env)) == 4

        for job_id in job_ids:
            env.service.gate(job_id).set()

        for job_id in job_ids:
            row = await _wait_for_row_status(env, job_id, "completed", timeout=30)
            assert row.status == "completed"

        publications = []
        for view in await _outbox_rows(env):
            pub = await _publication(env, view.id)
            assert pub is not None
            publications.append(pub)
        assert len(publications) == 8
        assert len({p.work_id for p in publications}) == 8
        assert all(v.state == OUTBOX_STATE_DONE for v in await _outbox_rows(env))

    async def test_mixed_workload_confluence(self, pressure_env):
        """One coordinator, one database, four behavior classes at once:
        fast, gated-slow, transient-failure (budget 1), hard failure
        (budget 0), and a mid-flight cancellation. Every job reaches a
        truthful terminal state; completed jobs each own exactly one
        publication; retries are durable and explainable; the slow and
        failing peers never contaminate the fast ones."""
        env = pressure_env
        fast = [f"job-fast-{i}" for i in range(6)]
        slow = [f"job-slow-{i}" for i in range(2)]
        transient = [f"job-transient-{i}" for i in range(2)]
        hard = ["job-hard"]
        cancel = ["job-cancel"]
        for job_id in slow:
            env.service.gate(job_id)
        for job_id in transient:
            env.service.fail_first[job_id] = 1
        env.service.raise_always[hard[0]] = RuntimeError("hard converter failure")
        env.service.gate(cancel[0])

        for job_id in fast + slow + transient + hard + cancel:
            await _submit(
                env, job_id, {"max_retries": 1} if job_id in transient else None
            )
        env.coordinator.start()

        # 1) Fast work completes while the slow gates are still held.
        # Coarse polling: this suite's semantic census, not a latency
        # probe — hammering SQLite at 20 Hz with 12 live rows only
        # starves the very dispatch being observed.
        async def _fast_done():
            rows = [await _row(env, job_id) for job_id in fast]
            return all(r is not None and r.status == "completed" for r in rows)

        assert await _wait_until(_fast_done, timeout=45, interval=0.25)

        # 2) Release the slow gates so the queue drains past them (the
        # fan-out cap deliberately holds later work while they run).
        for job_id in slow:
            env.service.gate(job_id).set()

        # 3) Cancel the held job mid-flight.
        async def _cancel_claimed():
            row = await _row(env, cancel[0])
            return row is not None and row.status == "processing"

        assert await _wait_until(_cancel_claimed, timeout=45, interval=0.25)
        assert await env.manager.cancel_job(cancel[0]) is True

        all_jobs = fast + slow + transient + hard + cancel
        assert await _wait_until(
            lambda: _all_rows_terminal(env, *all_jobs), timeout=45, interval=0.25
        )
        rows = {job_id: await _row(env, job_id) for job_id in all_jobs}
        assert all(rows[j].status == "completed" for j in fast + slow + transient)
        assert rows[hard[0]].status == "failed"
        assert rows[cancel[0]].status == "cancelled"

        by_job = {}
        for view in await _outbox_rows(env):
            by_job[view.payload.get("job_id")] = view
        assert all(v.state == OUTBOX_STATE_DONE for v in by_job.values())

        publications = {}
        for view in await _outbox_rows(env):
            pub = await _publication(env, view.id)
            if pub is not None:
                publications[view.payload.get("job_id")] = pub
        assert set(publications) == set(fast + slow + transient)
        for job_id in transient:
            assert by_job[job_id].attempts == 1  # retry accounting exact

    async def test_renewals_hold_under_concurrent_activity(
        self, tmp_path: Path, monkeypatch
    ):
        """Four long conversions with real activity evidence under a
        short lease: evidence-backed renewals keep every lease alive —
        zero lapse-retries — and all work completes."""
        service = ScriptedConversionService()
        async with failure_plane_stack(
            tmp_path,
            "sqlite",
            service,
            monkeypatch,
            lease_seconds=1.5,
            renew_interval_seconds=0.1,
        ) as env:
            service.activity = env.manager._kernel_note_activity
            job_ids = [f"job-renew-{i}" for i in range(4)]
            # Work outlives the lease by ~1s, so survival REQUIRES
            # evidence-backed renewals; the 1.5s window gives a
            # 15-tick margin against renewal skips under contention.
            for job_id in job_ids:
                service.slow_seconds[job_id] = 2.5
            for job_id in job_ids:
                await _submit(env, job_id)
            env.coordinator.start()

            for job_id in job_ids:
                row = await _wait_for_row_status(env, job_id, "completed", timeout=30)
                assert row.status == "completed"

            lapse_retries = [
                e
                for e in await _events(env, "work.retry")
                if e.payload.get("error") == "lease lapsed"
            ]
            assert lapse_retries == []  # activity kept every lease alive
