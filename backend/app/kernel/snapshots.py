"""Kernel snapshot resolution (V3.2 PR65A).

A :class:`KernelSnapshot` is a **committed sequence boundary**, never a
timestamp. Membership of the historical cut is defined exclusively by
``kernel_commit_id <= K`` for one workspace/shard chain; wall-clock
time, filesystem mtimes, and worker clocks never participate.

PR65A scope honesty: the master plan's full ``KernelSnapshot`` shape
also names content-revision, access-policy, verifier-policy, and
schema-registry identities. Those subsystems (PR70+) do not exist on
this branch, so this contract is explicitly **kernel-only v1**: the
future bindings are declared ``UNBOUND_FUTURE_FIELDS``, appear verbatim
as such in the snapshot identity preimage, and there is no API surface
through which a fabricated value for them could be supplied. Absence is
machine-detectable, never invented.

Completeness is classified honestly against the requested payload
requirement:

* ``metadata_only``   — complete when the committed metadata at the cut
  is coherent (manifest chain present, member counts agree);
* ``inspectable`` / ``replayable`` — complete only when every
  payload-bearing record in the cut verifies as ``available`` in the
  local content-addressed store. Missing, corrupt, and metadata-only
  payload references keep the snapshot **degraded**; nothing upgrades
  degraded bytes to completeness.

The local topology does not yet distinguish inspectable from replayable
byte sets; both map to "all declared payload bytes verified available".
The requested requirement is recorded on the snapshot so a future split
of the two classes cannot silently reinterpret old snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import validate_workspace_id
from app.kernel.errors import (
    InvalidSnapshotCutError,
    SnapshotIntegrityError,
    SnapshotRequirementError,
)
from app.kernel.models import (
    KERNEL_SCHEMA_VERSION,
    KernelCommitManifest,
    KernelPayloadObject,
    KernelPayloadRetirement,
    KernelRecord,
    KernelRecordEdge,
)
from app.kernel.payloads import LocalPayloadStore
from app.kernel.reconcile import (
    PAYLOAD_STATE_AVAILABLE,
    PAYLOAD_STATE_CORRUPT,
    PAYLOAD_STATE_METADATA_ONLY,
    PAYLOAD_STATE_MISSING,
    PAYLOAD_STATE_RETIRED,
)
from app.utils.canonical import CANONICALIZATION_PROFILE, record_identity_hash

__all__ = [
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_DEGRADED",
    "DEGRADED_SAMPLE_BOUND",
    "KernelSnapshot",
    "PAYLOAD_REQUIREMENT_INSPECTABLE",
    "PAYLOAD_REQUIREMENT_METADATA_ONLY",
    "PAYLOAD_REQUIREMENT_REPLAYABLE",
    "PAYLOAD_REQUIREMENTS",
    "SNAPSHOT_RECORD_TYPE",
    "SNAPSHOT_SCHEMA_VERSION",
    "UNBOUND_FUTURE_FIELDS",
    "compute_snapshot_identity",
    "resolve_snapshot",
]

#: Framing domain separating snapshot identity from other kernel hashes.
SNAPSHOT_RECORD_TYPE = "marker.kernel.snapshot.v1"
SNAPSHOT_SCHEMA_VERSION = "1.0.0"

PAYLOAD_REQUIREMENT_METADATA_ONLY = "metadata_only"
PAYLOAD_REQUIREMENT_INSPECTABLE = "inspectable"
PAYLOAD_REQUIREMENT_REPLAYABLE = "replayable"
PAYLOAD_REQUIREMENTS = frozenset(
    {
        PAYLOAD_REQUIREMENT_METADATA_ONLY,
        PAYLOAD_REQUIREMENT_INSPECTABLE,
        PAYLOAD_REQUIREMENT_REPLAYABLE,
    }
)

COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_DEGRADED = "degraded"

#: Master-plan snapshot fields whose owning subsystems (PR70+) are not
#: implemented on this branch. Declared unbound here and hashed as
#: unbound into the snapshot identity — never fabricated.
UNBOUND_FUTURE_FIELDS = frozenset(
    {
        "content_revision_ids",
        "access_policy_set_id",
        "verifier_policy_revision_id",
        "schema_registry_revision",
    }
)

#: Bounded diagnostic sample of degraded record ids (full truth is the
#: per-state counts plus reconciliation surfaces).
DEGRADED_SAMPLE_BOUND = 20


@dataclass(frozen=True)
class KernelSnapshot:
    """One resolved committed cut with its honest completeness verdict."""

    workspace_id: str
    kernel_commit_id: int
    snapshot_id: str
    required_payload_state: str
    completeness: str
    commit_count: int
    record_count: int
    edge_count: int
    record_class_counts: Mapping[str, int] = field(default_factory=dict)
    kernel_schema_versions: tuple[str, ...] = ()
    canonicalization_profiles: tuple[str, ...] = ()
    #: per-record payload availability histogram for the cut (empty when
    #: availability was not evaluated for a metadata_only requirement)
    payload_state_counts: Mapping[str, int] = field(default_factory=dict)
    #: None when availability was not evaluated; True only when every
    #: payload-bearing record in the cut verifies as available
    payload_backed_complete: bool | None = None
    #: bounded sample of record ids blocking payload completeness
    degraded_record_ids: tuple[str, ...] = ()

    #: future master-plan bindings not yet implemented (see module docs)
    UNBOUND_FIELDS: ClassVar[frozenset[str]] = UNBOUND_FUTURE_FIELDS

    @property
    def degraded(self) -> bool:
        return self.completeness == COMPLETENESS_DEGRADED


def compute_snapshot_identity(payload: Mapping) -> str:
    """Deterministic identity of a resolved snapshot under its framing."""
    return record_identity_hash(
        record_type=SNAPSHOT_RECORD_TYPE,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        payload=payload,
    )


async def resolve_snapshot(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    at_commit: int | None = None,
    required_payload_state: str = PAYLOAD_REQUIREMENT_METADATA_ONLY,
    payload_store: LocalPayloadStore | None = None,
    verify_payload_hashes: bool = True,
) -> KernelSnapshot:
    """Pin one committed cut and classify its completeness honestly.

    ``at_commit=None`` pins the current committed head (``0`` for a
    workspace with no commits). Later commits never change what a pinned
    snapshot contains: every membership query is bounded by
    ``kernel_commit_id <= K``.
    """
    validate_workspace_id(workspace_id)
    if required_payload_state not in PAYLOAD_REQUIREMENTS:
        raise SnapshotRequirementError(
            f"unknown payload requirement {required_payload_state!r}; "
            f"allowed: {sorted(PAYLOAD_REQUIREMENTS)}"
        )
    if payload_store is None and required_payload_state != PAYLOAD_REQUIREMENT_METADATA_ONLY:
        raise SnapshotRequirementError(
            f"requirement {required_payload_state!r} needs payload verification, "
            "but no payload store was supplied; refusing to claim completeness "
            "without evidence"
        )

    from app.kernel.replay import read_head

    head = await read_head(session_factory, workspace_id)
    cut = head if at_commit is None else at_commit
    if not isinstance(cut, int) or isinstance(cut, bool) or cut < 0:
        raise InvalidSnapshotCutError(
            f"workspace={workspace_id!r}: invalid cut {cut!r}; kernel cuts are "
            "non-negative commit ids"
        )
    if cut > head:
        raise InvalidSnapshotCutError(
            f"workspace={workspace_id!r}: cut {cut} is in the future; committed "
            f"head is {head}"
        )

    async with session_factory() as session:
        manifest_rows = (
            (
                await session.execute(
                    select(
                        KernelCommitManifest.kernel_commit_id,
                        KernelCommitManifest.record_count,
                        KernelCommitManifest.edge_count,
                        KernelCommitManifest.record_class_counts_json,
                        KernelCommitManifest.kernel_schema_version,
                        KernelCommitManifest.canonicalization_profile,
                    )
                    .where(
                        KernelCommitManifest.workspace_id == workspace_id,
                        KernelCommitManifest.kernel_commit_id <= cut,
                    )
                    .order_by(KernelCommitManifest.kernel_commit_id.asc())
                )
            )
            .all()
        )
        if cut > 0 and not manifest_rows:
            raise SnapshotIntegrityError(
                f"workspace={workspace_id!r}: cut {cut} is below the committed "
                f"head {head} but has no manifest; the chain is not contiguous"
            )
        manifest_commit_ids = {row.kernel_commit_id for row in manifest_rows}
        if cut > 0 and cut not in manifest_commit_ids:
            raise SnapshotIntegrityError(
                f"workspace={workspace_id!r}: no manifest at cut {cut}"
            )

        record_count = await session.scalar(
            select(func.count())
            .select_from(KernelRecord)
            .where(
                KernelRecord.workspace_id == workspace_id,
                KernelRecord.kernel_commit_id <= cut,
            )
        )
        edge_count = await session.scalar(
            select(func.count())
            .select_from(KernelRecordEdge)
            .where(
                KernelRecordEdge.workspace_id == workspace_id,
                KernelRecordEdge.kernel_commit_id <= cut,
            )
        )
        class_rows = (
            await session.execute(
                select(KernelRecord.record_class, func.count())
                .where(
                    KernelRecord.workspace_id == workspace_id,
                    KernelRecord.kernel_commit_id <= cut,
                )
                .group_by(KernelRecord.record_class)
            )
        ).all()

    record_class_counts = {row[0]: row[1] for row in class_rows}

    # Metadata coherence: manifest-declared members must equal the rows
    # actually visible in the cut (append-only history makes this cheap).
    declared_records = sum(row.record_count for row in manifest_rows)
    declared_edges = sum(row.edge_count for row in manifest_rows)
    if declared_records != record_count or declared_edges != edge_count:
        raise SnapshotIntegrityError(
            f"workspace={workspace_id!r}: metadata incoherent at cut {cut}: "
            f"manifests declare {declared_records} records/{declared_edges} edges, "
            f"found {record_count}/{edge_count}"
        )

    schema_versions = tuple(sorted({row.kernel_schema_version for row in manifest_rows}))
    profiles = tuple(sorted({row.canonicalization_profile for row in manifest_rows}))
    if not manifest_rows:
        # Empty cut: the interpreting code declares its own versions.
        schema_versions = (KERNEL_SCHEMA_VERSION,)
        profiles = (CANONICALIZATION_PROFILE,)

    # --- payload availability, bounded to the cut ------------------------
    payload_state_counts: dict[str, int] = {}
    payload_backed_complete: bool | None = None
    degraded_ids: tuple[str, ...] = ()
    if payload_store is not None:
        payload_backed_complete, payload_state_counts, degraded_ids = (
            await _classify_cut_payloads(
                session_factory,
                payload_store,
                workspace_id=workspace_id,
                cut=cut,
                verify_hashes=verify_payload_hashes,
            )
        )

    if required_payload_state == PAYLOAD_REQUIREMENT_METADATA_ONLY:
        completeness = COMPLETENESS_COMPLETE
    else:
        completeness = (
            COMPLETENESS_COMPLETE
            if payload_backed_complete
            else COMPLETENESS_DEGRADED
        )

    identity_payload = {
        "workspace_id": workspace_id,
        "kernel_commit_id": cut,
        "required_payload_state": required_payload_state,
        "completeness": completeness,
        "commit_count": len(manifest_rows),
        "record_count": record_count,
        "edge_count": edge_count,
        "record_class_counts": record_class_counts,
        "kernel_schema_versions": list(schema_versions),
        "canonicalization_profiles": list(profiles),
        "payload_state_counts": payload_state_counts,
        "unbound_future_fields": sorted(UNBOUND_FUTURE_FIELDS),
    }
    snapshot_id = compute_snapshot_identity(identity_payload)

    return KernelSnapshot(
        workspace_id=workspace_id,
        kernel_commit_id=cut,
        snapshot_id=snapshot_id,
        required_payload_state=required_payload_state,
        completeness=completeness,
        commit_count=len(manifest_rows),
        record_count=record_count,
        edge_count=edge_count,
        record_class_counts=record_class_counts,
        kernel_schema_versions=schema_versions,
        canonicalization_profiles=profiles,
        payload_state_counts=payload_state_counts,
        payload_backed_complete=payload_backed_complete,
        degraded_record_ids=degraded_ids,
    )


async def _classify_cut_payloads(
    session_factory: async_sessionmaker,
    store: LocalPayloadStore,
    *,
    workspace_id: str,
    cut: int,
    verify_hashes: bool,
) -> tuple[bool, dict[str, int], tuple[str, ...]]:
    """Availability histogram for payload-bearing records within the cut.

    Semantics mirror ``app.kernel.reconcile`` (available / missing /
    corrupt / metadata_only / retired) but the scan is bounded to
    records whose commit is ``<= cut`` — a pinned historical snapshot
    never consults payloads that entered the workspace later. A retired
    object (PR65B tombstone, bytes gone) keeps the snapshot honest:
    degraded for inspectable/replayable requirements, and never
    advertised as available. Re-supplied bytes that verify win over
    retirement history.
    """
    async with session_factory() as session:
        payload_rows = (
            await session.execute(
                select(
                    KernelRecord.id,
                    KernelRecord.payload_byte_hash,
                    KernelRecord.payload_length,
                )
                .where(
                    KernelRecord.workspace_id == workspace_id,
                    KernelRecord.kernel_commit_id <= cut,
                    KernelRecord.payload_byte_hash.is_not(None),
                )
                .order_by(KernelRecord.kernel_commit_id.asc(), KernelRecord.id.asc())
            )
        ).all()
        needed = sorted({row.payload_byte_hash for row in payload_rows})
        registry: dict[str, KernelPayloadObject] = {}
        tombstoned: set[str] = set()
        if needed:
            rows = (
                await session.execute(
                    select(KernelPayloadObject).where(
                        KernelPayloadObject.blob_key.in_(needed)
                    )
                )
            ).scalars().all()
            registry = {row.blob_key: row for row in rows}
            tombstoned = {
                row[0]
                for row in (
                    await session.execute(
                        select(KernelPayloadRetirement.blob_key).where(
                            KernelPayloadRetirement.blob_key.in_(needed)
                        )
                    )
                ).all()
            }

    blob_states: dict[str, str] = {}
    for blob_key, row in registry.items():
        check = await store.check_object(blob_key, expected_length=row.payload_length)
        if not check.exists:
            blob_states[blob_key] = (
                PAYLOAD_STATE_RETIRED
                if blob_key in tombstoned
                else PAYLOAD_STATE_MISSING
            )
        elif not (check.length_ok and (check.hash_ok or not verify_hashes)):
            blob_states[blob_key] = PAYLOAD_STATE_CORRUPT
        else:
            blob_states[blob_key] = PAYLOAD_STATE_AVAILABLE

    counts = {
        PAYLOAD_STATE_AVAILABLE: 0,
        PAYLOAD_STATE_MISSING: 0,
        PAYLOAD_STATE_CORRUPT: 0,
        PAYLOAD_STATE_METADATA_ONLY: 0,
        PAYLOAD_STATE_RETIRED: 0,
    }
    degraded: list[str] = []
    for row in payload_rows:
        state = blob_states.get(row.payload_byte_hash, PAYLOAD_STATE_METADATA_ONLY)
        counts[state] += 1
        if state != PAYLOAD_STATE_AVAILABLE:
            degraded.append(row.id)
    complete = not degraded
    return complete, counts, tuple(degraded[:DEGRADED_SAMPLE_BOUND])
