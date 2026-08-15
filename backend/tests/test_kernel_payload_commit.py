"""Payload durability commit integration tests (V3.2 PR64, plan B/C).

Durable-before-reference, registry truth, evidence-vs-byte identity,
declared-hash reuse, and no-false-completeness at the commit boundary.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.models import KernelPayloadObject, KernelRecord
from app.kernel.payloads import LOCAL_STORE_PROFILE
from app.kernel.reconcile import (
    PAYLOAD_STATE_AVAILABLE,
    PAYLOAD_STATE_METADATA_ONLY,
    verify_payload_availability,
)
from app.kernel.records import ObservationRecord

pytestmark = pytest.mark.asyncio


def make_observation(
    observer: str, derivation: dict, payload: bytes | None = None, **overrides
) -> ObservationRecord:
    fields = {
        "observer": observer,
        "derivation": derivation,
        "summary": "",
        "context": {},
        "payload_bytes": payload,
    }
    fields.update(overrides)
    return ObservationRecord(**fields)


async def _records(factory: async_sessionmaker, **filters) -> list[KernelRecord]:
    async with factory() as session:
        rows = (
            await session.execute(select(KernelRecord).filter_by(**filters))
        ).scalars().all()
    return list(rows)


async def _registry(factory: async_sessionmaker) -> list[KernelPayloadObject]:
    async with factory() as session:
        rows = (await session.execute(select(KernelPayloadObject))).scalars().all()
    return list(rows)


async def test_payload_bytes_are_durable_before_reference(payload_env) -> None:
    factory, store, service = payload_env
    payload = b"%PDF-1.7 fake document bytes"
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(make_observation("obs-1", {"src": "doc.pdf"}, payload=payload),),
        )
    )
    # Receipt advertises exactly the staged content identity.
    assert len(receipt.payload_blob_keys) == 1
    blob_key = receipt.payload_blob_keys[0]

    # Registry row: durable object, correct profile and locator.
    (row,) = await _registry(factory)
    assert row.blob_key == blob_key
    assert row.payload_length == len(payload)
    assert row.store_profile == LOCAL_STORE_PROFILE
    assert row.storage_locator == store.locator_for(blob_key)

    # Bytes re-open and verify from the immutable final location.
    assert await store.read(blob_key) == payload

    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.payload_backed_complete is True
    assert availability.record_states[0].state == PAYLOAD_STATE_AVAILABLE


async def test_distinct_evidence_shares_bytes_without_collapsing(payload_env) -> None:
    factory, store, service = payload_env
    shared = b"identical witness bytes"
    records = (
        make_observation("obs-a", {"derivation": "crop-left"}, payload=shared),
        make_observation("obs-b", {"derivation": "crop-right"}, payload=shared),
    )
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws", records=records)
    )
    # One content identity, two distinct evidence records.
    assert receipt.payload_blob_keys == tuple(sorted(receipt.payload_blob_keys))
    assert len(receipt.payload_blob_keys) == 1
    assert len(await _registry(factory)) == 1
    rows = await _records(factory, workspace_id="ws")
    assert len(rows) == 2
    assert len({row.identity_hash for row in rows}) == 2  # evidence NOT merged
    assert {row.payload_byte_hash for row in rows} == set(receipt.payload_blob_keys)
    assert await store.read(receipt.payload_blob_keys[0]) == shared


async def test_declared_hash_without_staging_is_metadata_only(payload_env) -> None:
    factory, store, service = payload_env
    from app.utils.canonical import payload_byte_hash

    declared = payload_byte_hash(b"bytes staged elsewhere")
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(
                make_observation(
                    "obs-1", {"src": "remote"}, declared_payload_hash=declared
                ),
            ),
        )
    )
    assert receipt.payload_blob_keys == ()  # nothing claimed available
    assert await _registry(factory) == []
    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.record_states[0].state == PAYLOAD_STATE_METADATA_ONLY
    assert availability.payload_backed_complete is False


async def test_declared_hash_reuses_verified_staged_object(payload_env) -> None:
    factory, store, service = payload_env
    payload = b"staged once, referenced twice"
    first = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(make_observation("obs-1", {"src": "a"}, payload=payload),),
        )
    )
    blob_key = first.payload_blob_keys[0]

    # A later record declares the same hash without supplying bytes.
    second = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(
                make_observation(
                    "obs-2", {"src": "b"}, declared_payload_hash=blob_key
                ),
            ),
        )
    )
    assert second.payload_blob_keys == (blob_key,)
    assert len(await _registry(factory)) == 1  # one object, two referencing records
    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.payload_backed_complete is True


async def test_no_store_preserves_pr63a_hash_only_behavior(kernel_env) -> None:
    """Without a store the commit authority stays metadata-only (PR63A)."""
    service = KernelCommitService(kernel_env)
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(
                make_observation("obs-1", {"src": "x"}, payload=b"unstored bytes"),
            ),
        )
    )
    assert receipt.payload_blob_keys == ()
    rows = await _records(kernel_env, workspace_id="ws")
    assert rows[0].payload_byte_hash is not None  # hash truth preserved
    assert rows[0].payload_length == len(b"unstored bytes")
    async with kernel_env() as session:
        registry = (await session.execute(select(KernelPayloadObject))).scalars().all()
    assert registry == []


async def test_failed_commit_leaves_unreferenced_object_only(payload_env) -> None:
    factory, store, service = payload_env
    payload = b"orphaned by rollback"
    with pytest.raises(Exception, match="injected fault"):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(make_observation("obs-1", {"src": "x"}, payload=payload),),
            ),
            _inject_fault_at="pre-commit",
        )
    # No committed truth, no registry row — only unreachable bytes.
    assert await _records(factory, workspace_id="ws") == []
    assert await _registry(factory) == []
    keys = await store.list_objects()
    assert len(keys) == 1  # unreachable bytes only, never referenced

    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.orphan_objects == tuple(keys)
    assert availability.record_states == ()


async def test_staging_failure_prevents_database_mutation(payload_env, tmp_path) -> None:
    factory, _store, service = payload_env
    from app.kernel.payloads import LocalPayloadStore, PHASE_AFTER_FSYNC

    broken = LocalPayloadStore(
        tmp_path / "broken-store", fault_phases={PHASE_AFTER_FSYNC}
    )
    service._payload_store = broken
    from app.kernel.errors import InjectedFaultError

    with pytest.raises(InjectedFaultError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    make_observation("obs-1", {"src": "x"}, payload=b"never committed"),
                ),
            )
        )
    assert await _records(factory, workspace_id="ws") == []
    from app.kernel.replay import read_head

    assert await read_head(factory, "ws") == 0
