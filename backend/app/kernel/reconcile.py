"""Payload availability verification and conservative repair (PR64).

Answers, reproducibly and without treating the filesystem as a second
truth ledger:

* what payload bytes each committed record requires;
* whether those bytes are locally available and integrity-verified;
* which registry objects are missing or corrupt;
* which physical objects are unreachable orphans (published before a
  database commit that never became visible);
* which staging scratch files are stale residue.

Classification per committed record that carries a ``payload_byte_hash``:

* ``available``     — registry row + object present + hash/length verify;
* ``missing``       — registry row, object file absent;
* ``corrupt``       — registry row, object present but bytes fail verify;
* ``metadata_only`` — no registry row: the hash is declared truth, but
  the bytes were never durably staged in the local profile. Honest, and
  explicitly NOT payload-backed-complete;
* ``retired`` (PR65B) — registry row + GC tombstone + object absent:
  the bytes were deliberately retired under an authorized retention
  decision. Distinct from ``missing`` (unexpected absence) and from
  ``available`` — a re-supplied object whose bytes verify again is
  ``available`` regardless of tombstone history.

Repair is conservative: it may delete stale tmp scratch and return
stuck outbox items to pending. It never deletes objects, never writes
payload bytes, never rewrites evidence identity, and never turns a
metadata-only or degraded state into "complete" — healing requires the
exact bytes to be re-supplied and re-verified through staging.

Known reporting quirk (kept deliberately, documented for GC authors):
``orphan_objects`` with a ``workspace_id`` filter compares the whole
physical store against only that workspace's needed registry keys, so
another workspace's object can appear as an "orphan". It is a report,
never a deletion candidate list — the PR65B collector derives
candidates from store-wide reachability instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import validate_workspace_id
from app.kernel.models import (
    KernelOutbox,
    KernelPayloadObject,
    KernelPayloadRetirement,
    KernelRecord,
)
from app.kernel.outbox import (
    OUTBOX_STATE_PENDING,
    list_outbox,
    reset_in_flight,
)
from app.kernel.payloads import LocalPayloadStore, ObjectCheck

__all__ = [
    "BlobState",
    "PAYLOAD_STATE_AVAILABLE",
    "PAYLOAD_STATE_CORRUPT",
    "PAYLOAD_STATE_METADATA_ONLY",
    "PAYLOAD_STATE_MISSING",
    "PAYLOAD_STATE_RETIRED",
    "PayloadAvailabilityResult",
    "RecordPayloadState",
    "ReconcileReport",
    "reconcile",
    "reconcile_after_restart",
    "verify_payload_availability",
]

PAYLOAD_STATE_AVAILABLE = "available"
PAYLOAD_STATE_MISSING = "missing"
PAYLOAD_STATE_CORRUPT = "corrupt"
PAYLOAD_STATE_METADATA_ONLY = "metadata_only"
PAYLOAD_STATE_RETIRED = "retired"


@dataclass(frozen=True)
class BlobState:
    """Verification outcome for one registry object."""

    blob_key: str
    locator: str
    payload_length: int
    exists: bool
    length_ok: bool
    hash_ok: bool
    #: a GC tombstone authorizes/records this object's retirement
    retired: bool = False

    @property
    def state(self) -> str:
        if self.exists:
            # Re-supplied bytes that verify win over retirement history:
            # availability is about present, verified bytes.
            if not (self.length_ok and self.hash_ok):
                return PAYLOAD_STATE_CORRUPT
            return PAYLOAD_STATE_AVAILABLE
        if self.retired:
            return PAYLOAD_STATE_RETIRED
        return PAYLOAD_STATE_MISSING


@dataclass(frozen=True)
class RecordPayloadState:
    record_id: str
    workspace_id: str
    kernel_commit_id: int
    blob_key: str
    state: str


@dataclass(frozen=True)
class PayloadAvailabilityResult:
    workspace_id: str | None
    record_states: tuple[RecordPayloadState, ...]
    blob_states: tuple[BlobState, ...]
    #: objects physically present that no registry row authorizes
    orphan_objects: tuple[str, ...]
    #: staging scratch files present at scan time
    tmp_residue: tuple[str, ...]

    @property
    def degraded(self) -> tuple[RecordPayloadState, ...]:
        return tuple(r for r in self.record_states if r.state != PAYLOAD_STATE_AVAILABLE)

    @property
    def payload_backed_complete(self) -> bool:
        """True only when every committed payload reference is available.

        Metadata-only history is deliberately incomplete under this
        definition: the bytes backing inspection/replay are not locally
        materialized, and no metadata may claim otherwise.
        """
        return all(r.state == PAYLOAD_STATE_AVAILABLE for r in self.record_states)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.record_states:
            counts[record.state] = counts.get(record.state, 0) + 1
        return counts


@dataclass(frozen=True)
class ReconcileReport:
    availability: PayloadAvailabilityResult
    pending_outbox: int
    in_flight_reset: int = 0
    tmp_removed: tuple[str, ...] = field(default_factory=tuple)


async def verify_payload_availability(
    session_factory: async_sessionmaker,
    store: LocalPayloadStore,
    *,
    workspace_id: str | None = None,
    verify_hashes: bool = True,
) -> PayloadAvailabilityResult:
    """Classify payload truth for one workspace (or the whole database)."""
    if workspace_id is not None:
        validate_workspace_id(workspace_id)

    record_stmt = select(
        KernelRecord.id,
        KernelRecord.workspace_id,
        KernelRecord.kernel_commit_id,
        KernelRecord.payload_byte_hash,
        KernelRecord.payload_length,
    ).where(KernelRecord.payload_byte_hash.is_not(None))
    if workspace_id is not None:
        record_stmt = record_stmt.where(KernelRecord.workspace_id == workspace_id)
    record_stmt = record_stmt.order_by(
        KernelRecord.kernel_commit_id.asc(), KernelRecord.id.asc()
    )

    async with session_factory() as session:
        record_rows = (await session.execute(record_stmt)).all()
        needed_keys = sorted({row.payload_byte_hash for row in record_rows})
        registry_rows: Sequence = ()
        if needed_keys:
            registry_rows = (
                await session.execute(
                    select(KernelPayloadObject).where(
                        KernelPayloadObject.blob_key.in_(needed_keys)
                    )
                )
            ).scalars().all()
        tombstoned_keys: set[str] = set()
        if needed_keys:
            tombstoned_keys = {
                row[0]
                for row in (
                    await session.execute(
                        select(KernelPayloadRetirement.blob_key).where(
                            KernelPayloadRetirement.blob_key.in_(needed_keys)
                        )
                    )
                ).all()
            }

    blob_states: dict[str, BlobState] = {}
    for row in registry_rows:
        check = await _check_blob(store, row, verify_hashes=verify_hashes)
        blob_states[row.blob_key] = check
    for key in sorted(tombstoned_keys):
        if key in blob_states:
            existing = blob_states[key]
            blob_states[key] = BlobState(
                blob_key=existing.blob_key,
                locator=existing.locator,
                payload_length=existing.payload_length,
                exists=existing.exists,
                length_ok=existing.length_ok,
                hash_ok=existing.hash_ok,
                retired=True,
            )

    record_states = tuple(
        RecordPayloadState(
            record_id=row.id,
            workspace_id=row.workspace_id,
            kernel_commit_id=row.kernel_commit_id,
            blob_key=row.payload_byte_hash,
            state=blob_states[row.payload_byte_hash].state
            if row.payload_byte_hash in blob_states
            else PAYLOAD_STATE_METADATA_ONLY,
        )
        for row in record_rows
    )

    physical = await store.list_objects()
    registry_keys = {row.blob_key for row in registry_rows}
    orphans = tuple(key for key in physical if key not in registry_keys)

    tmp = await store.list_tmp()

    return PayloadAvailabilityResult(
        workspace_id=workspace_id,
        record_states=record_states,
        blob_states=tuple(blob_states.values()),
        orphan_objects=orphans,
        tmp_residue=tuple(str(p.name) for p in tmp),
    )


async def _check_blob(
    store: LocalPayloadStore, row: KernelPayloadObject, *, verify_hashes: bool
) -> BlobState:
    check: ObjectCheck = await store.check_object(
        row.blob_key, expected_length=row.payload_length
    )
    return BlobState(
        blob_key=row.blob_key,
        locator=row.storage_locator,
        payload_length=row.payload_length,
        exists=check.exists,
        length_ok=check.length_ok,
        # The fast class trusts length only; full hashing is explicit.
        hash_ok=check.hash_ok if verify_hashes else check.length_ok,
    )


async def reconcile(
    session_factory: async_sessionmaker,
    store: LocalPayloadStore,
    *,
    workspace_id: str | None = None,
    tmp_older_than_seconds: float | None = None,
    reset_outbox: bool = False,
) -> ReconcileReport:
    """Scan payload/outbox truth and apply conservative repairs.

    ``tmp_older_than_seconds`` deletes staging scratch at least that old
    (live publishers hold scratch for milliseconds; an explicit age keeps
    concurrent staging safe). ``reset_outbox`` returns stuck in-flight
    outbox items to pending — at-least-once redelivery, never erasure:
    pending rows stay pending, done rows stay done.
    """
    availability = await verify_payload_availability(
        session_factory, store, workspace_id=workspace_id
    )
    pending = await list_outbox(session_factory, state=OUTBOX_STATE_PENDING)
    in_flight_reset = await reset_in_flight(session_factory) if reset_outbox else 0
    tmp_removed: tuple[str, ...] = ()
    if tmp_older_than_seconds is not None:
        removed = await store.cleanup_tmp(older_than_seconds=tmp_older_than_seconds)
        tmp_removed = tuple(str(p.name) for p in removed)
    return ReconcileReport(
        availability=availability,
        pending_outbox=len(pending),
        in_flight_reset=in_flight_reset,
        tmp_removed=tmp_removed,
    )


async def reconcile_after_restart(
    session_factory: async_sessionmaker,
    store: LocalPayloadStore,
    *,
    tmp_older_than_seconds: float = 3600.0,
) -> ReconcileReport:
    """Fresh-process recovery: classify durable state honestly.

    Reconstructs everything from disk and database — nothing depends on
    process memory. Stuck in-flight outbox work returns to pending
    (at-least-once); stale scratch is cleaned only past the age
    threshold; missing/corrupt/orphan classifications are reported, not
    fabricated away.
    """
    return await reconcile(
        session_factory,
        store,
        tmp_older_than_seconds=tmp_older_than_seconds,
        reset_outbox=True,
    )


async def count_outbox_states(session_factory: async_sessionmaker) -> dict[str, int]:
    """State histogram of the durable outbox (observability)."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(KernelOutbox.state, KernelOutbox.id).order_by(KernelOutbox.id)
            )
        ).all()
    counts: dict[str, int] = {}
    for state, _ in rows:
        counts[state] = counts.get(state, 0) + 1
    return counts
