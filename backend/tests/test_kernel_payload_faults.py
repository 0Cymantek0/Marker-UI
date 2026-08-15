"""Crash/fault matrix across the storage/database boundary (plan 7.3).

Every injected interruption is classified against the one-sided crash
ordering invariant: either NO committed kernel mutation exists (with at
most unreachable immutable payload residue), or exactly one COMPLETE
committed mutation whose payload exists, verifies, and whose outbox
intent is present. No intermediate state may advertise payload-backed
completeness.

Matrix phases (equivalents of the plan's twelve boundaries):

storage side (before the database transaction):
  1. stage-before-write      2. stage-mid-write (partial tmp)
  3. stage-after-write       4. stage-after-fsync
  5. stage-after-publish     6. stage-after-verify

database side (inside the transaction):
  7. begin                   8. head-read
  9. records-inserted       10. payloads-registered
 11. edges-inserted         12. manifest-inserted
 13. outbox-inserted        14. head-advanced
 15. pre-commit
plus the positive control: no fault (post-commit crash window).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import FAULT_PHASES, KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError
from app.kernel.models import (
    KernelCommitManifest,
    KernelOutbox,
    KernelPayloadObject,
    KernelRecord,
)
from app.kernel.outbox import OutboxIntent, list_outbox
from app.kernel.payloads import PAYLOAD_FAULT_PHASES, LocalPayloadStore
from app.kernel.reconcile import verify_payload_availability
from app.kernel.records import ObservationRecord
from app.kernel.replay import read_head, verify_history

pytestmark = pytest.mark.asyncio

PAYLOAD = b"matrix payload bytes \x00\xff\x80 binary"

STORAGE_FAULTS = sorted(PAYLOAD_FAULT_PHASES)
DB_FAULTS = sorted(FAULT_PHASES)
# Storage faults after successful publication leave complete bytes.
POST_PUBLISH_STORAGE_FAULTS = {"stage-after-publish", "stage-after-verify"}
# Storage faults before publication leave nothing (tmp is discarded by
# the raised error path; a real crash's tmp residue is reconciled
# separately — see test_kernel_reconcile.py).


def _batch() -> KernelCommitBatch:
    return KernelCommitBatch(
        workspace_id="ws",
        records=(
            ObservationRecord(
                observer="matrix",
                derivation={"case": "fault-matrix"},
                payload_bytes=PAYLOAD,
            ),
        ),
        outbox=(OutboxIntent(work_kind="materialize", payload={"why": "matrix"}),),
    )


async def _count(factory: async_sessionmaker, model) -> int:
    async with factory() as session:
        rows = (await session.execute(select(model))).scalars().all()
    return len(rows)


async def _assert_no_committed_mutation(factory, store) -> list[str]:
    """The only acceptable outcome for every injected interruption."""
    assert await read_head(factory, "ws") == 0
    assert await _count(factory, KernelRecord) == 0
    assert await _count(factory, KernelCommitManifest) == 0
    assert await _count(factory, KernelPayloadObject) == 0
    assert await _count(factory, KernelOutbox) == 0
    assert await list_outbox(factory) == []
    history = await verify_history(factory, "ws")
    assert history.ok, history.problems

    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.record_states == ()  # nothing falsely complete
    physical = await store.list_objects()
    # Residue, if any, must be classified unreachable — never available.
    assert set(availability.orphan_objects) == set(physical)
    for blob in availability.blob_states:
        assert blob.state != "available"
    return physical


@pytest.mark.parametrize("phase", STORAGE_FAULTS)
async def test_storage_side_interruptions_never_create_visible_truth(
    payload_env, phase: str
) -> None:
    factory, store, service = payload_env
    store._faults = frozenset({phase})
    with pytest.raises(InjectedFaultError):
        await service.commit(_batch())

    physical = await _assert_no_committed_mutation(factory, store)
    if phase in POST_PUBLISH_STORAGE_FAULTS:
        # Complete immutable bytes may exist; they are unreachable orphans.
        assert len(physical) == 1
    else:
        assert physical == []


@pytest.mark.parametrize("phase", DB_FAULTS)
async def test_db_side_interruptions_never_create_visible_truth(
    payload_env, phase: str
) -> None:
    factory, store, service = payload_env
    with pytest.raises(InjectedFaultError):
        await service.commit(_batch(), _inject_fault_at=phase)

    physical = await _assert_no_committed_mutation(factory, store)
    # The database-side faults all fire after staging succeeded, so the
    # one-sided invariant's allowed residue is present: exactly one
    # complete, verified, unreachable object.
    assert len(physical) == 1
    check = await store.check_object(physical[0], expected_length=len(PAYLOAD))
    assert check.available


async def test_post_commit_window_is_complete_truth(payload_env) -> None:
    """Positive control: success path (crash after DB commit, pre-ack).

    The receipt the caller never saw is irrelevant — durable state alone
    must show exactly one complete committed mutation.
    """
    factory, store, service = payload_env
    receipt = await service.commit(_batch())

    assert await read_head(factory, "ws") == receipt.kernel_commit_id == 1
    assert await _count(factory, KernelRecord) == 1
    assert await _count(factory, KernelPayloadObject) == 1
    pending = await list_outbox(factory)
    assert [r.id for r in pending] == list(receipt.outbox_ids)
    assert pending[0].state == "pending"

    history = await verify_history(factory, "ws")
    assert history.ok, history.problems
    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.payload_backed_complete is True
    assert availability.orphan_objects == ()


async def test_rolled_back_registry_leaves_blob_unreferenced_not_lost(
    payload_env,
) -> None:
    """A failed commit's payload becomes reusable, not garbage.

    After the failed commit, a retry with the same bytes must succeed
    and reference the SAME content identity — the durable object was
    already published, so retry reuses it (dedup) instead of rewriting.
    """
    factory, store, service = payload_env
    with pytest.raises(InjectedFaultError):
        await service.commit(_batch(), _inject_fault_at="payloads-registered")

    receipt = await service.commit(_batch())
    assert receipt.kernel_commit_id == 1  # first attempt never existed
    assert store.dedup_hits == 1  # object reused, not rewritten
    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.payload_backed_complete is True
    assert availability.orphan_objects == ()
