"""Dual-backend kernel conformance (PR83A).

One semantic suite, two first-class database profiles: the local SQLite
profile and the industrial PostgreSQL profile. Every scenario in the
PR83A validation matrix runs against both backends through the same
``KernelCommitService`` — there is no second implementation to drift.

PostgreSQL provisioning: tests need ``MARKER_TEST_POSTGRES_ADMIN_URL``
(an asyncpg URL to the server's maintenance database). Each test
creates and drops its own throwaway database through it, so runs are
isolated and repeatable. Without the variable the PostgreSQL params
skip with an actionable reason; when ``MARKER_TEST_POSTGRES_STRICT`` is
set (the conformance runner does this) a missing URL is a FAILURE, so
an invoked industrial target can never pass silently through skips.

Reproduce with::

    python backend/scripts/run_kernel_pg_conformance.py
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import DatabaseState, upgrade_database, verify_database_ready
from app.kernel.commit import (
    PHASE_HEAD_ADVANCED,
    PHASE_MANIFEST_INSERTED,
    PHASE_OUTBOX_INSERTED,
    PHASE_PAYLOADS_REGISTERED,
    PHASE_PRE_COMMIT,
    PHASE_RECORDS_INSERTED,
    KernelCommitBatch,
    KernelCommitService,
)
from app.kernel.errors import DuplicateRecordIdentityError
from app.kernel.models import (
    KernelCommitHead,
    KernelCommitManifest,
    KernelOutbox,
    KernelPayloadObject,
    KernelRecord,
)
from app.kernel.outbox import OutboxIntent
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ObservationRecord
from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)

pytestmark = pytest.mark.asyncio

#: Commit-protocol fault phases that must roll back the whole batch.
_ROLLBACK_PHASES = (
    PHASE_RECORDS_INSERTED,
    PHASE_PAYLOADS_REGISTERED,
    PHASE_MANIFEST_INSERTED,
    PHASE_OUTBOX_INSERTED,
    PHASE_HEAD_ADVANCED,
    PHASE_PRE_COMMIT,
)


@dataclass
class ConformanceEnv:
    """One migrated database + payload store + wired commit service."""

    backend: str
    url: str
    engine: object
    session_factory: async_sessionmaker
    store: LocalPayloadStore
    service: KernelCommitService
    #: server banner when the backend is a real PostgreSQL instance
    server_version: str = ""


def make_observation(observer: str, derivation: dict, payload: bytes | None = None):
    return ObservationRecord(
        observer=observer,
        derivation=derivation,
        payload_bytes=payload,
    )


async def _count(session_factory, *columns) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(*columns))


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def commit_env(backend: str, tmp_path: pathlib.Path):
    """Fresh, fully migrated database + wired kernel commit service."""
    async with provisioned_database(backend, (tmp_path / "kernel.db").as_posix()) as prov:
        url = prov.url
        result = await upgrade_database(url=url)
        assert result.to_revision, "bootstrap must reach a migration head"

        engine = create_async_engine(url, **engine_kwargs_for(backend))
        # Real-backend confirmation: the engine's dialect must match the
        # profile the test claims to exercise (guards against URL mixups).
        assert engine.dialect.name == backend

        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        store = LocalPayloadStore(tmp_path / "payloads")
        service = KernelCommitService(session_factory, payload_store=store)

        server_version = ""
        if backend == "postgresql":
            async with engine.connect() as conn:
                server_version = await conn.scalar(text("SELECT version()"))

        try:
            yield ConformanceEnv(
                backend=backend,
                url=url,
                engine=engine,
                session_factory=session_factory,
                store=store,
                service=service,
                server_version=server_version,
            )
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Matrix row: clean bootstrap + real-backend confirmation
# ---------------------------------------------------------------------------


async def test_bootstrap_reaches_current_head(commit_env, backend) -> None:
    """Both backends initialize to the current schema through Alembic."""
    status = await verify_database_ready(commit_env.url)
    assert status.state is DatabaseState.CURRENT, status.describe()
    if backend == "postgresql":
        assert "PostgreSQL" in commit_env.server_version
        # The readiness gate itself ran against the real server.
        assert commit_env.engine.dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# Matrix row: basic authoritative commit, equivalent visible result
# ---------------------------------------------------------------------------


async def test_commit_visible_result(commit_env) -> None:
    service, factory = commit_env.service, commit_env.session_factory
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="conf",
            records=(make_observation("op-a", {"k": 1}, payload=b"bytes-a"),),
            outbox=(OutboxIntent(work_kind="index", payload={"target": "conf"}),),
        )
    )
    assert (receipt.kernel_commit_id, receipt.parent_kernel_commit_id) == (1, 0)
    assert receipt.record_ids and receipt.payload_blob_keys

    async with factory() as session:
        head = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == "conf"
            )
        )
        record = await session.get(KernelRecord, receipt.record_ids[0])
        manifest = await session.get(
            KernelCommitManifest, ("conf", receipt.kernel_commit_id)
        )
        payload = await session.get(
            KernelPayloadObject, receipt.payload_blob_keys[0]
        )
        outbox_rows = (
            (
                await session.execute(
                    select(KernelOutbox).where(
                        KernelOutbox.workspace_id == "conf"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert head == 1
    assert record is not None and record.kernel_commit_id == 1
    assert manifest is not None and manifest.parent_kernel_commit_id == 0
    assert payload is not None
    assert len(outbox_rows) == 1
    assert receipt.outbox_ids == (outbox_rows[0].id,)
    # Durable-before-reference: staged bytes verify in the store.
    check = await commit_env.store.check_object(receipt.payload_blob_keys[0])
    assert check.available


# ---------------------------------------------------------------------------
# Matrix rows: monotonic identity, concurrency without forks
# ---------------------------------------------------------------------------


async def test_concurrent_same_head_writers_never_fork(commit_env) -> None:
    service, factory = commit_env.service, commit_env.session_factory
    writers = 12

    async def commit_one(i: int):
        return await commit_env.service.commit(
            KernelCommitBatch(
                workspace_id="race",
                records=(make_observation(f"op-{i}", {"i": i}),),
            )
        )

    receipts = await asyncio.gather(*(commit_one(i) for i in range(writers)))
    ids = sorted(r.kernel_commit_id for r in receipts)
    assert ids == list(range(1, writers + 1))

    parents = {r.kernel_commit_id: r.parent_kernel_commit_id for r in receipts}
    assert all(parents[i] == i - 1 for i in ids)

    async with factory() as session:
        head = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == "race"
            )
        )
        manifests = (
            (
                await session.execute(
                    select(
                        KernelCommitManifest.kernel_commit_id,
                        KernelCommitManifest.parent_kernel_commit_id,
                    ).where(KernelCommitManifest.workspace_id == "race")
                )
            )
            .all()
        )
        duplicate_ids = (
            await session.scalar(
                select(func.count()).select_from(
                    select(KernelCommitManifest)
                    .where(KernelCommitManifest.workspace_id == "race")
                    .subquery()
                )
            )
        )
    assert head == writers
    assert sorted(m[0] for m in manifests) == list(range(1, writers + 1))
    assert all(m[1] == m[0] - 1 for m in manifests)
    assert duplicate_ids == writers  # composite PK forbids duplicates anyway


# ---------------------------------------------------------------------------
# Matrix row: same-request idempotent replay
# ---------------------------------------------------------------------------


async def test_replay_of_accepted_request_resolves_consistently(commit_env) -> None:
    service = commit_env.service
    batch = KernelCommitBatch(
        workspace_id="replay",
        records=(make_observation("op", {"k": 1}),),
        outbox=(OutboxIntent(work_kind="index", payload={"ws": "replay"}),),
    )
    first = await service.commit(batch)
    # Transport-level replay of the identical accepted request: the
    # kernel resolves it consistently — a typed conflict, never a
    # second copy of history.
    with pytest.raises(DuplicateRecordIdentityError):
        await service.commit(batch)

    assert await _count(commit_env.session_factory, KernelRecord) == 1
    assert await _count(commit_env.session_factory, KernelCommitManifest) == 1
    assert await _count(commit_env.session_factory, KernelOutbox) == 1
    assert first.kernel_commit_id == 1


# ---------------------------------------------------------------------------
# Matrix row: conflicting reuse of the same request identity
# ---------------------------------------------------------------------------


async def test_conflicting_identity_reuse_fails_without_mutation(commit_env) -> None:
    service = commit_env.service
    accepted = KernelCommitBatch(
        workspace_id="conflict",
        records=(make_observation("op", {"k": 1}, payload=b"accepted-bytes"),),
    )
    receipt = await service.commit(accepted)

    # Same semantic identity, different payload bytes: conflicting reuse
    # must fail clearly, not silently mutate history.
    conflicting = KernelCommitBatch(
        workspace_id="conflict",
        records=(make_observation("op", {"k": 1}, payload=b"different-bytes"),),
    )
    with pytest.raises(DuplicateRecordIdentityError):
        await service.commit(conflicting)

    async with commit_env.session_factory() as session:
        record = await session.get(KernelRecord, receipt.record_ids[0])
        head = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == "conflict"
            )
        )
    assert record is not None
    assert record.payload_byte_hash is not None
    assert head == 1


# ---------------------------------------------------------------------------
# Matrix row: transaction rollback leaves no partial authoritative state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fault_phase", _ROLLBACK_PHASES)
async def test_fault_before_commit_rolls_back_everything(
    commit_env, fault_phase
) -> None:
    service = commit_env.service
    baseline_head = 3
    for i in range(baseline_head):
        await service.commit(
            KernelCommitBatch(
                workspace_id="rollback",
                records=(make_observation(f"op-{i}", {"i": i}),),
            )
        )
    with pytest.raises(Exception, match="fault"):
        await service.commit(
            KernelCommitBatch(
                workspace_id="rollback",
                records=(
                    make_observation("op-new", {"i": 99}, payload=b"doomed-bytes"),
                ),
                outbox=(OutboxIntent(work_kind="index", payload={"x": 1}),),
            ),
            _inject_fault_at=fault_phase,
        )
    assert await _count(commit_env.session_factory, KernelRecord) == baseline_head
    assert (
        await _count(commit_env.session_factory, KernelCommitManifest)
        == baseline_head
    )
    assert await _count(commit_env.session_factory, KernelOutbox) == 0
    async with commit_env.session_factory() as session:
        head = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == "rollback"
            )
        )
    assert head == baseline_head
    # The staged doomed bytes are safe garbage: verified in the store,
    # with no visible database reference to them (registry row rolled
    # back together with the transaction).
    from app.utils.canonical import payload_byte_hash

    orphan_key = payload_byte_hash(b"doomed-bytes")
    assert (await commit_env.store.check_object(orphan_key)).available
    assert await _count(commit_env.session_factory, KernelPayloadObject) == 0


# ---------------------------------------------------------------------------
# Matrix row: payload staged, database transaction fails
# ---------------------------------------------------------------------------


async def test_staged_payload_db_failure_leaves_safe_orphan(commit_env) -> None:
    service, store = commit_env.service, commit_env.store
    with pytest.raises(Exception, match="fault"):
        await service.commit(
            KernelCommitBatch(
                workspace_id="orphan",
                records=(make_observation("op", {"k": 1}, payload=b"orphaned"),),
            ),
            _inject_fault_at=PHASE_PRE_COMMIT,
        )
    from app.utils.canonical import payload_byte_hash

    key = payload_byte_hash(b"orphaned")
    check = await store.check_object(key)
    # Orphaned staged bytes are acceptable — verified, unreferenced.
    assert check.available
    assert await _count(commit_env.session_factory, KernelPayloadObject) == 0
    assert await _count(commit_env.session_factory, KernelRecord) == 0
    # Retrying the same request after the failure succeeds cleanly.
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="orphan",
            records=(make_observation("op", {"k": 1}, payload=b"orphaned"),),
        )
    )
    assert receipt.kernel_commit_id == 1
    assert receipt.payload_blob_keys == (key,)


# ---------------------------------------------------------------------------
# Matrix row: declared-hash reuse does not re-publish bytes
# ---------------------------------------------------------------------------


async def test_declared_hash_reuses_verified_object(commit_env) -> None:
    service, store = commit_env.service, commit_env.store
    first = await service.commit(
        KernelCommitBatch(
            workspace_id="reuse",
            records=(make_observation("op", {"k": 1}, payload=b"shared-bytes"),),
        )
    )
    key = first.payload_blob_keys[0]
    calls_before = store.stage_calls

    second = await service.commit(
        KernelCommitBatch(
            workspace_id="reuse",
            records=(
                make_observation(
                    "op-2",
                    {"k": 2},
                ),
            ),
        )
    )
    # A record declaring the already-published hash reuses the verified
    # object without staging new bytes.
    third = await service.commit(
        KernelCommitBatch(
            workspace_id="reuse",
            records=(
                ObservationRecord(
                    observer="op-3",
                    derivation={"k": 3},
                    declared_payload_hash=key,
                ),
            ),
        )
    )
    # Neither the payload-free record nor the declared-hash record
    # staged new bytes: only byte-bearing records call stage().
    assert store.stage_calls == calls_before
    assert third.payload_blob_keys == (key,)  # declared hash reused the object
    registry = await _count(commit_env.session_factory, KernelPayloadObject)
    assert registry == 1
