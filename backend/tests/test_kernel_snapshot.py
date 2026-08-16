"""Kernel snapshot resolution tests (V3.2 PR65A, plan matrix 10.1).

A snapshot is a committed sequence boundary: membership by
``kernel_commit_id <= K`` only, stability across later commits, honest
completeness against payload availability, and explicit unbound future
bindings (no fabricated PR70 identities).
"""

from __future__ import annotations

import inspect
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    InvalidSnapshotCutError,
    SnapshotIntegrityError,
    SnapshotRequirementError,
)
from app.kernel.records import (
    EDGE_KIND_EVIDENCE_FOR,
    ClaimAssertionRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.snapshots import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_DEGRADED,
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
    PAYLOAD_REQUIREMENT_REPLAYABLE,
    UNBOUND_FUTURE_FIELDS,
    KernelSnapshot,
    resolve_snapshot,
)
from tests._payload_tamper import corrupt_object, make_unreadable, unlink_object

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


def _observation(tag: str, payload: bytes | None = None) -> ObservationRecord:
    return ObservationRecord(
        observer="obs", derivation={"tag": tag}, payload_bytes=payload
    )


async def seed(service: KernelCommitService) -> tuple[str, ...]:
    """Three commits in ws-a plus one in ws-b; returns observation ids."""
    a1 = _assertion("a1")
    o1 = _observation("o1")
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(a1, o1),
            edges=(
                KernelEdge(
                    edge_kind=EDGE_KIND_EVIDENCE_FOR,
                    source_ref=o1.record_id,
                    target_ref=a1.record_id,
                ),
            ),
        )
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a2"),))
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_observation("o2"),))
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-b", records=(_assertion("b1"),))
    )
    return (o1.record_id,)


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


def _sql(db_path: Path, statement: str, params: tuple = ()) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(statement, params)
        conn.commit()


# ---------------------------------------------------------------------------
# cuts, membership, identity
# ---------------------------------------------------------------------------


async def test_empty_workspace_resolves_honest_head_zero(
    kernel_env: async_sessionmaker,
) -> None:
    snapshot = await resolve_snapshot(kernel_env, "ws-empty")
    assert snapshot.kernel_commit_id == 0
    assert snapshot.record_count == 0 and snapshot.edge_count == 0
    assert snapshot.commit_count == 0
    assert snapshot.completeness == COMPLETENESS_COMPLETE
    assert snapshot.required_payload_state == PAYLOAD_REQUIREMENT_METADATA_ONLY
    assert snapshot.payload_backed_complete is None  # not evaluated, not claimed
    assert snapshot.kernel_schema_versions and snapshot.canonicalization_profiles


async def test_head_snapshot_membership_and_stable_identity(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed(service)

    first = await resolve_snapshot(factory, "ws-a")
    second = await resolve_snapshot(factory, "ws-a")

    assert first.kernel_commit_id == 3
    assert first.record_count == 4  # a1, o1, a2, o2
    assert first.edge_count == 1
    assert first.commit_count == 3
    assert first.record_class_counts == {"claim_assertion": 2, "observation": 2}
    assert first.snapshot_id == second.snapshot_id  # deterministic identity


async def test_historical_cut_resolves_without_wall_clock(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed(service)

    historical = await resolve_snapshot(factory, "ws-a", at_commit=2)
    assert historical.kernel_commit_id == 2
    assert historical.record_count == 3  # a1, o1, a2
    assert historical.edge_count == 1
    assert historical.commit_count == 2

    empty_cut = await resolve_snapshot(factory, "ws-a", at_commit=0)
    assert empty_cut.record_count == 0 and empty_cut.commit_count == 0


async def test_future_and_invalid_cuts_rejected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed(service)

    with pytest.raises(InvalidSnapshotCutError):
        await resolve_snapshot(factory, "ws-a", at_commit=4)
    with pytest.raises(InvalidSnapshotCutError):
        await resolve_snapshot(factory, "ws-a", at_commit=-1)
    with pytest.raises(InvalidSnapshotCutError):
        await resolve_snapshot(factory, "ws-a", at_commit="2")  # type: ignore[arg-type]


async def test_pinned_snapshot_stable_while_commits_proceed(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed(service)

    pinned = await resolve_snapshot(factory, "ws-a", at_commit=2)
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a3"),))
    )

    again = await resolve_snapshot(factory, "ws-a", at_commit=2)
    assert again.snapshot_id == pinned.snapshot_id
    assert again.record_count == pinned.record_count == 3
    assert again.kernel_commit_id == 2  # the new commit never leaks into the pin

    head = await resolve_snapshot(factory, "ws-a")
    assert head.kernel_commit_id == 4 and head.record_count == 5
    assert head.snapshot_id != pinned.snapshot_id  # different cut, different id


async def test_workspace_isolation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed(service)

    snapshot_b = await resolve_snapshot(factory, "ws-b")
    assert snapshot_b.kernel_commit_id == 1
    assert snapshot_b.record_count == 1
    assert snapshot_b.record_class_counts == {"claim_assertion": 1}


async def test_incoherent_metadata_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed(service)

    _sql(
        _db_path(factory),
        "DELETE FROM kernel_commit_manifests WHERE workspace_id = 'ws-a' "
        "AND kernel_commit_id = 2",
    )
    with pytest.raises(SnapshotIntegrityError):
        await resolve_snapshot(factory, "ws-a", at_commit=2)

    _sql(
        _db_path(factory),
        "UPDATE kernel_commit_manifests SET record_count = 99 "
        "WHERE workspace_id = 'ws-a' AND kernel_commit_id = 1",
    )
    with pytest.raises(SnapshotIntegrityError):
        await resolve_snapshot(factory, "ws-a")


# ---------------------------------------------------------------------------
# payload honesty
# ---------------------------------------------------------------------------


async def test_available_payloads_yield_complete_snapshot(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(_observation("with-bytes", payload=b"evidence" * 8),),
        )
    )
    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == COMPLETENESS_COMPLETE
    assert snapshot.payload_backed_complete is True
    assert snapshot.payload_state_counts == {"available": 1, "missing": 0,
                                             "corrupt": 0, "metadata_only": 0,
                                             "retired": 0}


async def test_missing_payload_degrades_not_completes(payload_env: tuple) -> None:
    factory, store, service = payload_env
    record = _observation("with-bytes", payload=b"evidence" * 8)
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    unlink_object(store, receipt.payload_blob_keys[0])

    degraded = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_REPLAYABLE,
        payload_store=store,
    )
    assert degraded.completeness == COMPLETENESS_DEGRADED
    assert degraded.payload_backed_complete is False
    assert degraded.payload_state_counts["missing"] == 1
    assert record.record_id in degraded.degraded_record_ids

    # metadata-only requirement stays complete but reports availability honestly
    metadata_view = await resolve_snapshot(
        factory, "ws-a", payload_store=store
    )
    assert metadata_view.completeness == COMPLETENESS_COMPLETE
    assert metadata_view.payload_state_counts["missing"] == 1


async def test_corrupt_payload_degrades_and_stays_visible(payload_env: tuple) -> None:
    factory, store, service = payload_env
    record = _observation("with-bytes", payload=b"evidence" * 8)
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    corrupt_object(store, receipt.payload_blob_keys[0], b"tampered-bytes")

    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == COMPLETENESS_DEGRADED
    assert snapshot.payload_state_counts["corrupt"] == 1
    assert record.record_id in snapshot.degraded_record_ids


async def test_same_length_tamper_detected_by_hash(payload_env: tuple) -> None:
    """Corruption that preserves byte length must still classify corrupt:
    length agreement alone is not integrity."""
    factory, store, service = payload_env
    payload = os.urandom(256)
    record = _observation("exact-length", payload=payload)
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    path = store.object_path(receipt.payload_blob_keys[0])
    tampered = bytearray(payload)
    tampered[0] ^= 0xFF  # same length, different bytes
    corrupt_object(store, receipt.payload_blob_keys[0], bytes(tampered))
    assert path.is_file()  # still present, still readable: pure byte mismatch

    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == COMPLETENESS_DEGRADED
    assert snapshot.payload_state_counts["corrupt"] == 1


async def test_unreadable_payload_never_reports_available(
    payload_env: tuple,
) -> None:
    """Bytes that exist but cannot be read are a distinct failure mode:
    present-but-unverifiable must degrade (corrupt bucket), never pass an
    availability check, and never surface as missing."""
    factory, store, service = payload_env
    record = _observation("with-bytes", payload=b"evidence" * 8)
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    path = store.object_path(receipt.payload_blob_keys[0])

    with make_unreadable(path):
        check = await store.check_object(receipt.payload_blob_keys[0])
        assert check.exists and not check.available
        assert not (check.length_ok and check.hash_ok)

        snapshot = await resolve_snapshot(
            factory,
            "ws-a",
            required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
            payload_store=store,
        )
        assert snapshot.completeness == COMPLETENESS_DEGRADED
        assert snapshot.payload_state_counts["corrupt"] == 1
        assert snapshot.payload_state_counts["missing"] == 0
        assert snapshot.payload_backed_complete is False
        assert record.record_id in snapshot.degraded_record_ids

    # the condition was physical and temporary: readability restores truth
    healed = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert healed.completeness == COMPLETENESS_COMPLETE
    assert healed.payload_state_counts["available"] == 1


async def test_metadata_only_reference_not_promoted(payload_env: tuple) -> None:
    factory, store, service = payload_env
    # declared hash whose bytes were never staged in the local profile
    declared = "sha256:" + "ab" * 32
    record = ObservationRecord(
        observer="obs", derivation={"tag": "declared"}, declared_payload_hash=declared
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )

    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == COMPLETENESS_DEGRADED
    assert snapshot.payload_state_counts["metadata_only"] == 1
    assert snapshot.payload_backed_complete is False


async def test_inspectable_requirement_needs_store(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed(service)
    with pytest.raises(SnapshotRequirementError):
        await resolve_snapshot(
            factory,
            "ws-a",
            required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        )
    with pytest.raises(SnapshotRequirementError):
        await resolve_snapshot(factory, "ws-a", required_payload_state="bogus")


async def test_historical_payload_scan_bounded_to_cut(payload_env: tuple) -> None:
    factory, store, service = payload_env
    early = _observation("early", payload=b"early-bytes" * 4)
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(early,))
    )
    later = _observation("later", payload=b"later-bytes" * 4)
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(later,))
    )
    unlink_object(store, receipt.payload_blob_keys[0])

    pinned = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert pinned.completeness == COMPLETENESS_COMPLETE
    assert pinned.payload_state_counts == {"available": 1, "missing": 0,
                                           "corrupt": 0, "metadata_only": 0,
                                           "retired": 0}


# ---------------------------------------------------------------------------
# future bindings honesty
# ---------------------------------------------------------------------------


async def test_future_bindings_unbound_and_machine_detectable(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed(service)
    snapshot = await resolve_snapshot(factory, "ws-a")

    assert KernelSnapshot.UNBOUND_FIELDS == UNBOUND_FUTURE_FIELDS
    assert UNBOUND_FUTURE_FIELDS == frozenset(
        {
            "content_revision_ids",
            "access_policy_set_id",
            "verifier_policy_revision_id",
            "schema_registry_revision",
        }
    )
    # no API surface exists through which a fabricated binding could enter
    params = inspect.signature(resolve_snapshot).parameters
    for name in UNBOUND_FUTURE_FIELDS:
        assert name not in params
    # identity round-trips over exactly the declared deterministic fields
    # (unbound names hashed as unbound), so future bound versions cannot
    # collide with v1 identities
    identity_payload = {
        "workspace_id": snapshot.workspace_id,
        "kernel_commit_id": snapshot.kernel_commit_id,
        "required_payload_state": snapshot.required_payload_state,
        "completeness": snapshot.completeness,
        "commit_count": snapshot.commit_count,
        "record_count": snapshot.record_count,
        "edge_count": snapshot.edge_count,
        "record_class_counts": snapshot.record_class_counts,
        "kernel_schema_versions": list(snapshot.kernel_schema_versions),
        "canonicalization_profiles": list(snapshot.canonicalization_profiles),
        "payload_state_counts": snapshot.payload_state_counts,
        "unbound_future_fields": sorted(UNBOUND_FUTURE_FIELDS),
    }
    from app.kernel.snapshots import compute_snapshot_identity

    assert compute_snapshot_identity(identity_payload) == snapshot.snapshot_id
    rebuilt = await resolve_snapshot(factory, "ws-a")
    assert rebuilt.snapshot_id == snapshot.snapshot_id
