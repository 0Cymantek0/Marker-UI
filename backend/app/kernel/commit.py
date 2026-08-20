"""Single transactional Truth Kernel commit authority (V3.2 PR63A/PR64).

This module is the ONE local code path that creates authoritative kernel
commits. The V3.2 commit protocol (SQLite and PostgreSQL profiles):

1. begin one transaction and immediately write the workspace
   ``KernelCommitHead`` row (insert-or-ignore) and read it back under a
   row lock — on SQLite the write-first upsert takes the database writer
   lock up front, and on PostgreSQL the ``SELECT ... FOR UPDATE``
   re-read holds the head row until commit, so concurrent committers
   serialize at the database rather than racing through a read snapshot;
2. read the committed head, derive ``kernel_commit_id = head + 1`` and
   ``parent_kernel_commit_id = head`` (causal order, never wall time);
3. validate external record references against visible committed state;
   then (PR65B) rescue any staged payload hash that carries a GC
   tombstone: present bytes un-tombstone the object, absent bytes abort
   and re-stage — a commit can never land referencing retired bytes;
   then (PR74) validate claim/proof integrity — proof topology,
   input provenance, and assessment semantics — against committed
   state overlaid with the batch, so an invalid proof rolls the whole
   commit back before anything becomes visible; then (PR75) apply the
   narrow high-risk source-native verification-risk gate;
4. insert all logical records and dependency edges for the batch;
5. register durably published payload objects (PR64): registry rows are
   inserted in the same transaction, so a visible reference always
   implies the immutable bytes were published and verified first;
6. insert the immutable commit manifest (counts + deterministic roots +
   manifest identity hash);
7. insert the batch's outbox intent rows (PR64): successor work becomes
   visible exactly when the authorizing commit does;
8. when the batch carries a view advancement (PR73): evaluate every
   patch precondition against current authoritative state and
   independently recompute the proposed revision under this
   transaction's writer lock, then flip the view head conditionally —
   the check, the records, and the head movement commit or roll back
   together (all-or-conflict, no TOCTOU window);
9. advance the head with a conditional update
   (``WHERE head_kernel_commit_id = <observed head>``) — a lost update
   cannot be silently accepted;
10. COMMIT while still holding the head row — the database commit is the
   linearization point that makes the new head, records, edges,
   manifest, payload references, outbox, and view-head movement visible
   atomically.

Contention policy: SQLite ``SQLITE_BUSY``/lock errors, PostgreSQL
serialization/deadlock SQLSTATEs, and concurrent head movement are
expected, retryable conditions with a bounded budget (see
``app.kernel.dialects``). Payload staging happens once per ``commit()``
call, before the retry loop: staging is content-addressed and
idempotent, and a database retry never re-publishes bytes.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.dialects import (
    dialect_insert,
    integrity_constraint_name,
    is_retryable_contention,
)

from app.kernel.errors import (
    BatchTooLargeError,
    CrossWorkspaceReferenceError,
    DuplicateRecordIdError,
    DuplicateRecordIdentityError,
    EmptyBatchError,
    HeadMovedError,
    InjectedFaultError,
    InvalidRecordPayloadError,
    InvalidWorkspaceIdError,
    KernelBusyError,
    KernelError,
    StaleBaseRevisionError,
    UnknownRecordReferenceError,
)
from app.kernel.manifest import (
    compute_manifest_identity_hash,
    compute_edge_root,
    compute_record_root,
    edge_root_entry,
    manifest_identity_payload,
    record_root_entry,
)
from app.kernel.models import (
    KERNEL_SCHEMA_VERSION,
    KernelCommitHead,
    KernelCommitManifest,
    KernelOutbox,
    KernelPayloadObject,
    KernelPayloadRetirement,
    KernelRecord,
    KernelRecordEdge,
    KernelViewHead,
)
from app.kernel.outbox import (
    OUTBOX_STATE_PENDING,
    OutboxIntent,
    compute_dedupe_key,
    intent_payload_json,
    validate_intent,
)
from app.kernel.patches import (
    PreparedViewRef,
    ViewAdvancement,
    ViewFlip,
    check_view_advancement,
)
from app.kernel.payloads import LOCAL_STORE_PROFILE, LocalPayloadStore
from app.kernel.proofs import ProofBatchRecord, check_batch_proof_integrity
from app.kernel.records import KernelEdge, KernelRecord as RecordInput
from app.kernel.verification_risk import check_batch_verification_risk
from app.utils.canonical import (
    CANONICALIZATION_PROFILE,
    CanonicalValueError,
    canonical_json_str,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "FAULT_PHASES",
    "KernelCommitBatch",
    "KernelCommitReceipt",
    "KernelCommitService",
    "PHASE_BEGIN",
    "PHASE_EDGES_INSERTED",
    "PHASE_HEAD_ADVANCED",
    "PHASE_HEAD_READ",
    "PHASE_MANIFEST_INSERTED",
    "PHASE_OUTBOX_INSERTED",
    "PHASE_PAYLOADS_REGISTERED",
    "PHASE_PRE_COMMIT",
    "PHASE_PROOF_CHECKED",
    "PHASE_RISK_CHECKED",
    "PHASE_RECORDS_INSERTED",
    "PHASE_VIEW_ADVANCED",
    "PHASE_VIEW_CHECKED",
    "default_commit_service",
]

# Deterministic fault-injection phases (test-only parameter).
PHASE_BEGIN = "begin"
PHASE_HEAD_READ = "head-read"
PHASE_VIEW_CHECKED = "view-checked"
PHASE_PROOF_CHECKED = "proof-checked"
PHASE_RISK_CHECKED = "risk-checked"
PHASE_RECORDS_INSERTED = "records-inserted"
PHASE_PAYLOADS_REGISTERED = "payloads-registered"
PHASE_EDGES_INSERTED = "edges-inserted"
PHASE_MANIFEST_INSERTED = "manifest-inserted"
PHASE_OUTBOX_INSERTED = "outbox-inserted"
PHASE_VIEW_ADVANCED = "view-advanced"
PHASE_HEAD_ADVANCED = "head-advanced"
PHASE_PRE_COMMIT = "pre-commit"

FAULT_PHASES = frozenset(
    {
        PHASE_BEGIN,
        PHASE_HEAD_READ,
        PHASE_VIEW_CHECKED,
        PHASE_PROOF_CHECKED,
        PHASE_RISK_CHECKED,
        PHASE_RECORDS_INSERTED,
        PHASE_PAYLOADS_REGISTERED,
        PHASE_EDGES_INSERTED,
        PHASE_MANIFEST_INSERTED,
        PHASE_OUTBOX_INSERTED,
        PHASE_VIEW_ADVANCED,
        PHASE_HEAD_ADVANCED,
        PHASE_PRE_COMMIT,
    }
)

WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
PAYLOAD_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

DEFAULT_MAX_BATCH_RECORDS = 1000
DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
MAX_RETRY_DELAY = 0.5


class _PayloadVanishedMidCommit(Exception):
    """Internal retry signal: a staged object was swept by GC after
    staging but before this transaction, so the bytes must be re-staged
    before the commit can be accepted (PR65B rescue protocol)."""

_duplicate_identity_marker = "uq_kernel_records_workspace_identity"
_manifest_pk_marker = "kernel_commit_manifests"


def validate_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.match(workspace_id):
        raise InvalidWorkspaceIdError(
            f"invalid workspace_id: {workspace_id!r} must match "
            f"{WORKSPACE_ID_PATTERN.pattern}"
        )
    return workspace_id


@dataclass(kw_only=True)
class KernelCommitBatch:
    """Bounded kernel mutation batch for one workspace/shard."""

    workspace_id: str
    records: tuple[RecordInput, ...] = ()
    edges: tuple[KernelEdge, ...] = ()
    #: commit-level producer/operation metadata (audit, canonical-safe)
    producer: Mapping[str, Any] = field(default_factory=dict)
    #: successor work that must become visible with this commit (PR64)
    outbox: tuple[OutboxIntent, ...] = ()
    #: conditional view-revision movement evaluated and flipped inside
    #: this commit's transaction (PR73); None for non-advancing batches
    view_advancement: ViewAdvancement | None = None


@dataclass(frozen=True)
class PreparedRecord:
    """Canonicalized record ready for insertion (pre-transaction)."""

    record_id: str
    record_class: str
    record_type: str
    schema_version: str
    identity_hash: str
    payload_json: str
    payload_byte_hash: str | None
    payload_length: int | None


@dataclass(frozen=True)
class KernelCommitReceipt:
    """Accepted commit identity returned to the caller."""

    workspace_id: str
    kernel_commit_id: int
    parent_kernel_commit_id: int
    manifest_identity_hash: str
    record_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    record_count: int
    edge_count: int
    #: payload blob keys whose durable objects back this commit's records
    payload_blob_keys: tuple[str, ...] = ()
    #: ids of outbox rows enqueued atomically with this commit
    outbox_ids: tuple[int, ...] = ()


class KernelCommitService:
    """The single ordered commit authority for local kernel mutations.

    One instance owns commits for one database; instantiate it once per
    process (see :func:`default_commit_service`). The service never
    mutates schema — callers on the production engine must pass a
    ``readiness_check`` (normally ``verify_database_ready``) so commits
    fail closed on an unmigrated database instead of self-healing.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        max_batch_records: int = DEFAULT_MAX_BATCH_RECORDS,
        busy_retry_attempts: int = DEFAULT_BUSY_RETRY_ATTEMPTS,
        busy_retry_base_delay: float = DEFAULT_BUSY_RETRY_BASE_DELAY,
        readiness_check: Callable[[], Awaitable[None]] | None = None,
        payload_store: LocalPayloadStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._max_batch_records = max_batch_records
        self._busy_retry_attempts = busy_retry_attempts
        self._busy_retry_base_delay = busy_retry_base_delay
        self._readiness_check = readiness_check
        self._ready = readiness_check is None
        self._payload_store = payload_store
        #: observed writer-contention retries (SQLite busy / PG serialization)
        self.busy_retries = 0
        #: observed concurrent-head-move retries
        self.head_retries = 0

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self._readiness_check()
        self._ready = True

    # ------------------------------------------------------------------
    # preparation (pre-transaction, side-effect free)
    # ------------------------------------------------------------------

    def _prepare(
        self, batch: KernelCommitBatch
    ) -> tuple[
        str,
        tuple[PreparedRecord, ...],
        tuple[KernelEdge, ...],
        str,
        tuple[tuple[OutboxIntent, str], ...],
    ]:
        workspace_id = validate_workspace_id(batch.workspace_id)
        records = tuple(batch.records)
        edges = tuple(batch.edges)
        if not records and not edges:
            raise EmptyBatchError(
                f"workspace={workspace_id!r}: batch contains neither records nor edges"
            )
        if len(records) > self._max_batch_records:
            raise BatchTooLargeError(
                f"workspace={workspace_id!r}: batch of {len(records)} records exceeds "
                f"bound {self._max_batch_records}"
            )

        seen_ids: set[str] = set()
        seen_identities: set[str] = set()
        prepared: list[PreparedRecord] = []
        for record in records:
            if record.record_id in seen_ids:
                raise DuplicateRecordIdError(
                    f"workspace={workspace_id!r}: duplicate record_id {record.record_id!r} in batch"
                )
            seen_ids.add(record.record_id)
            try:
                payload = to_json_ready(record.identity_payload())
            except CanonicalValueError as exc:
                raise InvalidRecordPayloadError(
                    f"workspace={workspace_id!r} record={record.record_id!r} "
                    f"class={record.record_class!r}: {exc}"
                ) from exc
            identity_hash = record_identity_hash(
                record_type=record.record_type,
                schema_version=record.schema_version,
                payload=payload,
            )
            if identity_hash in seen_identities:
                raise DuplicateRecordIdentityError(
                    f"workspace={workspace_id!r}: batch contains two semantically "
                    f"identical {record.record_class!r} records (identity {identity_hash})"
                )
            seen_identities.add(identity_hash)

            raw_payload: bytes | None = getattr(record, "payload_bytes", None)
            declared_hash: str | None = getattr(record, "declared_payload_hash", None)
            if raw_payload is not None and not isinstance(raw_payload, (bytes, bytearray)):
                raise InvalidRecordPayloadError(
                    f"workspace={workspace_id!r} record={record.record_id!r}: "
                    "payload_bytes must be bytes"
                )
            if declared_hash is not None and not PAYLOAD_HASH_PATTERN.match(declared_hash):
                raise InvalidRecordPayloadError(
                    f"workspace={workspace_id!r} record={record.record_id!r}: "
                    f"declared_payload_hash must match {PAYLOAD_HASH_PATTERN.pattern}"
                )
            byte_hash = payload_byte_hash(bytes(raw_payload)) if raw_payload is not None else declared_hash
            prepared.append(
                PreparedRecord(
                    record_id=record.record_id,
                    record_class=record.record_class,
                    record_type=record.record_type,
                    schema_version=record.schema_version,
                    identity_hash=identity_hash,
                    payload_json=canonical_json_str(payload),
                    payload_byte_hash=byte_hash,
                    payload_length=len(raw_payload) if raw_payload is not None else None,
                )
            )

        try:
            producer_json = canonical_json_str(to_json_ready(dict(batch.producer)))
        except CanonicalValueError as exc:
            raise InvalidRecordPayloadError(
                f"workspace={workspace_id!r}: producer metadata rejected: {exc}"
            ) from exc

        seen_edges: set[tuple[str, str, str]] = set()
        for edge in edges:
            key = (edge.edge_kind, edge.source_ref, edge.target_ref)
            if key in seen_edges:
                raise KernelError(
                    f"workspace={workspace_id!r}: duplicate edge {key} in batch"
                )
            seen_edges.add(key)

        prepared_outbox = tuple(
            (intent, intent_payload_json(validate_intent(intent)))
            for intent in batch.outbox
        )

        return workspace_id, tuple(prepared), edges, producer_json, prepared_outbox

    # ------------------------------------------------------------------
    # durable payload staging (pre-transaction; PR64)
    # ------------------------------------------------------------------

    async def _stage_payloads(
        self, records: Sequence[RecordInput]
    ) -> dict[str, tuple[int, str]]:
        """Publish payload bytes durably before any database reference.

        Returns ``{blob_key: (length, locator)}`` for every object this
        commit may reference as available: freshly staged bytes, reused
        verified objects for declared hashes, and deduplicated bytes
        shared by several records. Runs once per ``commit()`` call — a
        database retry never re-stages.
        """
        store = self._payload_store
        if store is None:
            return {}
        staged: dict[str, tuple[int, str]] = {}
        declared_keys: list[str] = []
        for record in records:
            raw = getattr(record, "payload_bytes", None)
            if raw is not None:
                blob = await store.stage(bytes(raw))
                staged[blob.blob_key] = (blob.payload_length, blob.locator)
            else:
                declared = getattr(record, "declared_payload_hash", None)
                if declared is not None:
                    declared_keys.append(declared)
        for key in declared_keys:
            if key in staged:
                continue
            check = await store.check_object(key)
            if check.available:
                staged[key] = (check.length, check.locator)
        return staged

    # ------------------------------------------------------------------
    # commit path
    # ------------------------------------------------------------------

    async def commit(
        self, batch: KernelCommitBatch, *, _inject_fault_at: str | None = None
    ) -> KernelCommitReceipt:
        """Atomically accept a bounded batch; returns the commit identity.

        ``_inject_fault_at`` is a deterministic test hook: when it names a
        commit-protocol phase, that phase raises ``InjectedFaultError``
        inside the transaction so fault tests can prove rollback behavior.
        """
        if _inject_fault_at is not None and _inject_fault_at not in FAULT_PHASES:
            raise KernelError(f"unknown fault phase {_inject_fault_at!r}")
        await self._ensure_ready()
        workspace_id, prepared, edges, producer_json, prepared_outbox = self._prepare(batch)
        staged_payloads = await self._stage_payloads(batch.records)

        last_error: Exception | None = None
        for attempt in range(self._busy_retry_attempts):
            try:
                return await self._commit_once(
                    workspace_id,
                    prepared,
                    edges,
                    producer_json,
                    prepared_outbox,
                    staged_payloads,
                    batch.view_advancement,
                    _inject_fault_at,
                )
            except _PayloadVanishedMidCommit:
                # PR65B rescue: GC swept a staged object between staging
                # and this transaction. Re-publish the exact bytes and
                # retry — the next attempt's tombstone check either sees
                # the republished object (rescue + proceed) or repeats
                # safely under the bounded attempt budget.
                last_error = None
                staged_payloads = await self._stage_payloads(batch.records)
            except HeadMovedError:
                self.head_retries += 1
                last_error = None
            except OperationalError as exc:
                if not is_retryable_contention(exc):
                    raise
                self.busy_retries += 1
                last_error = exc
            except IntegrityError as exc:
                raise _map_integrity_error(workspace_id, exc) from exc
            await asyncio.sleep(_retry_delay(self._busy_retry_base_delay, attempt))
        if last_error is not None:
            raise KernelBusyError(
                f"workspace={workspace_id!r}: database writer contention persisted after "
                f"{self._busy_retry_attempts} attempts: {last_error}"
            ) from last_error
        raise KernelBusyError(
            f"workspace={workspace_id!r}: head contention persisted after "
            f"{self._busy_retry_attempts} attempts"
        )

    async def _commit_once(
        self,
        workspace_id: str,
        prepared: Sequence[PreparedRecord],
        edges: Sequence[KernelEdge],
        producer_json: str,
        prepared_outbox: Sequence[tuple[OutboxIntent, str]],
        staged_payloads: Mapping[str, tuple[int, str]],
        view_advancement: ViewAdvancement | None,
        inject_fault_at: str | None,
    ) -> KernelCommitReceipt:
        def maybe_inject(phase: str) -> None:
            if inject_fault_at == phase:
                raise InjectedFaultError(phase)

        async with self._session_factory() as session:
            async with session.begin():
                maybe_inject(PHASE_BEGIN)

                # 1. Write-first head upsert + locked re-read. On SQLite
                #    the write-first upsert acquires the database writer
                #    lock; on PostgreSQL the FOR UPDATE re-read holds the
                #    head row until this transaction ends. Either way,
                #    committers for one workspace serialize here before
                #    any read, and the check-then-act phases below have
                #    no TOCTOU window.
                await session.execute(
                    dialect_insert(session.bind, KernelCommitHead)
                    .values(workspace_id=workspace_id, head_kernel_commit_id=0)
                    .on_conflict_do_nothing(index_elements=[KernelCommitHead.workspace_id])
                )
                current_head = await session.scalar(
                    select(KernelCommitHead.head_kernel_commit_id)
                    .where(KernelCommitHead.workspace_id == workspace_id)
                    .with_for_update()
                )
                assert current_head is not None  # upsert guarantees the row exists
                next_commit_id = current_head + 1
                maybe_inject(PHASE_HEAD_READ)

                # 2. Resolve external references against visible committed state.
                batch_ids = {p.record_id for p in prepared}
                external_refs = {
                    ref
                    for edge in edges
                    for ref in (edge.source_ref, edge.target_ref)
                    if ref not in batch_ids
                }
                if external_refs:
                    rows = (
                        await session.execute(
                            select(
                                KernelRecord.id, KernelRecord.workspace_id
                            ).where(KernelRecord.id.in_(external_refs))
                        )
                    ).all()
                    found = {row.id: row.workspace_id for row in rows}
                    missing = sorted(external_refs - found.keys())
                    if missing:
                        raise UnknownRecordReferenceError(
                            f"workspace={workspace_id!r}: edges reference records not "
                            f"visible to this commit: {missing}"
                        )
                    foreign = sorted(
                        ref for ref, ws in found.items() if ws != workspace_id
                    )
                    if foreign:
                        raise CrossWorkspaceReferenceError(
                            f"workspace={workspace_id!r}: edges reference records of "
                            f"other workspaces: {foreign}"
                        )

                # 2.5. PR65B tombstone rescue. The head row lock is held
                #     (write-first upsert + locked read above), so this
                #     check and a concurrent GC sweep cannot interleave. Any staged
                #     hash carrying a retirement tombstone is either
                #     physically present — delete the tombstone; this
                #     commit is re-referencing the bytes — or absent,
                #     meaning GC swept between staging and this
                #     transaction: abort, re-stage, retry.
                if staged_payloads:
                    tombstoned = (
                        await session.execute(
                            select(KernelPayloadRetirement.blob_key).where(
                                KernelPayloadRetirement.blob_key.in_(
                                    sorted(staged_payloads)
                                )
                            )
                        )
                    ).all()
                    if tombstoned:
                        store = self._payload_store
                        assert store is not None  # staged_payloads implies a store
                        vanished = [
                            row[0]
                            for row in tombstoned
                            if not await store.object_exists(row[0])
                        ]
                        if vanished:
                            raise _PayloadVanishedMidCommit(
                                f"workspace={workspace_id!r}: staged objects were "
                                f"retired before the commit transaction: {vanished}"
                            )
                        await session.execute(
                            delete(KernelPayloadRetirement).where(
                                KernelPayloadRetirement.blob_key.in_(
                                    [row[0] for row in tombstoned]
                                )
                            )
                        )

                # 2.7. PR73 view advancement check. The head row lock is
                #     held, so the precondition evaluation below and any
                #     concurrent view head movement cannot interleave.
                #     A false precondition raises a typed conflict and
                #     rolls the whole batch back — all-or-conflict.
                view_flip: ViewFlip | None = None
                if view_advancement is not None:
                    view_flip = await check_view_advancement(
                        session,
                        workspace_id=workspace_id,
                        advancement=view_advancement,
                        prepared_records={
                            p.record_id: PreparedViewRef(
                                record_id=p.record_id,
                                record_class=p.record_class,
                                identity_hash=p.identity_hash,
                                payload_json=p.payload_json,
                            )
                            for p in prepared
                        },
                        next_commit_id=next_commit_id,
                    )
                maybe_inject(PHASE_VIEW_CHECKED)

                # 2.8. PR74 claim/proof integrity. Still before any
                #     insert and still under the writer lock: proof
                #     topology, input provenance, and claim-assessment
                #     semantics are validated against committed state
                #     overlaid with this batch, so an invalid proof can
                #     never become visible (no records, edges, manifest,
                #     outbox, or head movement survive the rollback).
                await check_batch_proof_integrity(
                    session,
                    workspace_id=workspace_id,
                    batch_records={
                        p.record_id: ProofBatchRecord(
                            record_id=p.record_id,
                            record_class=p.record_class,
                            payload_json=p.payload_json,
                        )
                        for p in prepared
                    },
                    edges=edges,
                    current_head=current_head,
                )
                maybe_inject(PHASE_PROOF_CHECKED)

                # 2.9. PR75 narrow verification-risk gate.  Structural
                # PR74 validation above always runs first; this gate only
                # activates for the explicitly recognized high-risk
                # source-native workflow and remains inside this transaction.
                await check_batch_verification_risk(
                    session,
                    workspace_id=workspace_id,
                    batch_records={
                        p.record_id: ProofBatchRecord(
                            record_id=p.record_id,
                            record_class=p.record_class,
                            payload_json=p.payload_json,
                        )
                        for p in prepared
                    },
                    current_head=current_head,
                )
                maybe_inject(PHASE_RISK_CHECKED)

                # 3. Insert logical records.
                session.add_all(
                    KernelRecord(
                        id=p.record_id,
                        workspace_id=workspace_id,
                        kernel_commit_id=next_commit_id,
                        record_class=p.record_class,
                        record_type=p.record_type,
                        schema_version=p.schema_version,
                        identity_hash=p.identity_hash,
                        payload_json=p.payload_json,
                        payload_byte_hash=p.payload_byte_hash,
                        payload_length=p.payload_length,
                    )
                    for p in prepared
                )
                maybe_inject(PHASE_RECORDS_INSERTED)

                # 3.5. Register durably published payload objects. The
                #     objects were staged and verified before this
                #     transaction began; the registry row is what makes
                #     the reference "available" — and it appears or
                #     disappears together with the records above.
                for blob_key in sorted(staged_payloads):
                    length, locator = staged_payloads[blob_key]
                    await session.execute(
                        dialect_insert(session.bind, KernelPayloadObject)
                        .values(
                            blob_key=blob_key,
                            payload_length=length,
                            store_profile=LOCAL_STORE_PROFILE,
                            storage_locator=locator,
                        )
                        .on_conflict_do_nothing(index_elements=[KernelPayloadObject.blob_key])
                    )
                maybe_inject(PHASE_PAYLOADS_REGISTERED)

                # 4. Insert dependency edges.
                session.add_all(
                    KernelRecordEdge(
                        id=edge.edge_id,
                        workspace_id=workspace_id,
                        kernel_commit_id=next_commit_id,
                        edge_kind=edge.edge_kind,
                        source_record_id=edge.source_ref,
                        target_record_id=edge.target_ref,
                    )
                    for edge in edges
                )
                maybe_inject(PHASE_EDGES_INSERTED)

                # 5. Insert the immutable manifest.
                class_counts: dict[str, int] = {}
                for p in prepared:
                    class_counts[p.record_class] = class_counts.get(p.record_class, 0) + 1
                record_root = compute_record_root(
                    record_root_entry(p.identity_hash, p.payload_byte_hash) for p in prepared
                )
                edge_root = compute_edge_root(
                    edge_root_entry(edge.source_ref, edge.target_ref, edge.edge_kind)
                    for edge in edges
                )
                manifest_payload = manifest_identity_payload(
                    workspace_id=workspace_id,
                    kernel_commit_id=next_commit_id,
                    parent_kernel_commit_id=current_head,
                    record_count=len(prepared),
                    edge_count=len(edges),
                    record_class_counts=class_counts,
                    record_identity_root=record_root,
                    edge_identity_root=edge_root,
                    canonicalization_profile=CANONICALIZATION_PROFILE,
                )
                manifest_hash = compute_manifest_identity_hash(manifest_payload)
                session.add(
                    KernelCommitManifest(
                        workspace_id=workspace_id,
                        kernel_commit_id=next_commit_id,
                        parent_kernel_commit_id=current_head,
                        record_count=len(prepared),
                        edge_count=len(edges),
                        record_class_counts_json=canonical_json_str(class_counts),
                        record_identity_root=record_root,
                        edge_identity_root=edge_root,
                        manifest_identity_hash=manifest_hash,
                        kernel_schema_version=KERNEL_SCHEMA_VERSION,
                        canonicalization_profile=CANONICALIZATION_PROFILE,
                        producer_json=producer_json,
                    )
                )
                maybe_inject(PHASE_MANIFEST_INSERTED)

                # 5.5. Enqueue successor-work intent. Same transaction as
                #      the commit that authorizes it: rolled back with it,
                #      durable with it. Dedupe keys are deterministic, so
                #      a retried commit protocol cannot duplicate intent.
                inserted_outbox_keys: list[str] = []
                for intent, payload_json in prepared_outbox:
                    dedupe_key = compute_dedupe_key(
                        workspace_id=workspace_id,
                        kernel_commit_id=next_commit_id,
                        work_kind=intent.work_kind,
                        payload_json=payload_json,
                    )
                    result = await session.execute(
                        dialect_insert(session.bind, KernelOutbox)
                        .values(
                            workspace_id=workspace_id,
                            kernel_commit_id=next_commit_id,
                            work_kind=intent.work_kind,
                            payload_json=payload_json,
                            dedupe_key=dedupe_key,
                            state=OUTBOX_STATE_PENDING,
                            attempts=0,
                        )
                        .on_conflict_do_nothing(index_elements=[KernelOutbox.dedupe_key])
                    )
                    if result.rowcount == 1:
                        inserted_outbox_keys.append(dedupe_key)
                maybe_inject(PHASE_OUTBOX_INSERTED)

                # 5.7. PR73 view head movement. Conditional on the exact
                #      base observed during the check above (still under
                #      this transaction's writer lock); anything but one
                #      affected row is a stale-base conflict that rolls
                #      the whole commit back.
                if view_flip is not None:
                    if view_flip.kind == "insert":
                        session.add(
                            KernelViewHead(
                                workspace_id=view_flip.workspace_id,
                                view_id=view_flip.view_id,
                                current_revision_id=view_flip.new_revision_id,
                                kernel_commit_id=view_flip.kernel_commit_id,
                            )
                        )
                    else:
                        flip_result = await session.execute(
                            update(KernelViewHead)
                            .where(
                                KernelViewHead.workspace_id == view_flip.workspace_id,
                                KernelViewHead.view_id == view_flip.view_id,
                                KernelViewHead.current_revision_id
                                == view_flip.expected_base_revision_id,
                            )
                            .values(
                                current_revision_id=view_flip.new_revision_id,
                                kernel_commit_id=view_flip.kernel_commit_id,
                                updated_at=datetime.now(timezone.utc),
                            )
                        )
                        if flip_result.rowcount != 1:
                            raise StaleBaseRevisionError(
                                expected_base_revision_id=(
                                    view_flip.expected_base_revision_id
                                ),
                                observed_base_revision_id=None,
                            )
                maybe_inject(PHASE_VIEW_ADVANCED)

                # 6. Conditional head advance (lost-update guard).
                result = await session.execute(
                    update(KernelCommitHead)
                    .where(
                        KernelCommitHead.workspace_id == workspace_id,
                        KernelCommitHead.head_kernel_commit_id == current_head,
                    )
                    .values(
                        head_kernel_commit_id=next_commit_id,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                if result.rowcount != 1:
                    raise HeadMovedError(
                        f"workspace={workspace_id!r}: head moved from {current_head} "
                        "concurrently; retrying"
                    )
                maybe_inject(PHASE_HEAD_ADVANCED)
                maybe_inject(PHASE_PRE_COMMIT)
            # session.begin() exit == COMMIT == the linearization point.

            outbox_ids: tuple[int, ...] = ()
            if inserted_outbox_keys:
                rows = (
                    await session.execute(
                        select(KernelOutbox.id)
                        .where(KernelOutbox.dedupe_key.in_(inserted_outbox_keys))
                        .order_by(KernelOutbox.id.asc())
                    )
                ).all()
                outbox_ids = tuple(row.id for row in rows)

        return KernelCommitReceipt(
            workspace_id=workspace_id,
            kernel_commit_id=next_commit_id,
            parent_kernel_commit_id=current_head,
            manifest_identity_hash=manifest_hash,
            record_ids=tuple(p.record_id for p in prepared),
            edge_ids=tuple(edge.edge_id for edge in edges),
            record_count=len(prepared),
            edge_count=len(edges),
            payload_blob_keys=tuple(sorted(staged_payloads)),
            outbox_ids=outbox_ids,
        )


def _retry_delay(base: float, attempt: int) -> float:
    return min(base * 2**attempt, MAX_RETRY_DELAY)


def _map_integrity_error(workspace_id: str, exc: IntegrityError) -> KernelError:
    # PostgreSQL carries the violated constraint natively; SQLite only
    # embeds it in the message text. Both vocabularies must map to the
    # same typed kernel conflicts.
    constraint = integrity_constraint_name(exc) or ""
    text = str(exc)
    upper = text.upper()
    if (
        _duplicate_identity_marker in constraint
        or _duplicate_identity_marker in text
        or ("UNIQUE" in upper and "KERNEL_RECORDS" in upper and "IDENTITY_HASH" in upper)
    ):
        return DuplicateRecordIdentityError(
            f"workspace={workspace_id!r}: record semantic identity already committed; "
            "supersession requires a new record"
        )
    if (
        _manifest_pk_marker in constraint
        or _manifest_pk_marker in text
        or ("UNIQUE" in upper and "KERNEL_COMMIT_MANIFESTS" in upper)
    ):
        return HeadMovedError(
            f"workspace={workspace_id!r}: commit id already taken concurrently"
        )
    return KernelError(f"workspace={workspace_id!r}: constraint failure: {text}")


_default_service: KernelCommitService | None = None


def default_commit_service() -> KernelCommitService:
    """Process-wide service bound to the production engine.

    Commits fail closed until ``verify_database_ready`` passes; the check
    runs once per process (mirrors the agent API readiness convention).
    Payload-bearing commits stage through the local content-addressed
    store rooted at ``MARKER_KERNEL_PAYLOAD_ROOT`` (default
    ``<data>/kernel_payloads``).
    """
    global _default_service
    if _default_service is None:
        from app.core.config import KERNEL_PAYLOAD_ROOT
        from app.database import async_session_factory
        from app.db_migration import verify_database_ready

        _default_service = KernelCommitService(
            async_session_factory,
            readiness_check=verify_database_ready,
            payload_store=LocalPayloadStore(KERNEL_PAYLOAD_ROOT),
        )
    return _default_service
