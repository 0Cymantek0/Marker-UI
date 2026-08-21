"""Dual-backend, dual-store lifecycle conformance (PR83B1 WS6, Gate 6).

The full payload lifecycle — commit, reopen, availability verification,
orphan discovery, root/pin rescue races, retirement decision, physical
sweep, restart reconciliation, and the lexical fail-closure boundary —
runs against every supported database × store combination through the
same production modules. There is no per-profile implementation to
drift.

Provisioning mirrors ``test_kernel_dual_backend_conformance.py``:
PostgreSQL needs ``MARKER_TEST_POSTGRES_ADMIN_URL`` (throwaway database
per test); the S3 profile needs the MinIO variables from
``tests/s3_provisioning``. Strict modes (``MARKER_TEST_POSTGRES_STRICT``
/ ``MARKER_TEST_S3_STRICT``) turn missing provisioning into failures so
an invoked industrial target can never pass through silent skips or
fallback to the local store.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError, PayloadStageError
from app.kernel.gc import (
    PHASE_GC_AFTER_RECHECK,
    PHASE_GC_AFTER_UNLINK,
    PHASE_GC_PAUSE_AFTER_ROOTS,
    PHASE_GC_PAUSE_BEFORE_LOCK,
    RETIRE_STATE_DELETED,
    RETIRE_STATE_FAILED,
    RETIRE_STATE_PENDING,
    collect,
    execute_collection,
    plan_collection,
    reconcile_retirements,
)
from app.kernel.generations import (
    GenerationService,
    resolve_current_generation,
)
from app.kernel.models import (
    KernelLexicalGeneration,
    KernelPayloadObject,
    KernelPayloadRetirement,
    KernelRetentionRoot,
)
from app.kernel.object_store import (
    S3_OBJECT_STORE_PROFILE,
    S3PayloadStore,
    S3StoreConfig,
)
from app.kernel.outbox import OutboxIntent
from app.kernel.payloads import (
    LOCAL_STORE_PROFILE,
    LocalPayloadStore,
    PayloadMaintenanceStore,
)
from app.kernel.records import ObservationRecord
from app.kernel.reconcile import (
    PAYLOAD_STATE_AVAILABLE,
    PAYLOAD_STATE_CORRUPT,
    PAYLOAD_STATE_MISSING,
    PAYLOAD_STATE_RETIRED,
    verify_payload_availability,
)
from app.kernel.retention import (
    ROOT_KIND_SNAPSHOT_HOLD,
    acquire_reader_pin,
    declare_hold,
    release_hold,
    release_reader_pin,
)
from app.kernel.snapshots import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_DEGRADED,
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    resolve_snapshot,
)
from tests import s3_provisioning
from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)

pytestmark = pytest.mark.asyncio


def _s3_env_present() -> bool:
    return all(
        os.getenv(var, "").strip()
        for var in (
            s3_provisioning.ENDPOINT_ENV,
            s3_provisioning.ACCESS_KEY_ENV,
            s3_provisioning.SECRET_KEY_ENV,
        )
    )


_S3_AVAILABLE = _s3_env_present()
if s3_provisioning.strict_mode() and not _S3_AVAILABLE:
    # Strict industrial target: a missing object store must fail the
    # suite, never silently drop the S3 parameters out of the matrix.
    raise pytest.UsageError(
        "MARKER_TEST_S3_STRICT is set but S3 provisioning env is missing; "
        "refusing to run the lifecycle conformance without the real store"
    )

_STORE_NAMES = ("local_file",) + (
    (s3_provisioning.S3_STORE_NAME,) if _S3_AVAILABLE else ()
)
COMBOS = [(backend, store) for backend in BACKENDS for store in _STORE_NAMES]


@dataclass
class LifecycleEnv:
    """One migrated database × one payload store, wired for the kernel."""

    backend: str
    store_name: str
    url: str
    engine: object
    session_factory: async_sessionmaker
    store: PayloadMaintenanceStore
    service: KernelCommitService
    #: mints a fresh store handle over the SAME physical namespace
    reopen_store: object
    server_version: str = ""


def make_observation(observer: str, derivation: dict, payload: bytes | None = None):
    return ObservationRecord(
        observer=observer,
        derivation=derivation,
        payload_bytes=payload,
    )


async def _commit_one(env: LifecycleEnv, workspace: str, payload: bytes) -> int:
    # Unique derivation per call: recommitting the same observer payload
    # in one workspace is a duplicate identity, not a new commit.
    receipt = await env.service.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(
                make_observation(
                    f"op-{workspace}-{uuid4().hex[:8]}",
                    {"k": uuid4().hex},
                    payload=payload,
                ),
            ),
            outbox=(OutboxIntent(work_kind="index", payload={"t": workspace}),),
        )
    )
    return receipt.kernel_commit_id


async def _collect(env: LifecycleEnv, **kwargs):
    return await collect(env.session_factory, env.store, **kwargs)


async def _retirement_rows(env: LifecycleEnv) -> dict[str, str]:
    async with env.session_factory() as session:
        rows = (
            await session.execute(
                select(
                    KernelPayloadRetirement.blob_key,
                    KernelPayloadRetirement.state,
                )
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def _registry_blob_for_length(env: LifecycleEnv, length: int) -> str:
    async with env.session_factory() as session:
        return (
            await session.execute(
                select(KernelPayloadObject.blob_key).where(
                    KernelPayloadObject.payload_length == length
                )
            )
        ).scalar_one()


@pytest.fixture(params=COMBOS, ids=[f"{b}-{s}" for b, s in COMBOS])
def combo(request) -> tuple[str, str]:
    return request.param


@pytest_asyncio.fixture
async def lifecycle_env(combo, tmp_path: pathlib.Path):
    backend, store_name = combo
    async with provisioned_database(
        backend, (tmp_path / "kernel.db").as_posix()
    ) as prov:
        url = prov.url
        result = await upgrade_database(url=url)
        assert result.to_revision, "bootstrap must reach a migration head"

        engine = create_async_engine(url, **engine_kwargs_for(backend))
        assert engine.dialect.name == backend  # no URL mixups, no fallback
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        server_version = ""
        if backend == "postgresql":
            async with engine.connect() as conn:
                server_version = await conn.scalar(text("SELECT version()"))

        store: PayloadMaintenanceStore
        owner_close = None
        if store_name == "local_file":
            root = tmp_path / "payloads"

            def reopen_store():
                return LocalPayloadStore(root)

            store = LocalPayloadStore(root)
        else:
            endpoint, access, secret = s3_provisioning.require_s3_env()
            common = dict(
                endpoint_url=endpoint,
                bucket=s3_provisioning.unique_bucket(),
                access_key_id=access,
                secret_access_key=secret,
            )

            def reopen_store():
                return S3PayloadStore(
                    S3StoreConfig(**common, delete_namespace_on_close=False)
                )

            store = reopen_store()
            owner = S3PayloadStore(
                S3StoreConfig(**common, delete_namespace_on_close=True)
            )

            async def owner_close():  # type: ignore[misc]
                await owner.close()

        service = KernelCommitService(session_factory, payload_store=store)
        try:
            yield LifecycleEnv(
                backend=backend,
                store_name=store_name,
                url=url,
                engine=engine,
                session_factory=session_factory,
                store=store,
                service=service,
                reopen_store=reopen_store,
                server_version=server_version,
            )
        finally:
            if owner_close is not None:
                await owner_close()
            await engine.dispose()


async def _reopen(
    env: LifecycleEnv,
) -> tuple[async_sessionmaker, PayloadMaintenanceStore]:
    """Fresh engine + fresh store handle over the same durable state."""
    engine = create_async_engine(env.url, **engine_kwargs_for(env.backend))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, env.reopen_store()


# ---------------------------------------------------------------------------
# Provenance: real backends, real store, no substitution
# ---------------------------------------------------------------------------


async def test_environment_identity_is_real(lifecycle_env) -> None:
    env = lifecycle_env
    assert env.engine.dialect.name == env.backend
    if env.backend == "postgresql":
        assert "PostgreSQL" in env.server_version
    if env.store_name == s3_provisioning.S3_STORE_NAME:
        assert isinstance(env.store, S3PayloadStore)
        assert env.store.store_profile == S3_OBJECT_STORE_PROFILE
    else:
        assert isinstance(env.store, LocalPayloadStore)
        assert env.store.store_profile == LOCAL_STORE_PROFILE


# ---------------------------------------------------------------------------
# Commit → reopen → verify → snapshot completeness (X-C01/X-C02, X-O14)
# ---------------------------------------------------------------------------


async def test_commit_reopen_verify_and_snapshot_completeness(lifecycle_env) -> None:
    env = lifecycle_env
    cut = await _commit_one(env, "ws-life", b"lifecycle-bytes")

    # Fresh process semantics: new engine, new store handle, same state.
    factory, store = await _reopen(env)
    availability = await verify_payload_availability(factory, store)
    assert availability.payload_backed_complete
    assert all(
        r.state == PAYLOAD_STATE_AVAILABLE for r in availability.record_states
    )
    snapshot = await resolve_snapshot(
        factory,
        "ws-life",
        at_commit=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == COMPLETENESS_COMPLETE

    # Bytes lost behind the database's back (no tombstone): missing, and
    # an inspectable snapshot degrades honestly.
    blob_key = availability.record_states[0].blob_key
    await store.delete_object(blob_key)
    availability = await verify_payload_availability(factory, store)
    assert availability.summary().get(PAYLOAD_STATE_MISSING, 0) == 1
    snapshot = await resolve_snapshot(
        factory,
        "ws-life",
        at_commit=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == COMPLETENESS_DEGRADED

    # Tamper is detectable where the physical medium allows tampering
    # (the local profile); the S3 conditional-create profile refuses
    # wrong-byte occupants by construction instead.
    if isinstance(store, LocalPayloadStore):
        await _commit_one(env, "ws-life", b"lifecycle-bytes")  # heal first
        path = store.object_path(blob_key)
        path.chmod(0o600)
        path.write_bytes(b"tampered-same-place")
        availability = await verify_payload_availability(factory, store)
        # Both records reference the same content-addressed blob, so one
        # tampered object degrades every record pointing at it.
        assert availability.summary().get(PAYLOAD_STATE_CORRUPT, 0) == 2


# ---------------------------------------------------------------------------
# Orphan reporting and collection (X-C03/X-C04)
# ---------------------------------------------------------------------------


async def test_orphan_reported_then_collected_registry_survives(lifecycle_env) -> None:
    env = lifecycle_env
    await _commit_one(env, "ws-orph", b"orphan-registry-bytes")
    blob = await env.store.stage(b"never-committed-orphan")

    # Reconciliation reports the orphan but deletes nothing.
    availability = await verify_payload_availability(
        env.session_factory, env.store
    )
    assert availability.orphan_objects == (blob.blob_key,)
    assert await env.store.object_exists(blob.blob_key)

    # No root protects ws-orph either, so this pass retires both the
    # staged orphan and the now-unreachable committed object.
    report = await _collect(env)
    assert report.tombstoned == 2 and report.swept_deleted == 2
    assert not await env.store.object_exists(blob.blob_key)

    # Registry truth for the committed object is permanent metadata:
    # its row survives even though its bytes were retired.
    rows = await _retirement_rows(env)
    assert set(rows.values()) == {RETIRE_STATE_DELETED}
    async with env.session_factory() as session:
        registry = await session.scalar(
            select(func.count()).select_from(KernelPayloadObject)
        )
    assert registry == 1


# ---------------------------------------------------------------------------
# Root/pin rescue (X-C05/X-C06) and store-wide dedup domain (X-C13)
# ---------------------------------------------------------------------------


async def test_root_declared_after_mark_is_rescued_at_recheck(lifecycle_env) -> None:
    env = lifecycle_env
    cut = await _commit_one(env, "ws-rescue", b"rescue-bytes")
    plan = await plan_collection(env.session_factory, env.store)
    assert plan.candidate_registry_keys  # unprotected right after mark

    await declare_hold(
        env.session_factory,
        workspace_id="ws-rescue",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )
    report = await execute_collection(env.session_factory, env.store, plan)
    assert report.rescued_count == 1
    assert report.tombstoned == 0
    assert await _retirement_rows(env) == {}
    check = await env.store.check_object(
        plan.candidate_registry_keys[0],
        expected_length=len(b"rescue-bytes"),
    )
    assert check.available


async def test_pinned_superseded_generation_survives_collection(lifecycle_env) -> None:
    env = lifecycle_env
    cut1 = await _commit_one(env, "ws-gen", b"gen-bytes-one")
    cut2 = await _commit_one(env, "ws-gen", b"gen-bytes-two")
    gens = GenerationService(env.session_factory)

    first = await gens.build_and_activate(
        await resolve_snapshot(
            env.session_factory, "ws-gen", at_commit=cut1
        )
    )
    second = await gens.build_and_activate(
        await resolve_snapshot(env.session_factory, "ws-gen", at_commit=cut2)
    )
    assert second.generation_id != first.generation_id

    # Mark first: the superseded generation is collectible, then a pin
    # acquired after the mark must rescue it at the recheck decision.
    plan = await plan_collection(env.session_factory, env.store)
    assert first.generation_id in [c.generation_id for c in plan.eligible_generations]

    pin = await acquire_reader_pin(env.session_factory, first.generation_id)
    report = await execute_collection(env.session_factory, env.store, plan)
    assert report.generations_rescued == (first.generation_id,)
    assert report.generations_retired == 0
    current = await resolve_current_generation(env.session_factory, "ws-gen")
    assert current.generation_id == second.generation_id

    # Releasing the pin makes the superseded generation collectible.
    await release_reader_pin(env.session_factory, pin.pin_id)
    report = await _collect(env)
    assert report.generations_retired == 1
    assert report.generations_rescued == ()
    current = await resolve_current_generation(env.session_factory, "ws-gen")
    assert current.generation_id == second.generation_id  # head untouched


async def test_storewide_dedup_protects_shared_bytes_across_workspaces(
    lifecycle_env,
) -> None:
    env = lifecycle_env
    cut_a = await _commit_one(env, "ws-share-a", b"shared-dedup-bytes")
    await _commit_one(env, "ws-share-b", b"shared-dedup-bytes")

    # Only workspace B holds a root; A has none. The bytes are live
    # store-wide, so collection must not delete them.
    root_b = await declare_hold(
        env.session_factory,
        workspace_id="ws-share-b",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut_a,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )
    report = await _collect(env)
    assert report.tombstoned == 0

    await release_hold(env.session_factory, root_b.root_id)
    report = await _collect(env)
    assert report.tombstoned == 1
    assert report.swept_deleted == 1


# ---------------------------------------------------------------------------
# Decision-first ordering and healing (X-C07)
# ---------------------------------------------------------------------------


async def test_decision_first_late_root_sees_retired_then_heals(lifecycle_env) -> None:
    env = lifecycle_env
    cut = await _commit_one(env, "ws-late", b"late-root-bytes")
    blob_key = await _registry_blob_for_length(env, len(b"late-root-bytes"))

    report = await _collect(env)  # no roots anywhere: decision + sweep
    assert report.swept_deleted == 1

    # A root declared AFTER the decision sees honest retired state...
    await declare_hold(
        env.session_factory,
        workspace_id="ws-late",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )
    availability = await verify_payload_availability(env.session_factory, env.store)
    assert availability.summary().get(PAYLOAD_STATE_RETIRED, 0) == 1

    # ...and re-supplying the exact bytes through the normal publication
    # path rescues the tombstone and restores availability.
    receipt = await env.service.commit(
        KernelCommitBatch(
            workspace_id="ws-late",
            records=(make_observation("op-heal", {"k": 2}, payload=b"late-root-bytes"),),
        )
    )
    assert receipt.kernel_commit_id > cut
    availability = await verify_payload_availability(env.session_factory, env.store)
    assert PAYLOAD_STATE_RETIRED not in availability.summary()
    assert blob_key not in await _retirement_rows(env)  # commit-side rescue


# ---------------------------------------------------------------------------
# Concurrent collectors (X-C08)
# ---------------------------------------------------------------------------


async def test_concurrent_collectors_converge_on_one_outcome(lifecycle_env) -> None:
    env = lifecycle_env
    await _commit_one(env, "ws-race", b"collector-race-bytes")
    plan = await plan_collection(env.session_factory, env.store)
    assert plan.candidate_registry_keys

    first, second = await asyncio.gather(
        execute_collection(env.session_factory, env.store, plan),
        execute_collection(env.session_factory, env.store, plan),
    )
    assert first.failed_keys == () and second.failed_keys == ()

    rows = await _retirement_rows(env)
    assert len(rows) == 1
    state = next(iter(rows.values()))
    assert state == RETIRE_STATE_DELETED
    assert not await env.store.object_exists(next(iter(rows)))

    # Idempotent re-run from durable state converges without error.
    report = await reconcile_retirements(env.session_factory, env.store)
    assert report.failed_keys == ()


# ---------------------------------------------------------------------------
# Barrier-controlled decision races (§29.4): root vs deletion linearization
# ---------------------------------------------------------------------------


async def test_barrier_root_committing_before_decision_rescues_bytes(
    lifecycle_env,
) -> None:
    env = lifecycle_env
    cut = await _commit_one(env, "ws-bar-a", b"barrier-rescue-bytes")
    plan = await plan_collection(env.session_factory, env.store)
    assert plan.candidate_registry_keys

    released = asyncio.Event()

    async def pause(phase: str) -> None:
        if phase != PHASE_GC_PAUSE_BEFORE_LOCK:
            return
        # The root commits while the collector is paused BEFORE the
        # decision transaction takes the linearization lock: the recheck
        # reads must then see it and rescue the candidate.
        await declare_hold(
            env.session_factory,
            workspace_id="ws-bar-a",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=cut,
            required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        )
        released.set()

    report = await execute_collection(
        env.session_factory, env.store, plan, _test_pause=pause
    )
    assert released.is_set()
    assert report.rescued_count == 1
    assert report.tombstoned == 0
    assert await _retirement_rows(env) == {}
    check = await env.store.check_object(
        plan.candidate_registry_keys[0],
        expected_length=len(b"barrier-rescue-bytes"),
    )
    assert check.available


async def test_barrier_decision_holding_lock_blocks_then_beats_late_root(
    lifecycle_env,
) -> None:
    """True-overlap ordering proof: the decision transaction holds the
    linearization lock (PostgreSQL advisory / SQLite writer lock) while
    a root writer waits; the decision commits first, the root lands
    after it, and the observable outcome is the declared honest one —
    retired availability plus heal-by-restage — never deleted-but-live
    ambiguity."""
    env = lifecycle_env
    cut = await _commit_one(env, "ws-bar-b", b"barrier-order-bytes")
    plan = await plan_collection(env.session_factory, env.store)
    assert plan.candidate_registry_keys
    key = plan.candidate_registry_keys[0]

    decision_in_flight = asyncio.Event()
    release_decision = asyncio.Event()

    async def pause(phase: str) -> None:
        if phase != PHASE_GC_PAUSE_AFTER_ROOTS:
            return
        decision_in_flight.set()
        await release_decision.wait()

    async def late_root() -> None:
        await decision_in_flight.wait()
        await declare_hold(
            env.session_factory,
            workspace_id="ws-bar-b",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=cut,
            required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        )

    collector = asyncio.create_task(
        execute_collection(env.session_factory, env.store, plan, _test_pause=pause)
    )
    root_task = asyncio.create_task(late_root())
    await asyncio.sleep(0.3)  # let the root writer reach the lock

    # The root writer is genuinely blocked by the in-flight decision:
    # not done, and nothing durable is visible to other sessions yet.
    assert not root_task.done()
    async with env.session_factory() as session:
        roots = await session.scalar(
            select(func.count()).select_from(KernelRetentionRoot)
        )
    assert roots == 0

    release_decision.set()
    report = await collector
    await root_task

    # Decision won: the object was swept under its tombstone...
    assert report.swept_deleted == 1
    assert not await env.store.object_exists(key)
    assert (await _retirement_rows(env))[key] == RETIRE_STATE_DELETED

    # ...and the post-decision root observes honest retired state.
    availability = await verify_payload_availability(env.session_factory, env.store)
    assert availability.summary().get(PAYLOAD_STATE_RETIRED, 0) == 1


# ---------------------------------------------------------------------------
# Crash windows + restart reconciliation (X-C11/X-C12)
# ---------------------------------------------------------------------------


async def test_crash_after_decision_and_after_delete_both_reconcile(
    lifecycle_env,
) -> None:
    env = lifecycle_env
    await _commit_one(env, "ws-crash", b"crash-window-bytes")

    # Window 1: crash after the recheck/tombstone commit, before sweep.
    with pytest.raises(InjectedFaultError):
        await _collect(env, _inject_fault_at=PHASE_GC_AFTER_RECHECK)
    rows = await _retirement_rows(env)
    assert len(rows) == 1
    key = next(iter(rows))
    assert rows[key] == RETIRE_STATE_PENDING
    assert await env.store.object_exists(key)  # bytes still there

    factory, store = await _reopen(env)
    report = await reconcile_retirements(factory, store)
    assert report.swept_deleted == 1 and report.failed_keys == ()
    assert not await store.object_exists(key)
    assert (await _retirement_rows(env))[key] == RETIRE_STATE_DELETED

    # Window 2: crash after the physical delete, before the outcome
    # update — the rollback leaves pending + absent bytes.
    await _commit_one(env, "ws-crash", b"crash-window-bytes-2")
    key2 = await _registry_blob_for_length(env, len(b"crash-window-bytes-2"))
    with pytest.raises(InjectedFaultError):
        await _collect(env, _inject_fault_at=PHASE_GC_AFTER_UNLINK)
    assert (await _retirement_rows(env))[key2] == RETIRE_STATE_PENDING

    factory, store = await _reopen(env)
    report = await reconcile_retirements(factory, store)
    assert report.already_absent >= 1  # idempotent convergence, no error
    assert (await _retirement_rows(env))[key2] == RETIRE_STATE_DELETED


# ---------------------------------------------------------------------------
# Physical delete transport failure (X-C10)
# ---------------------------------------------------------------------------


class _TransportFailingStore:
    """Maintenance-view wrapper simulating object-store transport
    failures on delete (transport-exception simulation only; every
    conformance assertion still runs against the real service)."""

    def __init__(self, inner: PayloadMaintenanceStore, failures: int) -> None:
        self._inner = inner
        self._failures = failures

    async def delete_object(self, blob_key: str):
        if self._failures > 0:
            self._failures -= 1
            raise PayloadStageError(
                f"object store DELETE {blob_key} failed: injected transport failure"
            )
        return await self._inner.delete_object(blob_key)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_delete_transport_failure_is_retryable_never_success(
    lifecycle_env,
) -> None:
    env = lifecycle_env
    await _commit_one(env, "ws-transport", b"transport-failure-bytes")
    failing = _TransportFailingStore(env.store, failures=1)

    report = await collect(env.session_factory, failing)
    assert report.failed_keys  # recorded as failure, not success
    failed_key = report.failed_keys[0]
    assert (await _retirement_rows(env))[failed_key] == RETIRE_STATE_FAILED

    # The real store still holds the bytes; a later clean pass retries.
    assert await env.store.object_exists(failed_key)
    report = await _collect(env)
    assert report.failed_keys == ()
    assert (await _retirement_rows(env))[failed_key] == RETIRE_STATE_DELETED


# ---------------------------------------------------------------------------
# Lexical retirement boundary (X-C14; SQLite FTS behavior: X-C15 suite)
# ---------------------------------------------------------------------------


async def test_lexical_retirement_runs_on_both_backends(lifecycle_env) -> None:
    """Lexical retirement is backend-neutral since PR83B2.

    The former PostgreSQL fail-closed boundary is gone: real industrial
    lexical generations exist, their physical artifact is named by the
    manifest, and ``DROP TABLE`` is portable. A dormant unreferenced
    generation retires identically on SQLite and PostgreSQL (the here
    absent physical table makes the drop a no-op on both).
    """
    env = lifecycle_env
    now = datetime.now(timezone.utc)
    lexical_id = "sha256:" + "d0" * 32
    async with env.session_factory() as session:
        session.add(
            KernelLexicalGeneration(
                lexical_generation_id=lexical_id,
                workspace_id="ws-lex",
                source_generation_id="sha256:" + "ab" * 32,
                kernel_commit_id=1,
                snapshot_id="sha256:" + "cd" * 32,
                tokenizer="unicode",
                tokenizer_config_json="{}",
                schema_version="1.0.0",
                fts_table="fts_dormant_marker",
                row_count=0,
                text_char_count=0,
                content_digest="sha256:" + "ef" * 32,
                state="failed",
                created_at=now,
            )
        )
        await session.commit()

    plan = await plan_collection(env.session_factory, env.store)
    assert lexical_id in plan.eligible_lexical_generations

    await execute_collection(env.session_factory, env.store, plan)
    async with env.session_factory() as session:
        surviving = await session.scalar(
            select(func.count()).select_from(KernelLexicalGeneration)
        )
    assert surviving == 0


# ---------------------------------------------------------------------------
# Post-GC snapshot honesty per requirement (§29.1)
# ---------------------------------------------------------------------------


async def test_retired_snapshot_stays_degraded_until_bytes_return(lifecycle_env) -> None:
    env = lifecycle_env
    cut = await _commit_one(env, "ws-honest", b"honest-retirement-bytes")
    report = await _collect(env)
    assert report.swept_deleted == 1

    snapshot = await resolve_snapshot(
        env.session_factory,
        "ws-honest",
        at_commit=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=env.store,
    )
    assert snapshot.completeness == COMPLETENESS_DEGRADED
    assert snapshot.payload_state_counts.get(PAYLOAD_STATE_RETIRED, 0) == 1

    await env.store.stage(b"honest-retirement-bytes")
    snapshot = await resolve_snapshot(
        env.session_factory,
        "ws-honest",
        at_commit=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=env.store,
    )
    assert snapshot.completeness == COMPLETENESS_COMPLETE
