"""Adversarial runtime / data-plane fault matrix (PR82A).

A bounded, deterministic matrix executed against the REAL local seams
— outbox, fencing, publications, durable events, and the artifact
handle store — asserting the runtime truth invariants behind
preregistered Q7 (no false completion, no stale accepted publication,
duplicate execution without duplicate accepted truth) and Q8 (slow or
disconnected clients cannot affect job truth).

Every fault uses the established injection styles: in-transaction
phase hooks for crash windows, raw primitive sequencing for crash
gaps, and on-disk tampering for data-plane corruption. No chaos
infrastructure; PR69 admission/model-lease machinery does not exist in
this branch and is recorded as an absence finding, not tested.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    InjectedFaultError,
    PublicationConflictError,
    StaleFenceError,
)
from app.kernel.events import append, append_progress, get_latest_sequence, replay
from app.kernel.fencing import (
    PHASE_FENCE_VALIDATED,
    PHASE_POST_COMMIT,
    accept,
    acquire,
    release,
)
from app.kernel.models import KernelEvent, KernelOutbox, KernelProgress, KernelPublication
from app.kernel.outbox import OutboxIntent, claim, reset_in_flight
from app.kernel.records import ObservationRecord
from app.services.artifact_handles import ArtifactHandleError, ArtifactHandleStore


@dataclass
class FaultResult:
    fault_id: str
    invariant: str
    held: bool
    detail: str


@dataclass
class RuntimeResult:
    faults: tuple[FaultResult, ...]

    @property
    def violation_count(self) -> int:
        return sum(1 for fault in self.faults if not fault.held)

    def summary(self) -> dict[str, Any]:
        return {
            "faults": [
                {
                    "fault_id": fault.fault_id,
                    "invariant": fault.invariant,
                    "held": fault.held,
                    "detail": fault.detail,
                }
                for fault in self.faults
            ],
            "held": sum(1 for fault in self.faults if fault.held),
            "violations": self.violation_count,
        }


async def _new_work(factory, ws: str, kind: str = "materialize") -> int:
    """Commit one tiny record batch carrying one successor-work intent."""
    service = KernelCommitService(factory)
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=ws,
            records=(
                ObservationRecord(
                    record_id=f"obs-{ws}-{kind}",
                    observer="pr82-runtime",
                    derivation=dict(note="pr82 runtime fault matrix"),
                ),
            ),
            outbox=(OutboxIntent(work_kind=kind, payload={"ws": ws}),),
            producer={"op": "pr82-runtime-fault"},
        )
    )
    return receipt.outbox_ids[0]


async def _publication_count(factory, work_id: int) -> int:
    async with factory() as session:
        return await session.scalar(
            select(func.count()).select_from(KernelPublication).where(
                KernelPublication.work_id == work_id
            )
        )


async def _fault_crash_before_linearization(factory) -> FaultResult:
    work_id = await _new_work(factory, "ws-pr82-rt-crash-pre")
    await claim(factory, work_id)
    lease = await acquire(factory, work_id=work_id, owner_id="worker-a")
    assert lease is not None
    try:
        await accept(
            factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"status": "done", "v": 1},
            _inject_fault_at=PHASE_FENCE_VALIDATED,
        )
        return FaultResult(
            "crash_before_linearization", "no publication before the linearization point",
            False, "fault injection did not fire",
        )
    except InjectedFaultError:
        pass
    before = await _publication_count(factory, work_id)
    outcome = await accept(
        factory,
        work_id=work_id,
        fencing_token=lease.fencing_token,
        result={"status": "done", "v": 1},
    )
    after = await _publication_count(factory, work_id)
    held = before == 0 and after == 1 and not outcome.already_accepted
    return FaultResult(
        "crash_before_linearization",
        "no publication before the linearization point; retry creates exactly one",
        held,
        f"publications before={before} after={after}",
    )


async def _fault_crash_after_linearization(factory) -> FaultResult:
    work_id = await _new_work(factory, "ws-pr82-rt-crash-post")
    await claim(factory, work_id)
    lease = await acquire(factory, work_id=work_id, owner_id="worker-a")
    try:
        await accept(
            factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"status": "done", "v": 1},
            _inject_fault_at=PHASE_POST_COMMIT,
        )
        return FaultResult(
            "crash_after_linearization", "truth survives a post-commit crash",
            False, "fault injection did not fire",
        )
    except InjectedFaultError:
        pass
    after_crash = await _publication_count(factory, work_id)
    outcome = await accept(
        factory,
        work_id=work_id,
        fencing_token=lease.fencing_token,
        result={"status": "done", "v": 1},
    )
    held = after_crash == 1 and outcome.already_accepted and await _publication_count(
        factory, work_id
    ) == 1
    return FaultResult(
        "crash_after_linearization",
        "truth survives a post-commit crash and retries converge",
        held,
        f"publications after-crash={after_crash} converged={outcome.already_accepted}",
    )


async def _fault_stale_worker(factory) -> FaultResult:
    work_id = await _new_work(factory, "ws-pr82-rt-stale")
    await claim(factory, work_id)
    stale_lease = await acquire(factory, work_id=work_id, owner_id="worker-old")
    await release(factory, work_id=work_id, owner_id="worker-old", fencing_token=stale_lease.fencing_token)
    fresh_lease = await acquire(factory, work_id=work_id, owner_id="worker-new")
    await accept(
        factory,
        work_id=work_id,
        fencing_token=fresh_lease.fencing_token,
        result={"status": "done", "owner": "new"},
    )
    stale_rejected = False
    try:
        await accept(
            factory,
            work_id=work_id,
            fencing_token=stale_lease.fencing_token,
            result={"status": "done", "owner": "old"},
        )
    except StaleFenceError:
        stale_rejected = True
    count = await _publication_count(factory, work_id)
    held = stale_rejected and count == 1
    return FaultResult(
        "stale_worker_publication",
        "a superseded owner can never overwrite accepted truth",
        held,
        f"stale_rejected={stale_rejected} publications={count}",
    )


async def _fault_divergent_result(factory) -> FaultResult:
    work_id = await _new_work(factory, "ws-pr82-rt-divergent")
    await claim(factory, work_id)
    lease = await acquire(factory, work_id=work_id, owner_id="worker-a")
    await accept(
        factory, work_id=work_id, fencing_token=lease.fencing_token,
        result={"status": "done", "v": 1},
    )
    divergent_rejected = False
    try:
        await accept(
            factory, work_id=work_id, fencing_token=lease.fencing_token,
            result={"status": "done", "v": 2},
        )
    except PublicationConflictError:
        divergent_rejected = True
    held = divergent_rejected and await _publication_count(factory, work_id) == 1
    return FaultResult(
        "divergent_result",
        "a different result for accepted work is a conflict, never an update",
        held,
        f"rejected={divergent_rejected}",
    )


async def _fault_duplicate_execution(factory) -> FaultResult:
    work_id = await _new_work(factory, "ws-pr82-rt-duplicate")
    await claim(factory, work_id)
    lease = await acquire(factory, work_id=work_id, owner_id="worker-a")
    first = await accept(
        factory, work_id=work_id, fencing_token=lease.fencing_token,
        result={"status": "done", "v": 1},
    )
    second = await accept(
        factory, work_id=work_id, fencing_token=lease.fencing_token,
        result={"status": "done", "v": 1},
    )
    count = await _publication_count(factory, work_id)
    held = (
        not first.already_accepted
        and second.already_accepted
        and count == 1
        and second.publication.publication_id == first.publication.publication_id
    )
    return FaultResult(
        "duplicate_execution_single_truth",
        "duplicate work may happen; duplicate accepted truth cannot",
        held,
        f"publications={count}",
    )


async def _fault_cancelled_owner(factory) -> FaultResult:
    work_id = await _new_work(factory, "ws-pr82-rt-cancel")
    await claim(factory, work_id)
    lease = await acquire(factory, work_id=work_id, owner_id="worker-a")
    await release(factory, work_id=work_id, owner_id="worker-a", fencing_token=lease.fencing_token)
    rejected = False
    try:
        await accept(
            factory, work_id=work_id, fencing_token=lease.fencing_token,
            result={"status": "done"},
        )
    except StaleFenceError:
        rejected = True
    held = rejected and await _publication_count(factory, work_id) == 0
    return FaultResult(
        "cancelled_owner_cannot_complete",
        "a released/cancelled owner cannot fabricate completion",
        held,
        f"rejected={rejected} publications={await _publication_count(factory, work_id)}",
    )


async def _fault_slow_consumer(factory) -> FaultResult:
    ws = "ws-pr82-rt-slow"
    for index in range(12):
        await append(
            factory,
            workspace_id=ws,
            stream="work",
            event_type="work.claimed",
            payload={"i": index},
        )
        await append_progress(
            factory, workspace_id=ws, work_id=(index % 3) + 1, counter=index
        )
    # The consumer reads slowly (one at a time with a yield between
    # batches) while more appends race ahead; appends must complete and
    # progress must coalesce.
    async def slow_consume() -> list[int]:
        sequences: list[int] = []
        after = 0
        for _ in range(4):
            batch = await replay(
            factory, workspace_id=ws, stream="work", after_sequence=after, limit=3
        )
            sequences.extend(event.semantic_sequence for event in batch)
            after = sequences[-1]
            await asyncio.sleep(0)
        return sequences

    consumer_task = asyncio.create_task(slow_consume())
    for index in range(12, 20):
        await append(
            factory, workspace_id=ws, stream="work", event_type="work.progress",
            payload={"i": index},
        )
    consumed = await consumer_task
    latest = await get_latest_sequence(factory, workspace_id=ws, stream="work")
    async with factory() as session:
        progress_rows = await session.scalar(
            select(func.count()).select_from(KernelProgress).where(
                KernelProgress.workspace_id == ws
            )
        )
        event_rows = await session.scalar(
            select(func.count()).select_from(KernelEvent).where(
                KernelEvent.workspace_id == ws
            )
        )
    held = (
        len(consumed) == len(set(consumed))
        and consumed == sorted(consumed)
        and latest == 20
        and event_rows == 20
        and progress_rows == 3
    )
    return FaultResult(
        "slow_consumer_never_blocks_truth",
        "slow consumers slow only themselves; progress coalesces; events never drop",
        held,
        f"events={event_rows} progress_rows={progress_rows} latest={latest}",
    )


async def _fault_restart_recovery(factory) -> FaultResult:
    ws = "ws-pr82-rt-restart"
    for index in range(5):
        await append(
            factory, workspace_id=ws, stream="work", event_type="work.done",
            payload={"i": index},
        )
    pre_restart = await get_latest_sequence(factory, workspace_id=ws, stream="work")
    work_id = await _new_work(factory, ws, kind="index")
    await claim(factory, work_id)  # crashes mid-flight

    # Simulate a process restart: a brand-new engine on the same file.
    engine = factory.kw["bind"]
    url = str(engine.url)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    new_engine = create_async_engine(url)
    new_factory = async_sessionmaker(new_engine, expire_on_commit=False)
    try:
        post_restart = await get_latest_sequence(new_factory, workspace_id=ws, stream="work")
        recovered = await reset_in_flight(new_factory)
        async with new_factory() as session:
            row = await session.get(KernelOutbox, work_id)
            state_after = row.state if row is not None else "missing"
        events_after = await replay(
            new_factory, workspace_id=ws, stream="work", after_sequence=0, limit=100
        )
        held = (
            post_restart == pre_restart
            and recovered >= 1
            and state_after == "pending"
            and len(events_after) == 5
        )
        detail = (
            f"sequence_pre={pre_restart} post={post_restart} reset={recovered} "
            f"state={state_after}"
        )
    finally:
        await new_engine.dispose()
    return FaultResult(
        "restart_recovery_explicit",
        "durable events and pending work survive restart; in-flight resets honestly",
        held,
        detail,
    )


def _fault_artifact_tamper(root) -> FaultResult:
    store = ArtifactHandleStore(root)
    from app.services.artifact_handles import resolve_worker_payload, stage_worker_payload

    # "text" is the declared hoistable field kind; a big text payload
    # above the inline limit must move into a verified file handle.
    payload_text = "pr82 artifact bytes " * 64
    envelope = stage_worker_payload(
        {"status": "ok", "text": payload_text},
        store=store,
        job_id="job-pr82",
        inline_limit=16,
    )
    handles = envelope.get("artifact_handles", {}) if isinstance(envelope, dict) else {}
    if not handles:
        return FaultResult(
            "artifact_tamper_fails_closed", "large payloads must be handle-backed",
            False, "payload stayed inline",
        )
    clean = resolve_worker_payload(envelope, store=store, job_id="job-pr82")
    # Tamper with the backing blob on disk.
    blob_files = [path for path in root.rglob("*") if path.is_file()]
    tampered = False
    for blob in blob_files:
        data = bytearray(blob.read_bytes())
        data[0] ^= 0xFF
        blob.write_bytes(bytes(data))
    try:
        resolve_worker_payload(envelope, store=store, job_id="job-pr82")
    except ArtifactHandleError:
        # Missing/corrupt/length-mismatch are all fail-closed refusals;
        # none may reconstruct corrupted bytes as valid output.
        tampered = True
    held = clean["text"] == payload_text and tampered
    return FaultResult(
        "artifact_tamper_fails_closed",
        "tampered/truncated bytes can never reconstruct valid output",
        held,
        f"clean_resolved={clean['text'] == payload_text} tamper_rejected={tampered}",
    )


async def evaluate_runtime(kernel_env, artifact_root) -> RuntimeResult:
    """Run the bounded fault matrix against real seams."""
    faults: list[FaultResult] = []
    faults.append(await _fault_crash_before_linearization(kernel_env))
    faults.append(await _fault_crash_after_linearization(kernel_env))
    faults.append(await _fault_stale_worker(kernel_env))
    faults.append(await _fault_divergent_result(kernel_env))
    faults.append(await _fault_duplicate_execution(kernel_env))
    faults.append(await _fault_cancelled_owner(kernel_env))
    faults.append(await _fault_slow_consumer(kernel_env))
    faults.append(await _fault_restart_recovery(kernel_env))
    faults.append(_fault_artifact_tamper(artifact_root))
    return RuntimeResult(faults=tuple(faults))
