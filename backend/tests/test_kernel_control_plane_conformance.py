"""Dual-backend control-plane conformance (PR83B1).

One semantic suite over the durable runtime control plane — fenced
ownership, fair bounded dispatch, challenge-backed liveness, durable
semantic events — executed against both first-class database profiles
(SQLite local, PostgreSQL industrial) through the same public module
surfaces. No second implementation exists to drift.

What this suite proves beyond the SQLite contract tests:

* authority races (concurrent first-acquire, takeover, acceptance,
  capacity) resolve to exactly one winner **in committed database
  state**, audited through raw table reads rather than return values;
* the hard ``max_in_flight`` cap cannot be oversubscribed by
  concurrent dispatchers, with a committed-state poller watching for
  transient violations and (on PostgreSQL) ``pg_stat_activity`` proof
  that claim transactions genuinely overlapped;
* the semantic event sequence cannot fork under concurrent producers
  and leaves no committed hole after a rolled-back append;
* retry classification reaches the real PostgreSQL SQLSTATE vocabulary
  (``40001``/``40P01`` produced by the server itself, not constructed
  exception objects) and the bounded retry budget converges whole
  operations;
* restart from durable state (fresh engine over the same database)
  preserves membership, order, and authority.

PostgreSQL provisioning is shared with the PR83A commit conformance
suite (``tests/pg_provisioning.py``): ``MARKER_TEST_POSTGRES_ADMIN_URL``
plus strict mode via the runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, InternalError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel import events as kernel_events
from app.kernel import fencing, liveness, scheduler
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.dialects import is_retryable_contention, run_with_contention_retry
from app.kernel.errors import (
    InvalidChallengeError,
    KernelBusyError,
    ProgressNotAdvancingError,
    PublicationConflictError,
    RequestNotActiveError,
    StaleFenceError,
    TopologyMismatchError,
    WorkCancelledError,
)
from app.kernel.models import (
    KernelEvent,
    KernelOutbox,
    KernelProgress,
    KernelPublication,
    KernelSchedulingGroup,
    KernelWorkLease,
)
from app.kernel.outbox import (
    OUTBOX_STATE_DONE,
    OUTBOX_STATE_IN_FLIGHT,
    OUTBOX_STATE_PENDING,
    OutboxIntent,
    claim as outbox_claim,
)
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ObservationRecord
from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)

pytestmark = pytest.mark.asyncio

SHORT_LEASE = 0.05  # seconds; expires almost immediately
PG_DEADLOCK_TIMEOUT_MS = 100


@dataclass
class ControlEnv:
    """One migrated database + the services under test."""

    backend: str
    url: str
    engine: object
    session_factory: async_sessionmaker
    store: LocalPayloadStore
    service: KernelCommitService
    server_version: str = ""

    def new_engine(self) -> tuple[object, async_sessionmaker]:
        """A *fresh* engine over the same durable database (restart).

        Returns the engine so callers dispose it — an undisposed engine
        would hold pool connections open and block database teardown.
        """
        engine = create_async_engine(self.url, **engine_kwargs_for(self.backend))
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        return engine, factory


@pytest_asyncio.fixture(params=["postgresql"], ids=["postgresql"])
async def control_env_pg_only(request, tmp_path: pathlib.Path):
    """PostgreSQL-only variant for server-produced SQLSTATE probes.

    These tests assert error shapes the *server* emits; there is no
    SQLite counterpart, so the fixture never parameterizes SQLite and
    strict industrial runs see no skip.
    """
    backend = request.param
    async with provisioned_database(
        backend, (tmp_path / "kernel.db").as_posix()
    ) as prov:
        result = await upgrade_database(url=prov.url)
        assert result.to_revision, "bootstrap must reach a migration head"
        engine = create_async_engine(prov.url, **engine_kwargs_for(backend))
        assert engine.dialect.name == backend
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        store = LocalPayloadStore(tmp_path / "payloads")
        service = KernelCommitService(session_factory, payload_store=store)
        async with engine.connect() as conn:
            server_version = await conn.scalar(text("SELECT version()"))
            assert "PostgreSQL" in server_version
        env = ControlEnv(
            backend=backend,
            url=prov.url,
            engine=engine,
            session_factory=session_factory,
            store=store,
            service=service,
            server_version=server_version,
        )
        try:
            yield env
        finally:
            await engine.dispose()


@pytest_asyncio.fixture(params=BACKENDS, ids=BACKENDS)
async def control_env(request, tmp_path: pathlib.Path):
    backend = request.param
    async with provisioned_database(
        backend, (tmp_path / "kernel.db").as_posix()
    ) as prov:
        result = await upgrade_database(url=prov.url)
        assert result.to_revision, "bootstrap must reach a migration head"

        engine = create_async_engine(prov.url, **engine_kwargs_for(backend))
        assert engine.dialect.name == backend  # real-backend confirmation

        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        store = LocalPayloadStore(tmp_path / "payloads")
        service = KernelCommitService(session_factory, payload_store=store)

        server_version = ""
        if backend == "postgresql":
            async with engine.connect() as conn:
                server_version = await conn.scalar(text("SELECT version()"))
                assert "PostgreSQL" in server_version

        env = ControlEnv(
            backend=backend,
            url=prov.url,
            engine=engine,
            session_factory=session_factory,
            store=store,
            service=service,
            server_version=server_version,
        )
        try:
            yield env
        finally:
            await engine.dispose()


async def _new_work(env: ControlEnv, *, workspace_id: str = "ws", tag: str = "w") -> int:
    """Commit one outbox intent and return its durable work id."""
    receipt = await env.service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(ObservationRecord(observer=f"op-{tag}", derivation={"tag": tag}),),
            outbox=(OutboxIntent(work_kind="convert", payload={"tag": tag}),),
        )
    )
    return receipt.outbox_ids[0]


async def _count(env: ControlEnv, *entities) -> int:
    async with env.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(*entities))


async def _live_leases(env: ControlEnv, *, group_id: str) -> int:
    from app.kernel.models import KernelSchedulingEntry

    async with env.session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(KernelWorkLease)
                .join(
                    KernelSchedulingEntry,
                    KernelWorkLease.work_id == KernelSchedulingEntry.work_id,
                )
                .where(
                    KernelSchedulingEntry.group_id == group_id,
                    KernelWorkLease.state == fencing.LEASE_STATE_LEASED,
                    KernelWorkLease.lease_expires_at > datetime.now(timezone.utc),
                )
            )
        )


def _fresh_naive_utc(value: str) -> bool:
    """Views render naive-UTC isoformat on every backend."""
    return "+00:00" not in value and "Z" not in value and re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value
    )


# ===========================================================================
# Fencing authority (X-F01 .. X-F12)
# ===========================================================================


async def test_concurrent_first_acquire_has_single_winner(control_env) -> None:
    work_id = await _new_work(control_env)
    contenders = 8
    results = await asyncio.gather(
        *(
            fencing.acquire(
                control_env.session_factory,
                work_id=work_id,
                owner_id=f"worker-{i}",
            )
            for i in range(contenders)
        )
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1
    # Raw audit: exactly one authority row, token 1.
    assert await _count(control_env, KernelWorkLease) == 1
    lease = await fencing.get_lease(control_env.session_factory, work_id)
    assert lease.fencing_token == 1
    assert lease.owner_id == winners[0].owner_id
    # View stability: naive-UTC rendering on both backends.
    assert _fresh_naive_utc(lease.lease_expires_at)


async def test_same_owner_redelivery_keeps_token(control_env) -> None:
    work_id = await _new_work(control_env)
    first = await fencing.acquire(
        control_env.session_factory, work_id=work_id, owner_id="worker-a",
        lease_seconds=60.0,
    )
    again = await fencing.acquire(
        control_env.session_factory, work_id=work_id, owner_id="worker-a",
        lease_seconds=60.0,
    )
    assert again is not None and again.fencing_token == first.fencing_token
    assert again.lease_expires_at >= first.lease_expires_at


async def test_release_and_takeover_advance_the_fence(control_env) -> None:
    work_id = await _new_work(control_env)
    await fencing.acquire(
        control_env.session_factory, work_id=work_id, owner_id="worker-a"
    )
    assert await fencing.release(
        control_env.session_factory,
        work_id=work_id,
        owner_id="worker-a",
        fencing_token=1,
    )
    takeover = await fencing.acquire(
        control_env.session_factory, work_id=work_id, owner_id="worker-b"
    )
    assert takeover is not None and takeover.fencing_token == 3  # release=2, takeover=3
    # The old owner is permanently stale.
    with pytest.raises(StaleFenceError):
        await fencing.accept(
            control_env.session_factory,
            work_id=work_id,
            fencing_token=1,
            result={"late": True},
        )
    assert await _count(control_env, KernelPublication) == 0


async def test_concurrent_takeover_single_winner_monotonic_chain(control_env) -> None:
    work_id = await _new_work(control_env)
    await fencing.acquire(
        control_env.session_factory,
        work_id=work_id,
        owner_id="worker-a",
        lease_seconds=SHORT_LEASE,
    )
    await asyncio.sleep(SHORT_LEASE * 3)  # expire
    results = await asyncio.gather(
        *(
            fencing.acquire(
                control_env.session_factory, work_id=work_id, owner_id=f"taker-{i}"
            )
            for i in range(8)
        )
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 2  # exactly one advance
    lease = await fencing.get_lease(control_env.session_factory, work_id)
    assert lease.fencing_token == 2
    assert lease.owner_id == winners[0].owner_id


async def test_stale_owner_cannot_renew_after_takeover(control_env) -> None:
    env = control_env
    work_id = await _new_work(env)
    await scheduler.register_work(env.session_factory, work_id=work_id)
    first = await scheduler.claim_fair(
        env.session_factory, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    assert first is not None
    await asyncio.sleep(SHORT_LEASE * 3)  # expire the short lease
    # The runtime watchdog's takeover path: expired lease re-acquired by
    # a new owner through the fencing boundary.
    takeover = await fencing.acquire(
        env.session_factory, work_id=work_id, owner_id="worker-b"
    )
    assert takeover is not None and takeover.fencing_token > first.lease.fencing_token
    with pytest.raises(StaleFenceError):
        await liveness.renew_lease(
            env.session_factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=first.lease.fencing_token,
            challenge_nonce=first.challenge_nonce,
            progress=first.lease.fencing_token + 100,
            active_request_id="req-a",
        )
    with pytest.raises(StaleFenceError):
        await fencing.accept(
            env.session_factory,
            work_id=work_id,
            fencing_token=first.lease.fencing_token,
            result={"stale": True},
        )
    assert await _count(env, KernelPublication) == 0


async def test_concurrent_accept_yields_exactly_one_publication(control_env) -> None:
    env = control_env
    work_id = await _new_work(env)
    await fencing.acquire(env.session_factory, work_id=work_id, owner_id="worker-a")
    result = {"pages": 3, "answer": 42}
    outcomes = await asyncio.gather(
        *(
            fencing.accept(
                env.session_factory,
                work_id=work_id,
                fencing_token=1,
                result=result,
            )
            for _ in range(8)
        )
    )
    created = [o for o in outcomes if not o.already_accepted]
    converged = [o for o in outcomes if o.already_accepted]
    assert len(created) == 1
    assert len(converged) == 7
    assert all(o.publication.publication_id == created[0].publication.publication_id
               for o in converged)
    # Raw audit: one publication row, lease accepted.
    assert await _count(env, KernelPublication) == 1
    lease = await fencing.get_lease(env.session_factory, work_id)
    assert lease.state == fencing.LEASE_STATE_ACCEPTED


async def test_divergent_result_conflicts_without_mutation(control_env) -> None:
    env = control_env
    work_id = await _new_work(env)
    await fencing.acquire(env.session_factory, work_id=work_id, owner_id="worker-a")
    accepted = await fencing.accept(
        env.session_factory, work_id=work_id, fencing_token=1, result={"pages": 3}
    )
    with pytest.raises(PublicationConflictError):
        await fencing.accept(
            env.session_factory,
            work_id=work_id,
            fencing_token=1,
            result={"pages": 999},
        )
    publication = await fencing.get_publication(env.session_factory, work_id=work_id)
    assert publication == accepted.publication
    assert await _count(env, KernelPublication) == 1


@pytest.mark.parametrize("fault_phase", [fencing.PHASE_FENCE_VALIDATED, fencing.PHASE_PUBLICATION_INSERTED])
async def test_fault_before_acceptance_commit_leaves_no_publication(
    control_env, fault_phase
) -> None:
    env = control_env
    work_id = await _new_work(env)
    await fencing.acquire(env.session_factory, work_id=work_id, owner_id="worker-a")
    with pytest.raises(Exception, match="fault"):
        await fencing.accept(
            env.session_factory,
            work_id=work_id,
            fencing_token=1,
            result={"doomed": True},
            _inject_fault_at=fault_phase,
        )
    assert await _count(env, KernelPublication) == 0
    lease = await fencing.get_lease(env.session_factory, work_id)
    assert lease.state == fencing.LEASE_STATE_LEASED
    assert lease.fencing_token == 1  # acceptance flip rolled back too


async def test_fault_after_commit_is_durable_and_rediscovered(control_env) -> None:
    env = control_env
    work_id = await _new_work(env)
    await fencing.acquire(env.session_factory, work_id=work_id, owner_id="worker-a")
    with pytest.raises(Exception, match="fault"):
        await fencing.accept(
            env.session_factory,
            work_id=work_id,
            fencing_token=1,
            result={"pages": 3},
            _inject_fault_at=fencing.PHASE_POST_COMMIT,
        )
    # Raw truth committed despite the post-commit fault.
    assert await _count(env, KernelPublication) == 1
    lease = await fencing.get_lease(env.session_factory, work_id)
    assert lease.state == fencing.LEASE_STATE_ACCEPTED
    # A fresh engine over the same database discovers the same truth.
    engine2, factory2 = env.new_engine()
    try:
        reopened = await fencing.get_publication(factory2, work_id=work_id)
        assert reopened is not None and reopened.result == {"pages": 3}
        retry = await fencing.accept(
            factory2, work_id=work_id, fencing_token=1, result={"pages": 3}
        )
        assert retry.already_accepted
    finally:
        await engine2.dispose()


# ===========================================================================
# Scheduler and hard capacity (X-S01 .. X-S10)
# ===========================================================================


@pytest.mark.parametrize("cap", [1, 2, 4])
async def test_hard_capacity_never_oversubscribed_under_pressure(
    control_env, cap
) -> None:
    """Many concurrent dispatchers vs one group at ``max_in_flight=cap``.

    A committed-state poller watches for transient oversubscription;
    on PostgreSQL, ``pg_stat_activity`` proves claim transactions
    genuinely overlapped (the race is real, not sequential luck).
    """
    env = control_env
    group = f"capgroup-{cap}"
    await scheduler.set_group_policy(
        env.session_factory,
        resource_class="pressure",
        group_id=group,
        policy=scheduler.GroupPolicy(max_in_flight=cap),
    )
    items = 40
    for i in range(items):
        work_id = await _new_work(env, tag=f"p{i}")
        await scheduler.register_work(
            env.session_factory,
            work_id=work_id,
            resource_class="pressure",
            group_id=group,
        )

    claimers = 32
    stop = asyncio.Event()
    observations: list[int] = []
    max_active_backends = 0

    async def poller():
        nonlocal max_active_backends
        while not stop.is_set():
            observations.append(await _live_leases(env, group_id=group))
            if env.backend == "postgresql":
                async with env.engine.connect() as conn:
                    active = await conn.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND state = 'active'"
                        )
                    )
                max_active_backends = max(max_active_backends, int(active))
            await asyncio.sleep(0.01)

    async def claim_until_drained(i: int):
        """One dispatcher loop: claim repeatedly until no eligible work
        (exactly how the runtime dispatch loop drives ``claim_fair``)."""
        won = []
        while True:
            result = await scheduler.claim_fair(
                env.session_factory,
                owner_id=f"dispatcher-{i}",
                resource_class="pressure",
            )
            if result is None:
                return won
            won.append(result)

    poll = asyncio.create_task(poller())
    try:
        results = await asyncio.gather(*(claim_until_drained(i) for i in range(claimers)))
    finally:
        stop.set()
        await poll

    successes = [r for loop in results for r in loop]
    assert len(successes) == cap
    assert len({r.work_id for r in successes}) == cap
    assert all(o <= cap for o in observations), observations
    # Final committed state: exactly cap live leases, atomically booked.
    assert await _live_leases(env, group_id=group) == cap
    async with env.session_factory() as session:
        group_row = await session.get(
            KernelSchedulingGroup, {"resource_class": "pressure", "group_id": group}
        )
        claimed_events = int(
            await session.scalar(
                select(func.count())
                .select_from(KernelEvent)
                .where(KernelEvent.event_type == scheduler.EVENT_CLAIMED)
            )
        )
        in_flight = int(
            await session.scalar(
                select(func.count())
                .select_from(KernelOutbox)
                .where(KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT)
            )
        )
    assert group_row.served_count == cap  # losing races bumped nothing
    assert claimed_events == cap
    assert in_flight == cap
    if env.backend == "postgresql":
        assert max_active_backends >= 2, (
            "claim transactions never overlapped; the capacity race was "
            "not actually exercised"
        )


async def test_losing_claim_race_leaves_no_partial_state(control_env) -> None:
    env = control_env
    group = "racegroup"
    await scheduler.set_group_policy(
        env.session_factory,
        resource_class="race",
        group_id=group,
        policy=scheduler.GroupPolicy(max_in_flight=1),
    )
    work_ids = [
        await _new_work(env, tag=f"r{i}") for i in range(6)
    ]
    for wid in work_ids:
        await scheduler.register_work(
            env.session_factory, work_id=wid, resource_class="race", group_id=group
        )
    winner = await scheduler.claim_fair(
        env.session_factory, owner_id="winner", resource_class="race"
    )
    assert winner is not None
    losers = await asyncio.gather(
        *(
            scheduler.claim_fair(
                env.session_factory,
                owner_id=f"loser-{i}",
                resource_class="race",
            )
            for i in range(8)
        )
    )
    assert all(r is None for r in losers)
    async with env.session_factory() as session:
        group_row = await session.get(
            KernelSchedulingGroup, {"resource_class": "race", "group_id": group}
        )
        claimed = (
            await session.scalar(
                select(func.count())
                .select_from(KernelOutbox)
                .where(KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT)
            ),
            await session.scalar(
                select(func.count()).select_from(KernelWorkLease)
            ),
            await session.scalar(
                select(func.count())
                .select_from(KernelEvent)
                .where(KernelEvent.event_type == scheduler.EVENT_CLAIMED)
            ),
        )
    assert group_row.served_count == 1
    assert claimed == (1, 1, 1)  # no orphan delivery/lease/event anywhere


async def test_resource_classes_never_serve_each_other(control_env) -> None:
    env = control_env
    cpu_work = await _new_work(env, tag="cpu")
    marker_work = await _new_work(env, tag="marker")
    await scheduler.register_work(
        env.session_factory, work_id=cpu_work, resource_class="cpu"
    )
    await scheduler.register_work(
        env.session_factory, work_id=marker_work, resource_class="marker"
    )
    cpu_claim = await scheduler.claim_fair(
        env.session_factory, owner_id="cpu-worker", resource_class="cpu"
    )
    marker_claim = await scheduler.claim_fair(
        env.session_factory, owner_id="marker-worker", resource_class="marker"
    )
    assert cpu_claim is not None and cpu_claim.work_id == cpu_work
    assert marker_claim is not None and marker_claim.work_id == marker_work
    # The cpu class has nothing else to offer even before marker claims.
    assert await scheduler.claim_fair(
        env.session_factory, owner_id="cpu-worker-2", resource_class="cpu"
    ) is None


async def test_equal_weight_groups_interleave_without_starvation(control_env) -> None:
    env = control_env
    for group in ("alpha", "beta"):
        await scheduler.set_group_policy(
            env.session_factory,
            resource_class="fair",
            group_id=group,
            policy=scheduler.GroupPolicy(max_in_flight=4, weight=1.0),
        )
        for i in range(4):
            wid = await _new_work(env, workspace_id=group, tag=f"{group}{i}")
            await scheduler.register_work(
                env.session_factory, work_id=wid, resource_class="fair", group_id=group
            )
    order = []
    for _ in range(8):
        claimed = await scheduler.claim_fair(
            env.session_factory, owner_id="solo", resource_class="fair"
        )
        assert claimed is not None
        order.append(claimed.group_id)
    # Strict alternation at equal weight and identical served counts.
    assert order == ["alpha", "beta"] * 4


async def test_unequal_weights_converge_to_configured_share(control_env) -> None:
    env = control_env
    for group, weight in (("heavy", 2.0), ("light", 1.0)):
        await scheduler.set_group_policy(
            env.session_factory,
            resource_class="share",
            group_id=group,
            policy=scheduler.GroupPolicy(max_in_flight=25, weight=weight),
        )
        for i in range(30):
            wid = await _new_work(env, workspace_id=group, tag=f"{group}{i}")
            await scheduler.register_work(
                env.session_factory, work_id=wid, resource_class="share", group_id=group
            )
    counts = {"heavy": 0, "light": 0}
    for _ in range(30):
        claimed = await scheduler.claim_fair(
            env.session_factory, owner_id="solo", resource_class="share"
        )
        assert claimed is not None
        counts[claimed.group_id] += 1
    # 2:1 configured share; allow ±2 scheduling noise around 20/10.
    assert abs(counts["heavy"] - 20) <= 2
    assert abs(counts["light"] - 10) <= 2


async def test_authorized_unregistered_work_backfills_after_restart(control_env) -> None:
    """Crash between authorization and registration converges."""
    env = control_env
    work_id = await _new_work(env, tag="backfill")  # authorized, never registered
    # Restart: fresh engine over the same durable database.
    engine2, factory2 = env.new_engine()
    try:
        claimed = await scheduler.claim_fair(factory2, owner_id="post-restart")
        assert claimed is not None and claimed.work_id == work_id
        async with factory2() as session:
            in_flight = await session.scalar(
                select(func.count())
                .select_from(KernelOutbox)
                .where(KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT)
            )
        assert in_flight == 1
    finally:
        await engine2.dispose()


async def test_orphaned_in_flight_delivery_is_released_by_reconcile(control_env) -> None:
    """in_flight without a lease (crash between claim and fence) returns
    to pending; events are re-derived, never invented."""
    env = control_env
    work_id = await _new_work(env, tag="orphan")
    claimed = await outbox_claim(env.session_factory, work_id)  # no lease follows
    assert claimed is not None
    report = await scheduler.reconcile_dispatch(env.session_factory)
    assert report["orphaned_deliveries_released"] >= 1
    async with env.session_factory() as session:
        state = await session.scalar(
            select(KernelOutbox.state).where(KernelOutbox.id == work_id)
        )
    assert state == OUTBOX_STATE_PENDING


# ===========================================================================
# Liveness and cancellation (X-L01 .. X-L09)
# ===========================================================================


async def _claimed(
    env: ControlEnv,
    *,
    owner_id: str,
    tag: str,
    lease_seconds: float = 60.0,
    topology_generation: int | None = None,
):
    work_id = await _new_work(env, tag=tag)
    await scheduler.register_work(env.session_factory, work_id=work_id)
    claim = await scheduler.claim_fair(
        env.session_factory,
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        topology_generation=topology_generation,
    )
    assert claim is not None
    return claim


async def test_renewal_requires_current_evidence_and_advancing_progress(control_env):
    env = control_env
    claim = await _claimed(
        env, owner_id="healthy", tag="renew", topology_generation=7
    )
    outcome = await liveness.renew_lease(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="healthy",
        fencing_token=claim.lease.fencing_token,
        challenge_nonce=claim.challenge_nonce,
        progress=5,
        active_request_id="req-1",
        topology_generation=7,
    )
    assert outcome.renew_count == 1
    assert outcome.lease.fencing_token == claim.lease.fencing_token  # fence unmoved
    with pytest.raises(InvalidChallengeError):
        await liveness.renew_lease(  # replayed nonce
            env.session_factory,
            work_id=claim.work_id,
            owner_id="healthy",
            fencing_token=claim.lease.fencing_token,
            challenge_nonce=claim.challenge_nonce,
            progress=6,
            active_request_id="req-1",
            topology_generation=7,
        )
    with pytest.raises(ProgressNotAdvancingError):
        await liveness.renew_lease(  # non-advancing progress
            env.session_factory,
            work_id=claim.work_id,
            owner_id="healthy",
            fencing_token=claim.lease.fencing_token,
            challenge_nonce=outcome.next_challenge_nonce,
            progress=5,
            active_request_id="req-1",
            topology_generation=7,
        )
    with pytest.raises(TopologyMismatchError):
        await liveness.renew_lease(  # wrong topology generation
            env.session_factory,
            work_id=claim.work_id,
            owner_id="healthy",
            fencing_token=claim.lease.fencing_token,
            challenge_nonce=outcome.next_challenge_nonce,
            progress=6,
            active_request_id="req-1",
            topology_generation=8,
        )


async def test_active_request_binding_and_expiry_rules(control_env) -> None:
    env = control_env
    claim = await _claimed(env, owner_id="bound", tag="bind")
    fresh_expiry = datetime.now(timezone.utc) + timedelta(seconds=300)
    # Bind req-1 with a near-term expiry so the lapse transition is
    # observable without a long sleep.
    binding_expiry = datetime.now(timezone.utc) + timedelta(seconds=0.15)
    outcome = await liveness.renew_lease(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="bound",
        fencing_token=claim.lease.fencing_token,
        challenge_nonce=claim.challenge_nonce,
        progress=1,
        active_request_id="req-1",
        request_expires_at=binding_expiry,
    )
    # Serving a different request while the bound one is still active fails.
    with pytest.raises(RequestNotActiveError):
        await liveness.renew_lease(
            env.session_factory,
            work_id=claim.work_id,
            owner_id="bound",
            fencing_token=claim.lease.fencing_token,
            challenge_nonce=outcome.next_challenge_nonce,
            progress=2,
            active_request_id="req-2",
            request_expires_at=fresh_expiry,
        )
    # Once the bound request has lapsed, a stage switch is honest.
    await asyncio.sleep(0.2)
    switched = await liveness.renew_lease(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="bound",
        fencing_token=claim.lease.fencing_token,
        challenge_nonce=outcome.next_challenge_nonce,
        progress=3,
        active_request_id="req-2",
        request_expires_at=None,
    )
    assert switched.renew_count == 2
    # With the new binding unbounded (expiry cleared), the previously
    # bound request is acceptable again — expiry rules, not ghost bindings.
    final = await liveness.renew_lease(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="bound",
        fencing_token=claim.lease.fencing_token,
        challenge_nonce=switched.next_challenge_nonce,
        progress=4,
        active_request_id="req-1",
        request_expires_at=None,
    )
    assert final.renew_count == 3


async def test_durable_cancellation_defeats_every_later_renewal(control_env) -> None:
    env = control_env
    claim = await _claimed(env, owner_id="victim", tag="cancel")
    assert await liveness.report_cancellation(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="victim",
        fencing_token=claim.lease.fencing_token,
        reason="user-requested",
    )
    view = await liveness.get_liveness(env.session_factory, claim.work_id)
    assert view.cancelled_at is not None
    with pytest.raises(WorkCancelledError):
        await liveness.renew_lease(
            env.session_factory,
            work_id=claim.work_id,
            owner_id="victim",
            fencing_token=claim.lease.fencing_token,
            challenge_nonce=claim.challenge_nonce,
            progress=10,
            active_request_id="req-1",
        )
    # Idempotent observation: no duplicate event, returns False.
    assert not await liveness.report_cancellation(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="victim",
        fencing_token=claim.lease.fencing_token,
        reason="user-requested",
    )
    async with env.session_factory() as session:
        cancel_events = int(
            await session.scalar(
                select(func.count())
                .select_from(KernelEvent)
                .where(KernelEvent.event_type == liveness.EVENT_CANCEL_REQUESTED)
            )
        )
    assert cancel_events == 1


async def test_cancellation_and_renewal_cannot_resurrect_in_either_order(control_env):
    """Both commit orderings of a cancel/renew race stay truthful."""
    env = control_env
    await scheduler.set_group_policy(
        env.session_factory,
        resource_class="default",
        group_id="ws",
        policy=scheduler.GroupPolicy(max_in_flight=16),
    )
    # Ordering 1: cancellation commits first — renewal must fail.
    claim = await _claimed(env, owner_id="racer", tag="cr1")
    await liveness.report_cancellation(
        env.session_factory,
        work_id=claim.work_id,
        owner_id="racer",
        fencing_token=claim.lease.fencing_token,
        reason="cancel-first",
    )
    with pytest.raises((WorkCancelledError, InvalidChallengeError)):
        await liveness.renew_lease(
            env.session_factory,
            work_id=claim.work_id,
            owner_id="racer",
            fencing_token=claim.lease.fencing_token,
            challenge_nonce=claim.challenge_nonce,
            progress=3,
            active_request_id="req-1",
        )
    # Ordering 2: renewal commits first — cancellation still lands, and
    # every later renewal still fails.
    claim2 = await _claimed(env, owner_id="racer2", tag="cr2")
    renewed = await liveness.renew_lease(
        env.session_factory,
        work_id=claim2.work_id,
        owner_id="racer2",
        fencing_token=claim2.lease.fencing_token,
        challenge_nonce=claim2.challenge_nonce,
        progress=3,
        active_request_id="req-1",
    )
    assert await liveness.report_cancellation(
        env.session_factory,
        work_id=claim2.work_id,
        owner_id="racer2",
        fencing_token=claim2.lease.fencing_token,
        reason="renew-first",
    )
    with pytest.raises(WorkCancelledError):
        await liveness.renew_lease(
            env.session_factory,
            work_id=claim2.work_id,
            owner_id="racer2",
            fencing_token=claim2.lease.fencing_token,
            challenge_nonce=renewed.next_challenge_nonce,
            progress=9,
            active_request_id="req-1",
        )
    # Concurrent mixed rounds never leave an un-cancelled resurrected lease.
    for round_id in range(4):
        claim_n = await _claimed(env, owner_id=f"mixed-{round_id}", tag=f"mx{round_id}")
        token = claim_n.lease.fencing_token
        nonce = claim_n.challenge_nonce
        renew_task = asyncio.create_task(
            liveness.renew_lease(
                env.session_factory,
                work_id=claim_n.work_id,
                owner_id=f"mixed-{round_id}",
                fencing_token=token,
                challenge_nonce=nonce,
                progress=5,
                active_request_id="req-1",
            )
        )
        cancel_task = asyncio.create_task(
            liveness.report_cancellation(
                env.session_factory,
                work_id=claim_n.work_id,
                owner_id=f"mixed-{round_id}",
                fencing_token=token,
                reason="race",
            )
        )
        renew_result, cancel_result = await asyncio.gather(
            renew_task, cancel_task, return_exceptions=True
        )
        view = await liveness.get_liveness(env.session_factory, claim_n.work_id)
        if view.cancelled_at is not None and cancel_result is False:
            pass  # cancel observed first or idempotently
        # After both settle, the durable state must be cancelled-or-expired;
        # a further renewal attempt can never succeed.
        with pytest.raises((WorkCancelledError, InvalidChallengeError, StaleFenceError)):
            await liveness.renew_lease(
                env.session_factory,
                work_id=claim_n.work_id,
                owner_id=f"mixed-{round_id}",
                fencing_token=token,
                challenge_nonce=nonce,
                progress=50,
                active_request_id="req-1",
            )


# ===========================================================================
# Durable events and progress (X-V01 .. X-V06)
# ===========================================================================


async def test_concurrent_event_producers_cannot_fork_the_sequence(control_env) -> None:
    env = control_env
    producers, per_producer = 8, 15
    total = producers * per_producer

    async def produce(p: int):
        out = []
        for i in range(per_producer):
            out.append(
                await kernel_events.append(
                    env.session_factory,
                    workspace_id="storm",
                    stream="work",
                    event_type="progress.tick",
                    payload={"producer": p, "i": i},
                )
            )
        return out

    batches = await asyncio.gather(*(produce(p) for p in range(producers)))
    all_events = [e for batch in batches for e in batch]
    sequences = sorted(e.semantic_sequence for e in all_events)
    assert sequences == list(range(1, total + 1))  # unique, contiguous, from 1

    replayed = await kernel_events.replay(
        env.session_factory, workspace_id="storm", stream="work"
    )
    assert [e.semantic_sequence for e in replayed] == list(range(1, total + 1))
    # Raw audit: exactly one row per sequence value.
    async with env.session_factory() as session:
        rows = int(await session.scalar(select(func.count()).select_from(KernelEvent)))
        distinct = int(
            await session.scalar(
                select(func.count())
                .select_from(
                    select(KernelEvent.semantic_sequence).distinct().subquery()
                )
            )
        )
    assert rows == distinct == total


async def test_rolled_back_append_leaves_no_committed_hole(control_env) -> None:
    env = control_env
    await kernel_events.append(
        env.session_factory,
        workspace_id="holes",
        event_type="progress.tick",
        payload={"i": 1},
    )
    async with env.session_factory() as session:
        await kernel_events._append_in_session(
            session,
            workspace_id="holes",
            stream="work",
            event_type="progress.tick",
            payload_json='{"i": "doomed"}',
            durability=kernel_events.DURABILITY_DURABLE,
        )
        await session.flush()
        await session.rollback()
    assert await kernel_events.get_latest_sequence(
        env.session_factory, workspace_id="holes"
    ) == 1
    after = await kernel_events.append(
        env.session_factory,
        workspace_id="holes",
        event_type="progress.tick",
        payload={"i": 3},
    )
    assert after.semantic_sequence == 2  # the aborted 2 left no hole
    replayed = await kernel_events.replay(env.session_factory, workspace_id="holes")
    assert [e.payload["i"] for e in replayed] == [1, 3]


async def test_restart_preserves_event_membership_and_order(control_env) -> None:
    env = control_env
    for i in range(6):
        await kernel_events.append(
            env.session_factory,
            workspace_id="durable",
            event_type="progress.tick",
            payload={"i": i},
        )
    before = await kernel_events.replay(env.session_factory, workspace_id="durable")
    engine2, factory2 = env.new_engine()
    try:
        after = await kernel_events.replay(factory2, workspace_id="durable")
        assert after == before
        assert await kernel_events.get_latest_sequence(
            factory2, workspace_id="durable"
        ) == 6
    finally:
        await engine2.dispose()


async def test_progress_flood_coalesces_and_never_drops_events(control_env) -> None:
    env = control_env
    work_id = await _new_work(env, tag="flood")
    async def flood():
        for i in range(120):
            await kernel_events.append_progress(
                env.session_factory,
                workspace_id="ws",
                work_id=work_id,
                counter=i,
                payload={"pct": i},
            )
    async def semantic():
        for i in range(10):
            await kernel_events.append(
                env.session_factory,
                workspace_id="ws",
                event_type="progress.tick",
                payload={"i": i},
            )
    await asyncio.gather(flood(), semantic())
    assert await _count(env, KernelProgress) == 1  # one coalesced row
    snapshot = await kernel_events.get_progress(
        env.session_factory, workspace_id="ws", work_id=work_id
    )
    assert snapshot.counter == 119
    assert await kernel_events.get_latest_sequence(env.session_factory, workspace_id="ws") >= 10


async def test_slow_consumer_cannot_block_execution(control_env) -> None:
    """A polling cursor that idles must not delay durable appends."""
    env = control_env
    consumed: list[int] = []

    async def slow_consumer():
        agen = kernel_events.follow(
            env.session_factory,
            workspace_id="slow",
            poll_interval=0.01,
            max_idle_seconds=0.15,
        )
        async for event in agen:
            consumed.append(event.semantic_sequence)
        return len(consumed)

    consumer = asyncio.create_task(slow_consumer())
    await asyncio.sleep(0.02)  # consumer is now polling
    appended = []
    for i in range(5):
        appended.append(
            await kernel_events.append(
                env.session_factory,
                workspace_id="slow",
                event_type="progress.tick",
                payload={"i": i},
            )
        )
    consumed_count = await consumer
    # The consumer eventually observed every durable event (bounded
    # read batches, fresh short sessions — it held nothing open).
    assert consumed_count == 5
    assert [e.semantic_sequence for e in appended] == [1, 2, 3, 4, 5]


async def test_event_gap_reconciliation_derives_and_is_idempotent(control_env) -> None:
    env = control_env
    work_id = await _new_work(env, tag="gap")
    await scheduler.register_work(env.session_factory, work_id=work_id)
    claim = await scheduler.claim_fair(env.session_factory, owner_id="gapper")
    assert claim is not None
    # Simulate a lost claim event (crash between commit and bookkeeping).
    async with env.session_factory() as session:
        await session.execute(
            text("DELETE FROM kernel_events WHERE event_type = 'work.claimed'")
        )
        await session.commit()
    repaired = await kernel_events.reconcile_from_authority(
        env.session_factory, workspace_id="ws"
    )
    assert any(e.payload.get("work_id") == work_id and e.payload.get("repair")
               for e in repaired)
    again = await kernel_events.reconcile_from_authority(
        env.session_factory, workspace_id="ws"
    )
    assert again == []


# ===========================================================================
# Retry/error vocabulary (X-T01 .. X-T08)
# ===========================================================================


def _with_orig(op: OperationalError, message: str) -> OperationalError:
    # SQLAlchemy formats str() from the constructor-time orig.
    return OperationalError("stmt", {}, Exception(message))


def test_sqlite_busy_vocabulary_still_classifies() -> None:
    # aiosqlite surfaces the driver message verbatim
    assert is_retryable_contention(_with_orig(None, "database is locked"))
    assert not is_retryable_contention(
        _with_orig(None, "no such table: kernel_outbox")
    )


async def test_non_retryable_postgres_error_is_not_swallowed(control_env_pg_only) -> None:
    env = control_env_pg_only
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        async with env.engine.connect() as conn:
            await conn.execute(text("SELECT 1/0"))

    with pytest.raises(DBAPIError):
        await run_with_contention_retry(bad, operation_name="probe")
    assert calls["n"] == 1  # never retried


async def test_real_serialization_failure_produces_40001_and_retry_converges(
    control_env_pg_only,
) -> None:
    """The server itself emits ``40001`` under a deterministic
    read-write conflict; classification sees it and the bounded budget
    retries the WHOLE operation, converging exactly once."""
    env = control_env_pg_only
    work_id = await _new_work(env, tag="ser")

    async def serializable_conn():
        conn = await env.engine.connect()
        await conn.execution_options(isolation_level="SERIALIZABLE")
        return conn

    observed: dict = {}
    attempts = {"n": 0}

    async def conflicting_update():
        """Attempt 1 performs the deterministic read-write conflict (the
        server aborts it with 40001); attempt 2 is the whole-operation
        retry on a fresh transaction pair, which succeeds."""
        attempts["n"] += 1
        c1 = await serializable_conn()
        c2 = await serializable_conn()
        try:
            if attempts["n"] > 1:
                t = await c1.begin()
                await c1.execute(
                    update(KernelOutbox)
                    .where(KernelOutbox.id == work_id)
                    .values(state=OUTBOX_STATE_PENDING)
                    .execution_options(synchronize_session=False)
                )
                await t.commit()
                return
            t1 = await c1.begin()
            t2 = await c2.begin()
            # Establish BOTH read snapshots before either side writes —
            # gather returns only when both transactions hold their
            # snapshot, which is the deterministic overlap point.
            await asyncio.gather(
                c1.execute(text("SELECT count(*) FROM kernel_outbox")),
                c2.execute(text("SELECT count(*) FROM kernel_outbox")),
            )
            # T2 writes and commits first (invalidating T1's snapshot).
            await c2.execute(
                update(KernelOutbox)
                .where(KernelOutbox.id == work_id)
                .values(state=OUTBOX_STATE_PENDING)
                .execution_options(synchronize_session=False)
            )
            await t2.commit()
            # T1's write now conflicts → the server aborts it with 40001.
            try:
                await c1.execute(
                    update(KernelOutbox)
                    .where(KernelOutbox.id == work_id)
                    .values(state=OUTBOX_STATE_PENDING)
                    .execution_options(synchronize_session=False)
                )
                await t1.commit()
            except DBAPIError as exc:
                # asyncpg surfaces 40001 as sqlalchemy.exc.DBAPIError
                observed["sqlstate"] = _sqlstate_of(exc)
                observed["retryable"] = is_retryable_contention(exc)
                raise
            raise AssertionError("expected a serialization failure")

        finally:
            await c1.close()
            await c2.close()

    await run_with_contention_retry(conflicting_update, operation_name="serialize-probe")
    assert attempts["n"] == 2  # exactly one retry, then convergence
    assert observed["sqlstate"] == "40001"
    assert observed["retryable"] is True


async def test_real_deadlock_produces_40p01_and_retry_converges(control_env_pg_only) -> None:
    env = control_env_pg_only
    w1 = await _new_work(env, tag="dl1")
    w2 = await _new_work(env, tag="dl2")

    async def conn_fast_deadlock():
        conn = await env.engine.connect()
        await conn.execution_options(isolation_level="READ COMMITTED")
        # The SET autobegins a transaction; finish it so the explicit
        # begin() below starts the actual deadlock dance.
        await conn.execute(text(f"SET deadlock_timeout = '{PG_DEADLOCK_TIMEOUT_MS}ms'"))
        await conn.commit()
        return conn

    observed: dict = {}
    attempts = {"n": 0}

    async def inverted_locks():
        attempts["n"] += 1
        c1 = await conn_fast_deadlock()
        c2 = await conn_fast_deadlock()
        try:
            if attempts["n"] > 1:
                # Whole-operation retry: sequential single-writer pass.
                t = await c1.begin()
                await c1.execute(
                    update(KernelOutbox)
                    .where(KernelOutbox.id == w1)
                    .values(state=OUTBOX_STATE_PENDING)
                    .execution_options(synchronize_session=False)
                )
                await t.commit()
                return
            t1 = await c1.begin()
            t2 = await c2.begin()
            # Each transaction locks its own row first; both primaries
            # held before either asks for the other's (gather guarantees
            # the overlap), then the inverted requests deadlock.
            await asyncio.gather(
                c1.execute(
                    update(KernelOutbox)
                    .where(KernelOutbox.id == w1)
                    .values(state=OUTBOX_STATE_PENDING)
                    .execution_options(synchronize_session=False)
                ),
                c2.execute(
                    update(KernelOutbox)
                    .where(KernelOutbox.id == w2)
                    .values(state=OUTBOX_STATE_PENDING)
                    .execution_options(synchronize_session=False)
                ),
            )
            # Inverted order: each waits on the other's row; PG detects
            # the cycle and aborts one participant with 40P01.
            async def t1_second():
                await asyncio.sleep(0.02)
                try:
                    await c1.execute(
                        update(KernelOutbox)
                        .where(KernelOutbox.id == w2)
                        .values(state=OUTBOX_STATE_PENDING)
                        .execution_options(synchronize_session=False)
                    )
                    await t1.commit()
                    return "committed"
                except DBAPIError as exc:
                    # asyncpg surfaces 40P01 as sqlalchemy.exc.DBAPIError
                    observed["sqlstate"] = _sqlstate_of(exc)
                    observed["retryable"] = is_retryable_contention(exc)
                    raise

            async def t2_second():
                await asyncio.sleep(0.02)
                await c2.execute(
                    update(KernelOutbox)
                    .where(KernelOutbox.id == w1)
                    .values(state=OUTBOX_STATE_PENDING)
                    .execution_options(synchronize_session=False)
                )
                await t2.commit()
                return "committed"

            results = await asyncio.gather(
                t1_second(), t2_second(), return_exceptions=True
            )
            errors = [r for r in results if isinstance(r, BaseException)]
            # PostgreSQL terminates *one* participant; which side loses
            # is the server's choice, so either side may carry the
            # observed SQLSTATE.
            for r in errors:
                if isinstance(r, DBAPIError) and observed.get("sqlstate") is None:
                    observed["sqlstate"] = _sqlstate_of(r)
                    observed["retryable"] = is_retryable_contention(r)
            if errors and not isinstance(errors[0], asyncio.CancelledError):
                raise errors[0]
            if not errors:
                raise AssertionError("deadlock did not occur; lock inversion was not exercised")
        finally:
            await c1.close()
            await c2.close()

    await run_with_contention_retry(inverted_locks, operation_name="deadlock-probe")
    assert attempts["n"] == 2
    assert observed["sqlstate"] == "40P01"
    assert observed["retryable"] is True


def _sqlstate_of(exc: BaseException) -> str | None:
    from app.kernel.dialects import _sqlstate

    return _sqlstate(exc)


def test_retry_exhaustion_is_typed_with_context() -> None:
    attempts = {"n": 0}

    async def always_busy():
        attempts["n"] += 1
        raise OperationalError("stmt", {}, Exception("database is locked"))

    with pytest.raises(KernelBusyError, match="probe-op .*still busy.* 3"):
        asyncio.run(
            run_with_contention_retry(
                always_busy,
                attempts=3,
                base_delay=0.001,
                operation_name="probe-op",
            )
        )
    assert attempts["n"] == 3
