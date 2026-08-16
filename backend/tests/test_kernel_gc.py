"""Retention GC tests (V3.2 PR65B): roots, dedup, races, honesty.

Adversarial matrix coverage: current-generation protection, pinned and
unpinned superseded generations, holds (fresh, late, expired), orphan
reuse races, shared/deduplicated bytes across records and workspaces,
the workspace-scoped orphan-report quirk regression, delete failures,
already-missing objects, corruption distinctness, completeness honesty
after retirement, stale-staging residue, and the commit-side tombstone
rescue. Crash-window variants live in test_kernel_gc_faults.py.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError, PayloadStageError
from app.kernel.gc import (
    collect,
    plan_collection,
    reconcile_retirements,
)
from app.kernel.generations import (
    GenerationReader,
    GenerationService,
    open_pinned_generation,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ClaimAssertionRecord, ObservationRecord
from app.kernel.reconcile import (
    PAYLOAD_STATE_AVAILABLE,
    PAYLOAD_STATE_CORRUPT,
    PAYLOAD_STATE_RETIRED,
    verify_payload_availability,
)
from app.kernel.retention import (
    ROOT_KIND_SNAPSHOT_HOLD,
    declare_hold,
)
from app.kernel.snapshots import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_DEGRADED,
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
    resolve_snapshot,
)

pytestmark = pytest.mark.asyncio


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


async def _commit_payload(
    service: KernelCommitService, workspace_id: str, key: str, data: bytes
) -> str:
    """One payload-bearing observation commit; returns the blob key."""
    record = ObservationRecord(
        observer="marker",
        derivation={"probe": key},
        payload_bytes=data,
    )
    await service.commit(
        KernelCommitBatch(workspace_id=workspace_id, records=(record,))
    )
    from app.utils.canonical import payload_byte_hash

    return payload_byte_hash(data)


async def _activate(
    factory: async_sessionmaker,
    workspace_id: str,
    *,
    required: str = PAYLOAD_REQUIREMENT_INSPECTABLE,
    store: LocalPayloadStore | None = None,
):
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(
        factory,
        workspace_id,
        required_payload_state=required,
        payload_store=store,
    )
    return await gen_service.build_and_activate(snapshot)


async def _retire_state_rows(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT blob_key, state FROM kernel_payload_retirements"
        ).fetchall()


# ---------------------------------------------------------------------------
# plan (dry run) purity and current-generation protection (matrix 1)
# ---------------------------------------------------------------------------


async def test_plan_is_read_only_and_reports_candidates(payload_env: tuple) -> None:
    factory, store, service = payload_env
    key = await _commit_payload(service, "ws-a", "p1", b"plan-probe-1")
    await _activate(factory, "ws-a", store=store)
    await store.stage(b"orphan-probe-bytes")  # never committed

    plan = await plan_collection(factory, store)
    assert plan.roots >= 1
    assert key in plan.live_blob_keys
    assert plan.candidate_registry_keys == ()
    assert len(plan.candidate_orphan_keys) == 1
    assert plan.eligible_generations == ()  # current generation retained
    assert plan.summary()["candidate_orphan_objects"] == 1

    objects_before = set(await store.list_objects())
    report = await collect(factory, store, dry_run=True)
    assert report.dry_run and report.tombstoned == 0
    assert set(await store.list_objects()) == objects_before
    assert await _retire_state_rows(_db_path(factory)) == []


async def test_current_generation_and_its_bytes_never_collected(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    k1 = await _commit_payload(service, "ws-a", "p1", b"live-one")
    k2 = await _commit_payload(service, "ws-a", "p2", b"live-two")
    await _activate(factory, "ws-a", store=store)

    report = await collect(factory, store)
    assert report.generations_retired == 0
    assert report.eligible_registry_objects == 0
    assert report.bytes_reclaimed == 0
    assert set(await store.list_objects()) == {k1, k2}
    current = await resolve_current_generation(factory, "ws-a")
    assert current is not None
    assert (await verify_generation(factory, current.generation_id)).ok


# ---------------------------------------------------------------------------
# superseded generations (matrix 2, 3, 8)
# ---------------------------------------------------------------------------


async def test_unpinned_superseded_generation_retired_and_rebuildable(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_payload(service, "ws-a", "p1", b"supersede-one")
    first = await _activate(factory, "ws-a", store=store)
    await _commit_payload(service, "ws-a", "p2", b"supersede-two")
    second = await _activate(factory, "ws-a", store=store)

    report = await collect(factory, store)
    assert report.generations_retired == 1
    assert report.generations_rescued == ()

    gen_service = GenerationService(factory)
    assert await gen_service.get_generation(second.generation_id)  # current intact
    with pytest.raises(Exception):
        await gen_service.get_generation(first.generation_id)  # rows gone
    with sqlite3.connect(_db_path(factory)) as conn:
        residue = conn.execute(
            "SELECT COUNT(*) FROM kernel_generation_records WHERE generation_id = ?",
            (first.generation_id,),
        ).fetchone()[0]
    assert residue == 0

    # rebuild of the retired generation reproduces the declared content
    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    rebuilt = await gen_service.build(snapshot)
    assert rebuilt.generation_id == first.generation_id
    assert rebuilt.content_digest == first.content_digest


async def test_pinned_superseded_generation_survives_collection(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_payload(service, "ws-a", "p1", b"pin-one")
    first = await _activate(factory, "ws-a", store=store)
    await _commit_payload(service, "ws-a", "p2", b"pin-two")
    await _activate(factory, "ws-a", store=store)

    reader = await open_pinned_generation(factory, first.generation_id)
    assert reader.pinned
    report = await collect(factory, store)
    assert report.generations_retired == 0  # pin held through the pass

    records = await reader.list_records()
    assert len(records) == 1
    await reader.close()
    assert not reader.pinned

    # released pin: the next pass may retire it
    report = await collect(factory, store)
    assert report.generations_retired == 1


async def test_reader_acquiring_pin_after_mark_is_rescued_at_recheck(
    payload_env: tuple,
) -> None:
    """Matrix 8: a reader that acquires protection before the deletion
    linearization boundary (the recheck transaction) is protected."""
    factory, store, service = payload_env
    await _commit_payload(service, "ws-a", "p1", b"near-sweep")
    first = await _activate(factory, "ws-a", store=store)
    await _commit_payload(service, "ws-a", "p2", b"near-sweep-two")
    await _activate(factory, "ws-a", store=store)

    plan = await plan_collection(factory, store)
    assert [c.generation_id for c in plan.eligible_generations] == [first.generation_id]

    reader = await open_pinned_generation(factory, first.generation_id)
    from app.kernel.gc import execute_collection

    report = await execute_collection(factory, store, plan)
    assert first.generation_id in report.generations_rescued
    assert report.generations_retired == 0
    assert await reader.count_records() == 1
    await reader.close()


# ---------------------------------------------------------------------------
# late roots and late commits rescue candidates (matrix 6, 7, 13)
# ---------------------------------------------------------------------------


async def _unreachable_payload_setup(payload_env: tuple) -> tuple:
    """One inspectable generation at cut 1, then a metadata-only current
    generation at cut 2: cut-1 bytes stay, cut-2 bytes are unreachable."""
    factory, store, service = payload_env
    k1 = await _commit_payload(service, "ws-a", "held", b"held-bytes")
    await _activate(factory, "ws-a", store=store)
    k2 = await _commit_payload(service, "ws-a", "loose", b"loose-bytes")
    await _activate(factory, "ws-a", required=PAYLOAD_REQUIREMENT_METADATA_ONLY)
    return factory, store, service, k1, k2


async def test_hold_declared_after_mark_rescues_bytes(payload_env: tuple) -> None:
    factory, store, service, k1, k2 = await _unreachable_payload_setup(payload_env)

    plan = await plan_collection(factory, store)
    assert set(plan.candidate_registry_keys) == {k1, k2}

    hold = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )
    assert hold.active

    from app.kernel.gc import execute_collection

    report = await execute_collection(factory, store, plan)
    assert k1 in report.rescued_keys
    assert report.rescued_count == 1
    assert report.eligible_registry_objects == 1
    assert set(await store.list_objects()) == {k1}  # k2 swept, k1 rescued


async def test_new_commit_and_generation_after_mark_rescues_bytes(
    payload_env: tuple,
) -> None:
    factory, store, service, k1, _ = await _unreachable_payload_setup(payload_env)
    plan = await plan_collection(factory, store)
    assert k1 in plan.candidate_registry_keys

    # a later committed root covering the same bytes rescues everything
    await _commit_payload(service, "ws-a", "p3", b"later-commit")
    await _activate(factory, "ws-a", store=store)

    from app.kernel.gc import execute_collection

    report = await execute_collection(factory, store, plan)
    assert k1 in report.rescued_keys
    assert report.eligible_registry_objects == 0
    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    assert availability.payload_backed_complete


async def test_pre_commit_orphan_reused_by_later_commit_is_rescued(
    payload_env: tuple,
) -> None:
    """Matrix 13: staged-but-never-committed bytes are candidates; a
    commit that legitimately adopts the exact staged bytes rescues them."""
    factory, store, service = payload_env
    await _commit_payload(service, "ws-a", "seed", b"seed-bytes")
    await _activate(factory, "ws-a", store=store)

    staged = await store.stage(b"orphan-reuse-probe")  # no commit yet
    plan = await plan_collection(factory, store)
    assert plan.candidate_orphan_keys == (staged.blob_key,)

    await _commit_payload(service, "ws-a", "adopted", b"orphan-reuse-probe")
    await _activate(factory, "ws-a", store=store)

    from app.kernel.gc import execute_collection

    report = await execute_collection(factory, store, plan)
    assert staged.blob_key in report.rescued_keys
    assert staged.blob_key in set(await store.list_objects())


async def test_expired_hold_loses_protection_via_normal_path(payload_env: tuple) -> None:
    """Matrix 5: expiry only stops protection; the bytes then flow
    through the ordinary mark/recheck path."""
    from datetime import datetime, timedelta, timezone

    factory, store, service, k1, k2 = await _unreachable_payload_setup(payload_env)
    hold = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    report = await collect(factory, store)
    # protected at mark time (never a candidate), so nothing needed rescuing
    assert report.eligible_registry_objects == 1
    assert k1 in set(await store.list_objects())

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(tzinfo=None)
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_retention_roots SET expires_at = ? WHERE root_id = ?",
            (expired.isoformat(sep=" "), hold.root_id),
        )
        conn.commit()

    report = await collect(factory, store)
    assert set(await store.list_objects()) == set()
    assert dict(await _retire_state_rows(_db_path(factory))).get(k2) == "deleted"


# ---------------------------------------------------------------------------
# dedup and workspace isolation (matrix 10, 11, 12)
# ---------------------------------------------------------------------------


async def test_shared_blob_two_workspaces_protected_by_either(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    shared = b"shared-across-workspaces"
    ka = await _commit_payload(service, "ws-a", "a", shared)
    kb = await _commit_payload(service, "ws-b", "b", shared)
    assert ka == kb  # one physical object backs both evidence records

    await _activate(factory, "ws-a", store=store)  # ws-a inspectable
    await _activate(
        factory, "ws-b", required=PAYLOAD_REQUIREMENT_METADATA_ONLY
    )

    report = await collect(factory, store)
    assert report.eligible_registry_objects == 0  # ws-a root keeps the bytes
    assert set(await store.list_objects()) == {ka}

    # dropping ws-a to metadata-only releases the last byte requirement
    await _activate(factory, "ws-a", required=PAYLOAD_REQUIREMENT_METADATA_ONLY)
    report = await collect(factory, store)
    assert report.eligible_registry_objects == 1
    assert set(await store.list_objects()) == set()
    # both workspaces' records now honestly report retired bytes
    for ws in ("ws-a", "ws-b"):
        availability = await verify_payload_availability(factory, store, workspace_id=ws)
        assert all(r.state == PAYLOAD_STATE_RETIRED for r in availability.record_states)


async def test_same_bytes_two_records_one_object_one_tombstone(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    data = b"deduped-evidence"
    r1 = ObservationRecord(observer="m", derivation={"i": 1}, payload_bytes=data)
    r2 = ObservationRecord(observer="m", derivation={"i": 2}, payload_bytes=data)
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(r1, r2))
    )
    await _activate(factory, "ws-a", required=PAYLOAD_REQUIREMENT_METADATA_ONLY)

    report = await collect(factory, store)
    assert report.eligible_registry_objects == 1  # retiring one path never
    assert len(await _retire_state_rows(_db_path(factory))) == 1  # doubles
    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    assert len(availability.record_states) == 2
    assert all(r.state == PAYLOAD_STATE_RETIRED for r in availability.record_states)


async def test_workspace_scoped_orphan_quirk_not_inherited_by_gc(
    payload_env: tuple,
) -> None:
    """Matrix 12: the known reconciliation quirk (another workspace's
    object reported as a workspace-scoped orphan) is reproduced here and
    proven NOT to leak into GC candidates."""
    factory, store, service = payload_env
    key = await _commit_payload(service, "ws-a", "a", b"ws-a-only-object")
    await _activate(factory, "ws-a", store=store)
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-b",
            records=(
                ClaimAssertionRecord(
                    claim_key="kb", subject="doc:y.pdf", predicate="p", value=2
                ),
            ),
        )
    )

    quirk = await verify_payload_availability(factory, store, workspace_id="ws-b")
    assert key in quirk.orphan_objects  # the documented reporting quirk

    report = await collect(factory, store, workspace_id="ws-b")
    assert report.eligible_registry_objects == 0
    assert report.orphan_objects == 0
    assert set(await store.list_objects()) == {key}  # ws-a's bytes untouched


# ---------------------------------------------------------------------------
# filesystem outcomes without process death (matrix 17, 18, 19)
# ---------------------------------------------------------------------------


async def _lone_candidate_env(payload_env: tuple) -> tuple:
    factory, store, service = payload_env
    key = await _commit_payload(service, "ws-a", "lone", b"lone-candidate")
    await _activate(factory, "ws-a", required=PAYLOAD_REQUIREMENT_METADATA_ONLY)
    return factory, store, service, key


async def test_unlink_failure_is_truthful_and_retryable(payload_env: tuple) -> None:
    factory, store, _service, key = await _lone_candidate_env(payload_env)
    original = store.delete_object

    async def failing_delete(blob_key: str):
        if blob_key == key:
            raise PayloadStageError("payload deletion failed: permission denied")
        return await original(blob_key)

    store.delete_object = failing_delete  # type: ignore[method-assign]
    report = await collect(factory, store)
    store.delete_object = original  # type: ignore[method-assign]

    assert report.failed_keys == (key,)
    assert report.swept_deleted == 0
    assert report.bytes_reclaimed == 0  # no false success
    assert key in set(await store.list_objects())  # bytes still there
    rows = dict(await _retire_state_rows(_db_path(factory)))
    assert rows[key] == "failed"
    with sqlite3.connect(_db_path(factory)) as conn:
        error = conn.execute(
            "SELECT last_error FROM kernel_payload_retirements WHERE blob_key = ?",
            (key,),
        ).fetchone()[0]
    assert "permission denied" in error

    # retry after the filesystem condition clears
    reconciled = await reconcile_retirements(factory, store)
    assert reconciled.swept_deleted == 1
    assert set(await store.list_objects()) == set()
    assert dict(await _retire_state_rows(_db_path(factory)))[key] == "deleted"


async def test_already_missing_object_converges_idempotently(payload_env: tuple) -> None:
    factory, store, _service, key = await _lone_candidate_env(payload_env)
    path = store.object_path(key)
    os.chmod(path, stat.S_IWRITE)  # clear the read-only tamper hint
    path.unlink()  # vanished outside GC's control

    report = await collect(factory, store)
    assert report.already_absent == 1
    assert report.swept_deleted == 0
    assert dict(await _retire_state_rows(_db_path(factory)))[key] == "deleted"

    again = await collect(factory, store)
    assert again.swept_deleted == 0 and again.already_absent == 0


async def test_corruption_and_retirement_stay_distinct_facts(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    live_key = await _commit_payload(service, "ws-a", "live", b"live-corrupt-probe")
    dead_key = await _commit_payload(service, "ws-a", "dead", b"dead-corrupt-probe")
    await _activate(factory, "ws-a", required=PAYLOAD_REQUIREMENT_METADATA_ONLY)
    # a hold protects only the live object's bytes
    await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )

    # tamper with both objects' bytes
    for key, tag in ((live_key, b"tampered-live"), (dead_key, b"tampered-dead")):
        path = store.object_path(key)
        os.chmod(path, stat.S_IWRITE)
        path.write_bytes(tag)

    report = await collect(factory, store)
    # the corrupt-but-unreachable object is retired (policy disposal of
    # unreachable bytes); the corrupt-but-required object survives
    assert report.eligible_registry_objects == 1
    assert set(await store.list_objects()) == {live_key}

    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    states = {r.kernel_commit_id: r.state for r in availability.record_states}
    assert states[1] == PAYLOAD_STATE_CORRUPT  # tamper evidence preserved
    assert states[2] == PAYLOAD_STATE_RETIRED  # retirement recorded as retirement
    assert not availability.payload_backed_complete


# ---------------------------------------------------------------------------
# completeness honesty (matrix 25, 26)
# ---------------------------------------------------------------------------


async def test_post_gc_completeness_is_honest_per_requirement(payload_env: tuple) -> None:
    factory, store, service, k1, k2 = await _unreachable_payload_setup(payload_env)
    # protect the cut-1 closure only
    await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )
    report = await collect(factory, store)
    assert report.eligible_registry_objects == 1
    assert report.bytes_reclaimed > 0
    assert set(await store.list_objects()) == {k1}

    # a live inspectable root stays complete (matrix 26)
    held = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert held.completeness == COMPLETENESS_COMPLETE
    assert held.payload_state_counts[PAYLOAD_STATE_AVAILABLE] == 1

    # a cut whose bytes were legitimately retired resolves degraded and
    # names the retirement — never "inspectable complete" (matrix 25)
    retired_view = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=2,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert retired_view.completeness == COMPLETENESS_DEGRADED
    assert retired_view.payload_state_counts[PAYLOAD_STATE_RETIRED] == 1

    # metadata-only views remain complete: identity/hash truth survives
    meta_view = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=2,
        required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY,
    )
    assert meta_view.completeness == COMPLETENESS_COMPLETE
    assert meta_view.record_count == 2

    # re-supplying the exact bytes heals the historical cut
    await store.stage(b"loose-bytes")
    healed = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=2,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert healed.completeness == COMPLETENESS_COMPLETE


# ---------------------------------------------------------------------------
# stale staging residue (movement 6)
# ---------------------------------------------------------------------------


async def test_stale_staging_residue_collectible_only_past_threshold(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_payload(service, "ws-a", "base", b"staging-base")
    await _activate(factory, "ws-a", store=store)
    await _commit_payload(service, "ws-a", "extra", b"staging-extra")

    gen_service = GenerationService(factory)
    with pytest.raises(InjectedFaultError):
        await gen_service.build_and_activate(
            await resolve_snapshot(factory, "ws-a"),
            _inject_fault_at="gen-staged",
        )
    staged = await gen_service.list_generations(state="staged")
    assert len(staged) == 1

    fresh = await collect(factory, store)  # default threshold retains it
    assert fresh.generations_retired == 0
    stale = await collect(factory, store, stale_staging_seconds=0.0)
    assert stale.generations_retired == 1
    assert await gen_service.list_generations(state="staged") == []
    current = await resolve_current_generation(factory, "ws-a")
    assert current is not None and current.state == "active"


# ---------------------------------------------------------------------------
# commit-side tombstone rescue (the in-flight commit race)
# ---------------------------------------------------------------------------


async def test_commit_referencing_retired_bytes_rescues_and_republishes(
    payload_env: tuple,
) -> None:
    factory, store, service, key = await _lone_candidate_env(payload_env)
    await collect(factory, store)
    assert set(await store.list_objects()) == set()  # bytes retired

    # a new commit re-supplies and re-references the exact bytes
    new_key = await _commit_payload(service, "ws-a", "again", b"lone-candidate")
    assert new_key == key
    assert key in set(await store.list_objects())  # staged again
    with sqlite3.connect(_db_path(factory)) as conn:
        tombstones = conn.execute(
            "SELECT COUNT(*) FROM kernel_payload_retirements"
        ).fetchone()[0]
    assert tombstones == 0  # the commit's rescue removed the tombstone

    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    assert availability.record_states[0].state == PAYLOAD_STATE_AVAILABLE


async def test_commit_retries_when_staged_object_vanishes_mid_commit(
    payload_env: tuple,
) -> None:
    """A GC sweep between staging and the commit transaction forces the
    vanish-abort path: the commit re-stages and converges."""
    factory, store, _service, key = await _lone_candidate_env(payload_env)
    plan = await plan_collection(factory, store)

    original_exists = store.object_exists
    calls = {"n": 0}

    async def flaky_exists(blob_key: str) -> bool:
        if blob_key == key:
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # the sweep appears to have won
        return await original_exists(blob_key)

    store.object_exists = flaky_exists  # type: ignore[method-assign]
    from app.kernel.gc import execute_collection

    await execute_collection(factory, store, plan)
    receipt_service = KernelCommitService(factory, payload_store=store)
    record = ObservationRecord(
        observer="marker",
        derivation={"probe": "vanishing"},
        payload_bytes=b"lone-candidate",
    )
    receipt = await receipt_service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    store.object_exists = original_exists  # type: ignore[method-assign]

    assert receipt.workspace_id == "ws-a"
    assert calls["n"] >= 2  # first attempt aborted, retry re-checked
    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    assert availability.payload_backed_complete


# ---------------------------------------------------------------------------
# reader/pin ergonomics on the generation surface
# ---------------------------------------------------------------------------


async def test_open_current_generation_with_pin_lease(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit_payload(service, "ws-a", "p", b"current-pin")
    await _activate(factory, "ws-a", store=store)

    async with await open_pinned_generation(
        factory, (await resolve_current_generation(factory, "ws-a")).generation_id
    ) as reader:
        assert reader.pinned
        await reader.renew(lease_seconds=120)
        assert await reader.count_records() == 1
    assert not reader.pinned

    from app.kernel.retention import active_reader_pins

    assert await active_reader_pins(factory) == ()

    plain = GenerationReader(factory, (await resolve_current_generation(factory, "ws-a")).generation_id)
    with pytest.raises(Exception):
        await plain.renew()  # unpinned readers have nothing to renew
    await plain.close()  # no-op
