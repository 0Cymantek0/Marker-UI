"""GC fault-injection tests (V3.2 PR65B, plan matrix 14-16, 20).

Every destructive boundary is crashed deterministically and restart
must converge to a truthful state: no object is lost before the
tombstone decision, durable intent resumes or rescues after it, an
unlink without its outcome recording converges idempotently, and a
kill during derived-generation cleanup leaves the current pointer and
surviving generations coherent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError
from app.kernel.gc import (
    GC_FAULT_PHASES,
    collect,
    reconcile_retirements,
)
from app.kernel.generations import (
    GenerationService,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ObservationRecord
from app.kernel.retention import ROOT_KIND_SNAPSHOT_HOLD, declare_hold
from app.kernel.snapshots import (
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
    resolve_snapshot,
)

pytestmark = pytest.mark.asyncio


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


async def _commit(service: KernelCommitService, key: str, data: bytes) -> str:
    record = ObservationRecord(
        observer="marker", derivation={"probe": key}, payload_bytes=data
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    from app.utils.canonical import payload_byte_hash

    return payload_byte_hash(data)


async def _two_unreachable_payloads(payload_env: tuple) -> tuple:
    """Two payload commits under a metadata-only current generation: both
    blob keys are unreachable and there is one superseded generation."""
    factory, store, service = payload_env
    k1 = await _commit(service, "f1", b"fault-probe-one")
    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    await GenerationService(factory).build_and_activate(snapshot)
    k2 = await _commit(service, "f2", b"fault-probe-two")
    meta = await resolve_snapshot(
        factory, "ws-a", required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY
    )
    await GenerationService(factory).build_and_activate(meta)
    return factory, store, k1, k2


def _retire_rows(db_path: Path) -> dict[str, str]:
    with sqlite3.connect(db_path) as conn:
        return dict(
            conn.execute(
                "SELECT blob_key, state FROM kernel_payload_retirements"
            ).fetchall()
        )


async def test_fault_phase_set_is_exact(payload_env: tuple) -> None:
    assert GC_FAULT_PHASES == frozenset(
        {
            "gc-after-mark",
            "gc-after-recheck",
            "gc-after-generations",
            "gc-before-unlink",
            "gc-after-unlink",
            "gc-after-sweep",
        }
    )
    with pytest.raises(Exception):
        await collect(payload_env[0], payload_env[1], _inject_fault_at="not-a-phase")


async def test_crash_after_mark_loses_nothing(payload_env: tuple) -> None:
    """Matrix 14: the plan is in-memory evidence only; a crash right
    after mark leaves no tombstone and no deleted byte."""
    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)
    objects_before = set(await store.list_objects())

    with pytest.raises(InjectedFaultError):
        await collect(factory, store, _inject_fault_at="gc-after-mark")

    assert set(await store.list_objects()) == objects_before
    assert _retire_rows(_db_path(factory)) == {}
    # a plain restart pass then behaves normally
    report = await collect(factory, store)
    assert report.swept_deleted == 2
    assert set(await store.list_objects()) == set()


async def test_crash_after_recheck_leaves_resumable_intent(payload_env: tuple) -> None:
    """Matrix 15: tombstones are durable; files still present. Restart
    reconciliation resumes the sweep and converges."""
    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)

    with pytest.raises(InjectedFaultError):
        await collect(factory, store, _inject_fault_at="gc-after-recheck")

    rows = _retire_rows(_db_path(factory))
    assert rows == {k1: "pending", k2: "pending"}
    assert set(await store.list_objects()) == {k1, k2}  # nothing unlinked yet

    restarted = await reconcile_retirements(factory, store)
    assert restarted.swept_deleted == 2
    assert _retire_rows(_db_path(factory)) == {k1: "deleted", k2: "deleted"}
    assert set(await store.list_objects()) == set()
    assert restarted.bytes_reclaimed > 0


async def test_hold_declared_after_decision_sees_honest_degradation(
    payload_env: tuple,
) -> None:
    """The tombstone transaction is the linearization boundary: a hold
    committed after it does not undo the decision, but the system stays
    truthful — the held cut degrades, never lies, and re-staging the
    exact bytes heals it (plan section 14: rescue applies to candidates
    before authorization; post-decision roots heal through staging)."""
    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)
    with pytest.raises(InjectedFaultError):
        await collect(factory, store, _inject_fault_at="gc-after-recheck")
    assert _retire_rows(_db_path(factory)) == {k1: "pending", k2: "pending"}

    # a root declared strictly after the authorization transaction
    await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )
    restarted = await reconcile_retirements(factory, store)
    assert restarted.swept_deleted + restarted.already_absent == 2
    assert set(await store.list_objects()) == set()

    # the held cut reports degraded with the retirement named — not
    # "inspectable complete", and not a silent "missing" either
    held = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert held.completeness == "degraded"
    assert held.payload_state_counts["retired"] == 1

    # the heal path: re-supply the exact bytes through staging
    await store.stage(b"fault-probe-one")
    healed = await resolve_snapshot(
        factory,
        "ws-a",
        at_commit=1,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert healed.completeness == "complete"
    assert healed.payload_state_counts["available"] == 1


async def test_crash_after_unlink_converges_without_fabrication(
    payload_env: tuple,
) -> None:
    """Matrix 16: the file is gone but the outcome row rolled back with
    the fault; restart treats physical absence idempotently."""
    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)

    with pytest.raises(InjectedFaultError):
        await collect(factory, store, _inject_fault_at="gc-after-unlink")

    rows = _retire_rows(_db_path(factory))
    assert rows in ({k1: "pending", k2: "pending"}, {k1: "pending"}, {k2: "pending"})
    remaining = set(await store.list_objects())
    assert len(remaining) < 2  # at least one object was unlinked

    restarted = await reconcile_retirements(factory, store)
    assert restarted.swept_deleted + restarted.already_absent == 2
    assert _retire_rows(_db_path(factory)) == {k1: "deleted", k2: "deleted"}
    assert set(await store.list_objects()) == set()
    # availability never fabricates the missing bytes
    from app.kernel.reconcile import verify_payload_availability

    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    assert not availability.payload_backed_complete


async def test_crash_before_unlink_keeps_object_and_intent(payload_env: tuple) -> None:
    """The other side of the unlink window: fault before unlink rolls
    everything back to pending + present."""
    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)

    with pytest.raises(InjectedFaultError):
        await collect(factory, store, _inject_fault_at="gc-before-unlink")

    assert _retire_rows(_db_path(factory)) == {k1: "pending", k2: "pending"}
    assert set(await store.list_objects()) == {k1, k2}
    await reconcile_retirements(factory, store)
    assert set(await store.list_objects()) == set()


async def test_store_level_delete_faults_bracket_the_unlink(
    payload_env: tuple,
) -> None:
    """LocalPayloadStore's own delete-after-unlink phase raises inside
    the sweep transaction; the row rolls back to pending while the file
    is gone, and restart reconciliation converges idempotently."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)
    url = f"sqlite+aiosqlite:///{_db_path(factory).as_posix()}"

    faulty = LocalPayloadStore(
        store.root, fault_phases=frozenset({"delete-after-unlink"})
    )
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        with pytest.raises(InjectedFaultError):
            await collect(factory2, faulty)
    finally:
        await engine2.dispose()

    assert len(set(await store.list_objects())) < 2  # the unlink happened
    assert set(_retire_rows(_db_path(factory)).values()) == {"pending"}
    restarted = await reconcile_retirements(factory, store)
    assert restarted.swept_deleted + restarted.already_absent == 2
    assert _retire_rows(_db_path(factory)) == {k1: "deleted", k2: "deleted"}


async def test_kill_during_generation_cleanup_leaves_pointer_coherent(
    payload_env: tuple,
) -> None:
    """Matrix 20: a fault after the generation-retirement transaction
    (or a kill inside it — transaction atomicity covers that) leaves the
    current generation resolvable and every surviving generation
    verifiable."""
    factory, store, k1, k2 = await _two_unreachable_payloads(payload_env)
    gen_service = GenerationService(factory)
    superseded = await gen_service.list_generations(state="superseded")
    assert len(superseded) == 1

    with pytest.raises(InjectedFaultError):
        await collect(factory, store, _inject_fault_at="gc-after-generations")

    current = await resolve_current_generation(factory, "ws-a")
    assert current is not None and current.state == "active"
    assert (await verify_generation(factory, current.generation_id)).ok
    for ref in await gen_service.list_generations():
        assert (await verify_generation(factory, ref.generation_id)).ok
    # the superseded generation was retired atomically before the fault
    assert await gen_service.list_generations(state="superseded") == []
    assert current.generation_id != superseded[0].generation_id
