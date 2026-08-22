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
    PHASE_CONNECTOR_APPLIED,
    PHASE_HEAD_ADVANCED,
    PHASE_MANIFEST_INSERTED,
    PHASE_OUTBOX_INSERTED,
    PHASE_PAYLOADS_REGISTERED,
    PHASE_PRE_COMMIT,
    PHASE_RECORDS_INSERTED,
    KernelCommitBatch,
    KernelCommitService,
)
from app.kernel.connector_state import (
    ConnectorCursorAdvancement,
    ConnectorEffects,
    ConnectorInboxEntry,
)
from app.kernel.errors import (
    DuplicateConnectorEventError,
    DuplicateRecordIdentityError,
    InjectedFaultError,
    StaleCursorError,
)
from app.kernel.models import (
    KernelCommitHead,
    KernelCommitManifest,
    KernelConnectorInbox,
    KernelConnectorStream,
    KernelOutbox,
    KernelPayloadObject,
    KernelRecord,
)
from app.kernel.outbox import OutboxIntent
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ObservationRecord, SourceObservationRecord
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


# ---------------------------------------------------------------------------
# Matrix row: PR71B connector convergence effects (T29 dual-backend lane)
# ---------------------------------------------------------------------------


class TestConnectorConvergenceDualBackend:
    """Connector checkpoint/inbox effects over both database profiles.

    The extension must behave identically on the local SQLite profile
    and the industrial PostgreSQL profile: the kernel owns one
    transaction protocol, and the conditional cursor flip relies only
    on rowcount-verified compare-and-set updates that both dialects
    execute inside the commit transaction.
    """

    async def test_connector_unit_commits_atomically(self, commit_env) -> None:
        service = commit_env.service
        record = SourceObservationRecord(
            observer="connector",
            source_ref="src.db.1",
            outcome="policy_updated",
        )
        receipt = await service.commit(
            KernelCommitBatch(
                workspace_id="conn",
                records=(record,),
                outbox=(
                    OutboxIntent(
                        work_kind="source.invalidated",
                        payload={"source_key": "connector:p:a:i1"},
                    ),
                ),
                connector=ConnectorEffects(
                    workspace_id="conn",
                    stream_id="drive:a:root",
                    inbox=(
                        ConnectorInboxEntry(
                            provider_event_id="evt-1",
                            event_kind="policy_changed",
                            applied_state="applied",
                            provider_item_id="i1",
                            provider_seq=1,
                            result={"note": "dual-backend"},
                        ),
                    ),
                    cursor=ConnectorCursorAdvancement(
                        expected_cursor_token=None,
                        new_cursor_token="tok-1",
                        new_cursor_seq=1,
                    ),
                ),
            )
        )
        assert receipt.kernel_commit_id == 1

        async with commit_env.session_factory() as session:
            stream = await session.get(KernelConnectorStream, "drive:a:root")
            inbox = (
                (
                    await session.execute(
                        select(KernelConnectorInbox).where(
                            KernelConnectorInbox.stream_id == "drive:a:root"
                        )
                    )
                )
                .scalars()
                .all()
            )
            outbox = (
                (
                    await session.execute(
                        select(KernelOutbox).where(
                            KernelOutbox.workspace_id == "conn"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert stream is not None
        assert stream.cursor_token == "tok-1"
        assert stream.cursor_seq == 1
        assert stream.applied_kernel_commit_id == receipt.kernel_commit_id
        assert len(inbox) == 1
        assert inbox[0].provider_event_id == "evt-1"
        assert inbox[0].applied_kernel_commit_id == receipt.kernel_commit_id
        assert len(outbox) == 1
        assert outbox[0].kernel_commit_id == receipt.kernel_commit_id

    async def test_stale_cursor_and_duplicate_event_refusals_roll_back(
        self, commit_env
    ) -> None:
        service = commit_env.service

        async def make_record(tag: str) -> SourceObservationRecord:
            return SourceObservationRecord(
                observer="connector",
                source_ref=f"src.db.{tag}",
                outcome="policy_updated",
            )

        await service.commit(
            KernelCommitBatch(
                workspace_id="conn2",
                records=(await make_record("base"),),
                connector=ConnectorEffects(
                    workspace_id="conn2",
                    stream_id="drive:b:root",
                    cursor=ConnectorCursorAdvancement(
                        expected_cursor_token=None,
                        new_cursor_token="tok-1",
                    ),
                ),
            )
        )
        # Deliver one event so a later batch can redeliver its identity.
        await service.commit(
            KernelCommitBatch(
                workspace_id="conn2",
                records=(await make_record("evt"),),
                connector=ConnectorEffects(
                    workspace_id="conn2",
                    stream_id="drive:b:root",
                    inbox=(
                        ConnectorInboxEntry(
                            provider_event_id="evt-dup",
                            event_kind="content_changed",
                            applied_state="applied",
                        ),
                    ),
                    cursor=ConnectorCursorAdvancement(
                        expected_cursor_token="tok-1",
                        new_cursor_token="tok-2",
                    ),
                ),
            )
        )
        manifests_before = await _count(
            commit_env.session_factory, KernelCommitManifest
        )

        # Stale cursor: an advancement based on a token that is no
        # longer current rolls the entire batch back on both backends.
        with pytest.raises(StaleCursorError):
            await service.commit(
                KernelCommitBatch(
                    workspace_id="conn2",
                    records=(await make_record("stale"),),
                    connector=ConnectorEffects(
                        workspace_id="conn2",
                        stream_id="drive:b:root",
                        cursor=ConnectorCursorAdvancement(
                            expected_cursor_token="wrong-token",
                            new_cursor_token="tok-3",
                        ),
                    ),
                )
            )
        # Duplicate event: the inbox unique authority refuses a batch
        # redelivering a recorded event identity.
        with pytest.raises(DuplicateConnectorEventError):
            await service.commit(
                KernelCommitBatch(
                    workspace_id="conn2",
                    records=(await make_record("dup"),),
                    connector=ConnectorEffects(
                        workspace_id="conn2",
                        stream_id="drive:b:root",
                        inbox=(
                            ConnectorInboxEntry(
                                provider_event_id="evt-dup",
                                event_kind="content_changed",
                                applied_state="applied",
                            ),
                        ),
                        cursor=ConnectorCursorAdvancement(
                            expected_cursor_token="tok-2",
                            new_cursor_token="tok-3",
                        ),
                    ),
                )
            )
        manifests_after = await _count(
            commit_env.session_factory, KernelCommitManifest
        )
        assert manifests_before == manifests_after  # nothing from refused batches

    async def test_connector_fault_rolls_back_and_retry_converges(
        self, commit_env
    ) -> None:
        service = commit_env.service

        def batch() -> KernelCommitBatch:
            return KernelCommitBatch(
                workspace_id="conn3",
                records=(
                    SourceObservationRecord(
                        observer="connector",
                        source_ref="src.db.3",
                        outcome="policy_updated",
                    ),
                ),
                connector=ConnectorEffects(
                    workspace_id="conn3",
                    stream_id="drive:c:root",
                    inbox=(
                        ConnectorInboxEntry(
                            provider_event_id="evt-1",
                            event_kind="content_changed",
                            applied_state="applied",
                            provider_seq=1,
                        ),
                    ),
                    cursor=ConnectorCursorAdvancement(
                        expected_cursor_token=None,
                        new_cursor_token="tok-1",
                        new_cursor_seq=1,
                    ),
                ),
            )

        with pytest.raises(InjectedFaultError):
            await service.commit(batch(), _inject_fault_at=PHASE_CONNECTOR_APPLIED)

        async with commit_env.session_factory() as session:
            assert await session.get(KernelConnectorStream, "drive:c:root") is None
            inbox = (
                (await session.execute(select(KernelConnectorInbox))).scalars().all()
            )
        assert inbox == []

        receipt = await service.commit(batch())
        assert receipt.kernel_commit_id == 1  # retry converged cleanly
