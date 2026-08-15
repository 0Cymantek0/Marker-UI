"""Restart reconciliation and availability truth tests (plan 7.4, 7.7).

Fresh engine/service instances (process-restart equivalents) prove that
payload availability, outbox state, orphan detection, and degraded
classification reconstruct from durable state alone — never from
process memory. Database-chain integrity and payload availability stay
two separate dimensions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.outbox import (
    OUTBOX_STATE_PENDING,
    OutboxIntent,
    claim,
    list_outbox,
)
from app.kernel.payloads import LocalPayloadStore
from app.kernel.reconcile import (
    PAYLOAD_STATE_AVAILABLE,
    PAYLOAD_STATE_CORRUPT,
    PAYLOAD_STATE_METADATA_ONLY,
    PAYLOAD_STATE_MISSING,
    reconcile,
    reconcile_after_restart,
    verify_payload_availability,
)
from app.kernel.records import ObservationRecord
from app.kernel.replay import verify_history

pytestmark = pytest.mark.asyncio

PAYLOAD_A = b"committed document bytes A"
PAYLOAD_B = b"committed document bytes B"


def _obs(observer: str, payload: bytes | None = None, **overrides) -> ObservationRecord:
    fields = {
        "observer": observer,
        "derivation": {"suite": "reconcile"},
        "summary": "",
        "context": {},
        "payload_bytes": payload,
    }
    fields.update(overrides)
    return ObservationRecord(**fields)


async def _commit(service: KernelCommitService, records, outbox=()) -> None:
    await service.commit(
        KernelCommitBatch(workspace_id="ws", records=records, outbox=outbox)
    )


async def _restart_env(tmp_path: Path):
    """Fresh process view: new engine + new store instance, same files."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'kernel.db').as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalPayloadStore(tmp_path / "payloads")
    return factory, store, engine


# ---------------------------------------------------------------------------
# 7.4 restart and reconciliation
# ---------------------------------------------------------------------------


async def test_committed_payload_survives_restart_readable_and_verifiable(
    payload_env, tmp_path
) -> None:
    _factory, _store, service = payload_env
    from app.utils.canonical import payload_byte_hash

    await _commit(service, (_obs("obs-1", payload=PAYLOAD_A),))

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        blob_key = payload_byte_hash(PAYLOAD_A)
        assert await store2.read(blob_key) == PAYLOAD_A
        availability = await verify_payload_availability(factory2, store2, workspace_id="ws")
        assert availability.payload_backed_complete is True
    finally:
        await engine2.dispose()


async def test_pending_outbox_survives_restart(payload_env, tmp_path) -> None:
    _factory, _store, service = payload_env
    await _commit(
        service,
        (_obs("obs-1", payload=PAYLOAD_A),),
        outbox=(OutboxIntent(work_kind="materialize", payload={"gen": 1}),),
    )

    factory2, _store2, engine2 = await _restart_env(tmp_path)
    try:
        pending = await list_outbox(factory2)
        assert len(pending) == 1 and pending[0].state == OUTBOX_STATE_PENDING
        # Claimed pre-crash, never acked: restart reconciliation must
        # return it to pending (at-least-once), not lose or fake it.
        await claim(factory2, pending[0].id)
        report = await reconcile_after_restart(factory2, _store2)
        assert report.in_flight_reset == 1
        after = await list_outbox(factory2)
        assert after[0].state == OUTBOX_STATE_PENDING
    finally:
        await engine2.dispose()


async def test_pre_commit_orphan_is_identified_as_unreachable(
    payload_env, tmp_path
) -> None:
    factory, store, service = payload_env
    with pytest.raises(Exception, match="injected fault"):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(_obs("obs-orphan", payload=b"unreachable bytes"),),
            ),
            _inject_fault_at="pre-commit",
        )

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        availability = await verify_payload_availability(factory2, store2)
        assert len(availability.orphan_objects) == 1
        assert availability.record_states == ()
        # Repair never deletes objects (GC is PR65): rerunning is stable.
        report = await reconcile(factory2, store2, tmp_older_than_seconds=0)
        assert report.availability.orphan_objects == availability.orphan_objects
    finally:
        await engine2.dispose()


async def test_stale_tmp_residue_cleaned_without_touching_live_objects(
    payload_env, tmp_path
) -> None:
    factory, store, service = payload_env
    await _commit(
        service,
        (_obs("obs-1", payload=PAYLOAD_A), _obs("obs-2", payload=PAYLOAD_B)),
    )

    residue = tmp_path / "payloads" / "tmp" / "interrupted.tmp"
    residue.write_bytes(b"half written")
    stale = residue.stat().st_mtime - 7200
    os.utime(residue, (stale, stale))

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        report = await reconcile_after_restart(factory2, store2, tmp_older_than_seconds=3600)
        assert report.tmp_removed == ("interrupted.tmp",)
        assert not residue.exists()
        # Live immutable data untouched.
        availability = report.availability
        assert availability.payload_backed_complete is True
        assert len(availability.blob_states) == 2
    finally:
        await engine2.dispose()


async def test_removed_committed_payload_is_degraded_not_complete(
    payload_env, tmp_path
) -> None:
    factory, store, service = payload_env
    await _commit(service, (_obs("obs-1", payload=PAYLOAD_A),))
    key = (await store.list_objects())[0]
    obj = store.object_path(key)
    obj.chmod(stat.S_IREAD | stat.S_IWRITE)
    obj.unlink()

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        availability = await verify_payload_availability(factory2, store2, workspace_id="ws")
        assert availability.record_states[0].state == PAYLOAD_STATE_MISSING
        assert availability.payload_backed_complete is False
        # Repair does not fabricate the bytes back.
        report = await reconcile(factory2, store2)
        assert report.availability.record_states[0].state == PAYLOAD_STATE_MISSING
    finally:
        await engine2.dispose()


async def test_modified_committed_payload_is_corrupt_not_complete(
    payload_env, tmp_path
) -> None:
    factory, store, service = payload_env
    await _commit(service, (_obs("obs-1", payload=PAYLOAD_A),))
    key = (await store.list_objects())[0]
    obj = store.object_path(key)
    obj.chmod(stat.S_IREAD | stat.S_IWRITE)
    obj.write_bytes(PAYLOAD_A + b"appended tampering")

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        availability = await verify_payload_availability(factory2, store2, workspace_id="ws")
        assert availability.record_states[0].state == PAYLOAD_STATE_CORRUPT
        assert availability.payload_backed_complete is False
    finally:
        await engine2.dispose()


async def test_truncated_committed_payload_is_corrupt(payload_env, tmp_path) -> None:
    factory, store, service = payload_env
    await _commit(service, (_obs("obs-1", payload=PAYLOAD_B),))
    key = (await store.list_objects())[0]
    obj = store.object_path(key)
    obj.chmod(stat.S_IREAD | stat.S_IWRITE)
    obj.write_bytes(PAYLOAD_B[:5])

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        availability = await verify_payload_availability(factory2, store2, workspace_id="ws")
        assert availability.record_states[0].state == PAYLOAD_STATE_CORRUPT
    finally:
        await engine2.dispose()


async def test_repair_reruns_do_not_oscillate(payload_env, tmp_path) -> None:
    factory, store, service = payload_env
    await _commit(service, (_obs("obs-1", payload=PAYLOAD_A),))
    await _commit(service, (_obs("declared", declared_payload_hash="sha256:" + "0" * 64),))
    key = (await store.list_objects())[0]
    store.object_path(key).chmod(stat.S_IREAD | stat.S_IWRITE)

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        first = await reconcile_after_restart(factory2, store2, tmp_older_than_seconds=0)
        second = await reconcile_after_restart(factory2, store2, tmp_older_than_seconds=0)
        states1 = {r.record_id: r.state for r in first.availability.record_states}
        states2 = {r.record_id: r.state for r in second.availability.record_states}
        assert states1 == states2
        assert set(states1.values()) == {PAYLOAD_STATE_AVAILABLE, PAYLOAD_STATE_METADATA_ONLY}
    finally:
        await engine2.dispose()


# ---------------------------------------------------------------------------
# 7.7 integrity dimensions stay separate
# ---------------------------------------------------------------------------


async def test_valid_db_history_with_missing_payload_is_degraded_not_broken(
    payload_env, tmp_path
) -> None:
    """DB chain integrity and payload availability must not be conflated."""
    factory, store, service = payload_env
    await _commit(service, (_obs("obs-1", payload=PAYLOAD_A),))
    key = (await store.list_objects())[0]
    obj = store.object_path(key)
    obj.chmod(stat.S_IREAD | stat.S_IWRITE)
    obj.unlink()

    factory2, store2, engine2 = await _restart_env(tmp_path)
    try:
        history = await verify_history(factory2, "ws")
        assert history.ok, "database chain must remain valid after external removal"
        availability = await verify_payload_availability(factory2, store2, workspace_id="ws")
        assert availability.payload_backed_complete is False
        assert availability.degraded[0].state == PAYLOAD_STATE_MISSING
    finally:
        await engine2.dispose()


async def test_metadata_only_history_is_not_payload_backed_complete(
    payload_env,
) -> None:
    factory, store, service = payload_env
    await _commit(
        service,
        (_obs("obs-1", declared_payload_hash="sha256:" + "1" * 64),),
    )
    history = await verify_history(factory, "ws")
    assert history.ok
    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.record_states[0].state == PAYLOAD_STATE_METADATA_ONLY
    assert availability.payload_backed_complete is False
