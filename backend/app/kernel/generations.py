"""Immutable materialized generations over pinned kernel snapshots (PR65A).

A generation is a **rebuildable read model, never a second truth
authority**: it is copied deterministically from exactly one committed
kernel cut (a :class:`~app.kernel.snapshots.KernelSnapshot`) under a
declared materializer/schema/config identity. Every row is derived,
discardable, and reproducible.

Lifecycle (three durable steps, each atomic):

1. **build → staged.** One transaction reads the committed cut, inserts
   the materialized record/edge rows, and writes the generation manifest
   row in state ``staged`` with the content digest computed from the
   source cut. A crash or injected fault inside this transaction rolls
   the whole staging back; a fault after it leaves at most an
   identifiable ``staged``/``failed`` residue that is never current.
2. **validate → validated.** The digest is recomputed *from the
   materialized rows* and must match; counts, workspace bounds, and cut
   bounds are re-checked. A mismatch marks the generation ``failed`` and
   raises — the previously accepted generation is untouched.
3. **activate.** One transaction flips the per-workspace
   ``kernel_generation_heads`` pointer under a conditional update,
   superseding the previous active generation. This database commit is
   the linearization point: readers observe the old accepted generation
   or the complete new one, never a mix.

``generation_id`` is deterministic over (workspace, cut, snapshot id,
materializer identity, schema version, canonical config): rebuilding
the same declared inputs either reproduces the same content digest
(idempotent reuse, immutable rows never rewritten) or fails closed as
an integrity violation. Readers pin a generation by identity; a reader
that began on generation A keeps reading A after B activates, because
materialized rows are immutable and superseded generations remain
readable until PR65B retention retires them.

Retention boundary (PR65B): this service still performs no physical
deletion — retirement of superseded generations and payload bytes is
owned by :mod:`app.kernel.gc`. A reader that needs protection across a
collection pass opens the generation **pinned**
(:func:`open_pinned_generation` / ``open_current_generation(pin_lease_seconds=...)``):
the durable lease is an active retention root until released or
expired. An unpinned superseded generation is collectible the moment
no other root protects it. Worker fencing/leases/exactly-once
publication remain PR66 and are intentionally absent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    GenerationIntegrityError,
    GenerationStateError,
    InjectedFaultError,
    KernelError,
    UnknownGenerationError,
)
from app.kernel.models import (
    KernelGeneration,
    KernelGenerationEdge,
    KernelGenerationHead,
    KernelGenerationRecord,
    KernelRecord,
    KernelRecordEdge,
)
from app.kernel.retention import (
    DEFAULT_PIN_LEASE_SECONDS,
    acquire_reader_pin,
    release_reader_pin,
    renew_reader_pin,
)
from app.kernel.snapshots import KernelSnapshot
from app.utils.canonical import (
    CanonicalValueError,
    canonical_json_bytes,
    canonical_json_str,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "GENERATION_FAULT_PHASES",
    "GENERATION_RECORD_TYPE",
    "GENERATION_SCHEMA_VERSION",
    "GENERATION_STATE_ACTIVE",
    "GENERATION_STATE_FAILED",
    "GENERATION_STATE_STAGED",
    "GENERATION_STATE_SUPERSEDED",
    "GENERATION_STATE_VALIDATED",
    "GenerationEdge",
    "GenerationReader",
    "GenerationRecord",
    "GenerationRef",
    "GenerationService",
    "MATERIALIZER_ID",
    "MATERIALIZER_VERSION",
    "PHASE_GEN_BUILD_BEGIN",
    "PHASE_GEN_POST_ACTIVATE",
    "PHASE_GEN_PRE_ACTIVATE",
    "PHASE_GEN_RECORDS_MATERIALIZED",
    "PHASE_GEN_SOURCE_READ",
    "PHASE_GEN_STAGED",
    "PHASE_GEN_VALIDATED",
    "PHASE_GEN_VALIDATE_BEGIN",
    "compute_generation_identity",
    "default_generation_service",
    "open_current_generation",
    "open_generation",
    "open_pinned_generation",
    "resolve_current_generation",
    "verify_generation",
]

#: Framing domain separating generation identity from other kernel hashes.
GENERATION_RECORD_TYPE = "marker.kernel.generation.v1"
GENERATION_ID_SCHEMA_VERSION = "1.0.0"

#: Identity of the PR65A materializer: it materializes the kernel's own
#: committed semantic material (records + edges) bounded to one cut.
MATERIALIZER_ID = "marker.kernel.materializer.kernel_state.v1"
MATERIALIZER_VERSION = "1.0.0"

#: Schema version of the materialized view (records/edges/summary shape).
GENERATION_SCHEMA_VERSION = "1.0.0"

GENERATION_STATE_STAGED = "staged"
GENERATION_STATE_VALIDATED = "validated"
GENERATION_STATE_ACTIVE = "active"
GENERATION_STATE_SUPERSEDED = "superseded"
GENERATION_STATE_FAILED = "failed"

#: Deterministic fault-injection phases (test-only parameters).
PHASE_GEN_BUILD_BEGIN = "gen-build-begin"
PHASE_GEN_SOURCE_READ = "gen-source-read"
PHASE_GEN_RECORDS_MATERIALIZED = "gen-records-materialized"
PHASE_GEN_STAGED = "gen-staged"
PHASE_GEN_VALIDATE_BEGIN = "gen-validate-begin"
PHASE_GEN_VALIDATED = "gen-validated"
PHASE_GEN_PRE_ACTIVATE = "gen-pre-activate"
PHASE_GEN_POST_ACTIVATE = "gen-post-activate"

GENERATION_FAULT_PHASES = frozenset(
    {
        PHASE_GEN_BUILD_BEGIN,
        PHASE_GEN_SOURCE_READ,
        PHASE_GEN_RECORDS_MATERIALIZED,
        PHASE_GEN_STAGED,
        PHASE_GEN_VALIDATE_BEGIN,
        PHASE_GEN_VALIDATED,
        PHASE_GEN_PRE_ACTIVATE,
        PHASE_GEN_POST_ACTIVATE,
    }
)

DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
MAX_RETRY_DELAY = 0.5

_BUSY_MARKERS = ("database is locked", "database table is locked", "database is busy")

# Phases honored by build() alone; build_and_activate() additionally
# honors the validate→activate boundary and the activation phases.
_BUILD_PHASES = frozenset(
    {
        PHASE_GEN_BUILD_BEGIN,
        PHASE_GEN_SOURCE_READ,
        PHASE_GEN_RECORDS_MATERIALIZED,
        PHASE_GEN_STAGED,
        PHASE_GEN_VALIDATE_BEGIN,
    }
)
_ACTIVATE_PHASES = frozenset(
    {PHASE_GEN_VALIDATED, PHASE_GEN_PRE_ACTIVATE, PHASE_GEN_POST_ACTIVATE}
)


class _ConcurrentPointerMove(Exception):
    """Internal retry signal: the current-generation pointer moved."""


def _is_busy(exc: OperationalError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def _retry_delay(base: float, attempt: int) -> float:
    return min(base * (2**attempt), MAX_RETRY_DELAY)


def compute_generation_identity(
    *,
    workspace_id: str,
    kernel_commit_id: int,
    snapshot_id: str,
    materializer_id: str = MATERIALIZER_ID,
    materializer_version: str = MATERIALIZER_VERSION,
    schema_version: str = GENERATION_SCHEMA_VERSION,
    config_json: str = "{}",
) -> str:
    """Deterministic generation identity over the declared inputs."""
    return record_identity_hash(
        record_type=GENERATION_RECORD_TYPE,
        schema_version=GENERATION_ID_SCHEMA_VERSION,
        payload={
            "workspace_id": workspace_id,
            "kernel_commit_id": kernel_commit_id,
            "snapshot_id": snapshot_id,
            "materializer": {
                "id": materializer_id,
                "version": materializer_version,
            },
            "schema_version": schema_version,
            "config": json.loads(config_json),
        },
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationRef:
    """Manifest-level view of one generation (its summary/read model)."""

    generation_id: str
    workspace_id: str
    kernel_commit_id: int
    snapshot_id: str
    materializer_id: str
    materializer_version: str
    schema_version: str
    config: dict
    state: str
    content_digest: str
    commit_count: int
    record_count: int
    edge_count: int
    record_class_counts: dict
    required_payload_state: str
    completeness: str
    payload_state_counts: dict
    created_at: str | None
    validated_at: str | None
    activated_at: str | None


@dataclass(frozen=True)
class GenerationRecord:
    """One materialized record inside one generation."""

    record_id: str
    workspace_id: str
    kernel_commit_id: int
    record_class: str
    record_type: str
    schema_version: str
    identity_hash: str
    payload: dict
    payload_json: str
    payload_byte_hash: str | None
    payload_length: int | None


@dataclass(frozen=True)
class GenerationEdge:
    """One materialized dependency edge inside one generation."""

    edge_id: str
    workspace_id: str
    kernel_commit_id: int
    edge_kind: str
    source_record_id: str
    target_record_id: str


def _ref(row: KernelGeneration) -> GenerationRef:
    return GenerationRef(
        generation_id=row.generation_id,
        workspace_id=row.workspace_id,
        kernel_commit_id=row.kernel_commit_id,
        snapshot_id=row.snapshot_id,
        materializer_id=row.materializer_id,
        materializer_version=row.materializer_version,
        schema_version=row.schema_version,
        config=json.loads(row.config_json),
        state=row.state,
        content_digest=row.content_digest,
        commit_count=row.commit_count,
        record_count=row.record_count,
        edge_count=row.edge_count,
        record_class_counts=json.loads(row.record_class_counts_json),
        required_payload_state=row.required_payload_state,
        completeness=row.completeness,
        payload_state_counts=json.loads(row.payload_state_counts_json),
        created_at=row.created_at.isoformat() if row.created_at else None,
        validated_at=row.validated_at.isoformat() if row.validated_at else None,
        activated_at=row.activated_at.isoformat() if row.activated_at else None,
    )


# ---------------------------------------------------------------------------
# Deterministic content digest (shared by build/validate/verify)
# ---------------------------------------------------------------------------


def _record_digest_entry(
    *,
    kernel_commit_id: int,
    record_id: str,
    record_class: str,
    record_type: str,
    schema_version: str,
    identity_hash: str,
    payload_json: str,
    payload_byte_hash: str | None,
    payload_length: int | None,
) -> dict:
    return {
        "commit": kernel_commit_id,
        "id": record_id,
        "class": record_class,
        "type": record_type,
        "schema_version": schema_version,
        "identity_hash": identity_hash,
        "payload_json": payload_json,
        "payload_byte_hash": payload_byte_hash,
        "payload_length": payload_length,
    }


def _edge_digest_entry(
    *,
    kernel_commit_id: int,
    edge_id: str,
    edge_kind: str,
    source: str,
    target: str,
) -> dict:
    return {
        "commit": kernel_commit_id,
        "id": edge_id,
        "kind": edge_kind,
        "source": source,
        "target": target,
    }


def _content_digest(
    *,
    workspace_id: str,
    cut: int,
    record_entries: Sequence[Mapping],
    edge_entries: Sequence[Mapping],
) -> tuple[str, dict[str, int], int, int]:
    class_counts: dict[str, int] = {}
    for entry in record_entries:
        class_counts[entry["class"]] = class_counts.get(entry["class"], 0) + 1
    view = {
        "workspace_id": workspace_id,
        "kernel_commit_id": cut,
        "record_count": len(record_entries),
        "edge_count": len(edge_entries),
        "record_class_counts": class_counts,
        "records": list(record_entries),
        "edges": list(edge_entries),
    }
    digest = payload_byte_hash(canonical_json_bytes(to_json_ready(view)))
    return digest, class_counts, len(record_entries), len(edge_entries)


_RECORD_ORDER = (
    KernelRecord.kernel_commit_id.asc(),
    KernelRecord.identity_hash.asc(),
    KernelRecord.id.asc(),
)
_EDGE_ORDER = (
    KernelRecordEdge.kernel_commit_id.asc(),
    KernelRecordEdge.source_record_id.asc(),
    KernelRecordEdge.target_record_id.asc(),
    KernelRecordEdge.edge_kind.asc(),
    KernelRecordEdge.id.asc(),
)
_GEN_RECORD_ORDER = (
    KernelGenerationRecord.kernel_commit_id.asc(),
    KernelGenerationRecord.identity_hash.asc(),
    KernelGenerationRecord.record_id.asc(),
)
_GEN_EDGE_ORDER = (
    KernelGenerationEdge.kernel_commit_id.asc(),
    KernelGenerationEdge.source_record_id.asc(),
    KernelGenerationEdge.target_record_id.asc(),
    KernelGenerationEdge.edge_kind.asc(),
    KernelGenerationEdge.edge_id.asc(),
)


async def _read_cut(
    session_factory: async_sessionmaker, workspace_id: str, cut: int
) -> tuple[list[KernelRecord], list[KernelRecordEdge]]:
    async with session_factory() as session:
        records = (
            (
                await session.execute(
                    select(KernelRecord)
                    .where(
                        KernelRecord.workspace_id == workspace_id,
                        KernelRecord.kernel_commit_id <= cut,
                    )
                    .order_by(*_RECORD_ORDER)
                )
            )
            .scalars()
            .all()
        )
        edges = (
            (
                await session.execute(
                    select(KernelRecordEdge)
                    .where(
                        KernelRecordEdge.workspace_id == workspace_id,
                        KernelRecordEdge.kernel_commit_id <= cut,
                    )
                    .order_by(*_EDGE_ORDER)
                )
            )
            .scalars()
            .all()
        )
    return list(records), list(edges)


def _source_record_entry(row: KernelRecord) -> dict:
    return _record_digest_entry(
        kernel_commit_id=row.kernel_commit_id,
        record_id=row.id,
        record_class=row.record_class,
        record_type=row.record_type,
        schema_version=row.schema_version,
        identity_hash=row.identity_hash,
        payload_json=row.payload_json,
        payload_byte_hash=row.payload_byte_hash,
        payload_length=row.payload_length,
    )


def _source_edge_entry(row: KernelRecordEdge) -> dict:
    return _edge_digest_entry(
        kernel_commit_id=row.kernel_commit_id,
        edge_id=row.id,
        edge_kind=row.edge_kind,
        source=row.source_record_id,
        target=row.target_record_id,
    )


def _materialized_record_entry(row: KernelGenerationRecord) -> dict:
    return _record_digest_entry(
        kernel_commit_id=row.kernel_commit_id,
        record_id=row.record_id,
        record_class=row.record_class,
        record_type=row.record_type,
        schema_version=row.schema_version,
        identity_hash=row.identity_hash,
        payload_json=row.payload_json,
        payload_byte_hash=row.payload_byte_hash,
        payload_length=row.payload_length,
    )


def _materialized_edge_entry(row: KernelGenerationEdge) -> dict:
    return _edge_digest_entry(
        kernel_commit_id=row.kernel_commit_id,
        edge_id=row.edge_id,
        edge_kind=row.edge_kind,
        source=row.source_record_id,
        target=row.target_record_id,
    )


async def _load_generation(
    session_factory: async_sessionmaker, generation_id: str
) -> GenerationRef | None:
    async with session_factory() as session:
        row = await session.get(KernelGeneration, generation_id)
    return _ref(row) if row is not None else None


# ---------------------------------------------------------------------------
# Generation service
# ---------------------------------------------------------------------------


class GenerationService:
    """Build → validate → activate lifecycle for materialized generations.

    One instance owns generation writes for one database; instantiate it
    once per process (see :func:`default_generation_service`). The
    service never mutates kernel schema or kernel truth: it only reads
    committed state and writes derived generation rows. When constructed
    with a ``readiness_check`` (normally ``verify_database_ready``) it
    fails closed on an unmigrated database.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        readiness_check: Any = None,
        busy_retry_attempts: int = DEFAULT_BUSY_RETRY_ATTEMPTS,
        busy_retry_base_delay: float = DEFAULT_BUSY_RETRY_BASE_DELAY,
    ) -> None:
        self._session_factory = session_factory
        self._readiness_check = readiness_check
        self._ready = readiness_check is None
        self._busy_retry_attempts = busy_retry_attempts
        self._busy_retry_base_delay = busy_retry_base_delay

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self._readiness_check()
        self._ready = True

    # -- build ------------------------------------------------------------

    async def build(
        self,
        snapshot: KernelSnapshot,
        *,
        config: Mapping[str, Any] | None = None,
        _inject_fault_at: str | None = None,
    ) -> GenerationRef:
        """Materialize one generation from a pinned snapshot.

        Returns the generation in state ``validated`` (or its existing
        immutable row when the identical declared generation was already
        built). Activation is a separate, explicit step.
        """
        if _inject_fault_at is not None and _inject_fault_at not in _BUILD_PHASES:
            if _inject_fault_at in _ACTIVATE_PHASES:
                raise KernelError(
                    f"fault phase {_inject_fault_at!r} applies to "
                    "build_and_activate/activate, not build"
                )
            raise KernelError(f"unknown fault phase {_inject_fault_at!r}")
        await self._ensure_ready()
        ref = await self._build_validated(
            snapshot, config=config, fault=_inject_fault_at
        )
        return ref

    async def build_and_activate(
        self,
        snapshot: KernelSnapshot,
        *,
        config: Mapping[str, Any] | None = None,
        _inject_fault_at: str | None = None,
    ) -> GenerationRef:
        """Convenience lifecycle: build, validate, then atomically activate."""
        if _inject_fault_at is not None and _inject_fault_at not in GENERATION_FAULT_PHASES:
            raise KernelError(f"unknown fault phase {_inject_fault_at!r}")
        await self._ensure_ready()
        fault = _inject_fault_at

        ref = await self._build_validated(snapshot, config=config, fault=fault)

        if fault == PHASE_GEN_VALIDATED:
            raise InjectedFaultError(PHASE_GEN_VALIDATED)

        if ref.state == GENERATION_STATE_ACTIVE:
            return ref  # idempotent rebuild of the live generation
        # Route through the retry-wrapped activation path; only the
        # activation phases can still fire at this point.
        return await self.activate(
            ref.generation_id,
            _inject_fault_at=fault if fault in _ACTIVATE_PHASES else None,
        )

    async def _build_validated(
        self,
        snapshot: KernelSnapshot,
        *,
        config: Mapping[str, Any] | None,
        fault: str | None,
    ) -> GenerationRef:
        def maybe_inject(phase: str) -> None:
            if fault == phase:
                raise InjectedFaultError(phase)

        maybe_inject(PHASE_GEN_BUILD_BEGIN)

        try:
            config_json = canonical_json_str(to_json_ready(dict(config or {})))
        except CanonicalValueError as exc:
            raise KernelError(f"generation config rejected: {exc}") from exc

        generation_id = compute_generation_identity(
            workspace_id=snapshot.workspace_id,
            kernel_commit_id=snapshot.kernel_commit_id,
            snapshot_id=snapshot.snapshot_id,
            config_json=config_json,
        )

        # Source read is stable: membership is bounded by the pinned cut,
        # so concurrent commits cannot change what is materialized.
        source_records, source_edges = await _read_cut(
            self._session_factory, snapshot.workspace_id, snapshot.kernel_commit_id
        )
        maybe_inject(PHASE_GEN_SOURCE_READ)

        record_entries = [_source_record_entry(r) for r in source_records]
        edge_entries = [_source_edge_entry(e) for e in source_edges]
        digest, class_counts, record_count, edge_count = _content_digest(
            workspace_id=snapshot.workspace_id,
            cut=snapshot.kernel_commit_id,
            record_entries=record_entries,
            edge_entries=edge_entries,
        )
        if (record_count, edge_count) != (snapshot.record_count, snapshot.edge_count):
            raise GenerationIntegrityError(
                f"generation={generation_id}: cut counts diverge from the pinned "
                f"snapshot (built {record_count}/{edge_count}, snapshot "
                f"{snapshot.record_count}/{snapshot.edge_count})"
            )

        existing = await _load_generation(self._session_factory, generation_id)
        if existing is not None and existing.state not in (
            GENERATION_STATE_STAGED,
            GENERATION_STATE_FAILED,
        ):
            if existing.content_digest != digest:
                raise GenerationIntegrityError(
                    f"generation={generation_id}: same declared inputs produced "
                    f"different content (stored {existing.content_digest}, "
                    f"rebuilt {digest}); refusing to rewrite an immutable generation"
                )
            return existing  # idempotent rebuild: immutable rows untouched

        await self._retry(
            lambda: self._stage_transaction(
                generation_id, snapshot, config_json, digest,
                class_counts, record_count, edge_count, source_records,
                source_edges, maybe_inject,
            )
        )

        maybe_inject(PHASE_GEN_STAGED)
        maybe_inject(PHASE_GEN_VALIDATE_BEGIN)
        return await self._retry(lambda: self._validate_transaction(generation_id))

    async def _stage_transaction(
        self,
        generation_id: str,
        snapshot: KernelSnapshot,
        config_json: str,
        digest: str,
        class_counts: dict[str, int],
        record_count: int,
        edge_count: int,
        source_records: Sequence[KernelRecord],
        source_edges: Sequence[KernelRecordEdge],
        maybe_inject: Any,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                # A concurrent builder may have advanced this identity
                # past staging since the pre-transaction check; staging
                # again would purge rows that are already authoritative.
                existing = await session.get(KernelGeneration, generation_id)
                if existing is not None and existing.state not in (
                    GENERATION_STATE_STAGED,
                    GENERATION_STATE_FAILED,
                ):
                    if existing.content_digest != digest:
                        raise GenerationIntegrityError(
                            f"generation={generation_id}: same declared inputs "
                            f"produced different content (stored "
                            f"{existing.content_digest}, rebuilt {digest})"
                        )
                    return  # durable; the validate step returns its ref

                # Purge only never-activated residue for this identity.
                await session.execute(
                    delete(KernelGenerationRecord).where(
                        KernelGenerationRecord.generation_id == generation_id
                    )
                )
                await session.execute(
                    delete(KernelGenerationEdge).where(
                        KernelGenerationEdge.generation_id == generation_id
                    )
                )
                referenced = await session.scalar(
                    select(func.count())
                    .select_from(KernelGenerationHead)
                    .where(
                        KernelGenerationHead.current_generation_id == generation_id
                    )
                )
                if referenced:
                    raise GenerationStateError(
                        f"generation={generation_id}: refusing to purge a "
                        "generation referenced as current"
                    )
                await session.execute(
                    delete(KernelGeneration).where(
                        KernelGeneration.generation_id == generation_id,
                        KernelGeneration.state.in_(
                            [GENERATION_STATE_STAGED, GENERATION_STATE_FAILED]
                        ),
                    )
                )

                session.add_all(
                    KernelGenerationRecord(
                        generation_id=generation_id,
                        record_id=row.id,
                        workspace_id=snapshot.workspace_id,
                        kernel_commit_id=row.kernel_commit_id,
                        record_class=row.record_class,
                        record_type=row.record_type,
                        schema_version=row.schema_version,
                        identity_hash=row.identity_hash,
                        payload_json=row.payload_json,
                        payload_byte_hash=row.payload_byte_hash,
                        payload_length=row.payload_length,
                    )
                    for row in source_records
                )
                session.add_all(
                    KernelGenerationEdge(
                        generation_id=generation_id,
                        edge_id=row.id,
                        workspace_id=snapshot.workspace_id,
                        kernel_commit_id=row.kernel_commit_id,
                        edge_kind=row.edge_kind,
                        source_record_id=row.source_record_id,
                        target_record_id=row.target_record_id,
                    )
                    for row in source_edges
                )
                maybe_inject(PHASE_GEN_RECORDS_MATERIALIZED)
                session.add(
                    KernelGeneration(
                        generation_id=generation_id,
                        workspace_id=snapshot.workspace_id,
                        kernel_commit_id=snapshot.kernel_commit_id,
                        snapshot_id=snapshot.snapshot_id,
                        materializer_id=MATERIALIZER_ID,
                        materializer_version=MATERIALIZER_VERSION,
                        schema_version=GENERATION_SCHEMA_VERSION,
                        config_json=config_json,
                        state=GENERATION_STATE_STAGED,
                        content_digest=digest,
                        commit_count=snapshot.commit_count,
                        record_count=record_count,
                        edge_count=edge_count,
                        record_class_counts_json=canonical_json_str(class_counts),
                        required_payload_state=snapshot.required_payload_state,
                        completeness=snapshot.completeness,
                        payload_state_counts_json=canonical_json_str(
                            dict(snapshot.payload_state_counts)
                        ),
                    )
                )

    async def _validate_transaction(self, generation_id: str) -> GenerationRef:
        """Recompute the digest from materialized rows; validated or failed.

        The staged manifest row's ``content_digest`` (computed from the
        committed source cut at staging time) is the expectation; the
        recomputation runs over the materialized rows themselves, so any
        divergence between source and materialized state — corruption or
        tampering — marks the generation ``failed`` and raises.
        """
        async with self._session_factory() as session:
            row = await session.get(KernelGeneration, generation_id)
            if row is None:
                raise UnknownGenerationError(
                    f"generation={generation_id}: staged row vanished"
                )
            if row.state in (GENERATION_STATE_VALIDATED, GENERATION_STATE_ACTIVE):
                return _ref(row)  # concurrent validator (or activator) won the race
            if row.state != GENERATION_STATE_STAGED:
                raise GenerationStateError(
                    f"generation={generation_id}: cannot validate from state "
                    f"{row.state!r}"
                )

            records = (
                (
                    await session.execute(
                        select(KernelGenerationRecord)
                        .where(KernelGenerationRecord.generation_id == generation_id)
                        .order_by(*_GEN_RECORD_ORDER)
                    )
                )
                .scalars()
                .all()
            )
            edges = (
                (
                    await session.execute(
                        select(KernelGenerationEdge)
                        .where(KernelGenerationEdge.generation_id == generation_id)
                        .order_by(*_GEN_EDGE_ORDER)
                    )
                )
                .scalars()
                .all()
            )

        expected_digest = row.content_digest
        problems: list[str] = []
        recomputed, class_counts, record_count, edge_count = _content_digest(
            workspace_id=row.workspace_id,
            cut=row.kernel_commit_id,
            record_entries=[_materialized_record_entry(r) for r in records],
            edge_entries=[_materialized_edge_entry(e) for e in edges],
        )
        if recomputed != expected_digest:
            problems.append(
                f"content digest mismatch: staged {expected_digest}, "
                f"materialized {recomputed}"
            )
        if (record_count, edge_count) != (row.record_count, row.edge_count):
            problems.append(
                f"count mismatch: manifest {row.record_count}/{row.edge_count}, "
                f"materialized {record_count}/{edge_count}"
            )
        if class_counts != json.loads(row.record_class_counts_json):
            problems.append("record class counts mismatch")
        for record in records:
            if record.workspace_id != row.workspace_id:
                problems.append(f"record {record.record_id!r} from foreign workspace")
            if record.kernel_commit_id > row.kernel_commit_id:
                problems.append(
                    f"record {record.record_id!r} leaks commit "
                    f"{record.kernel_commit_id} > cut {row.kernel_commit_id}"
                )
        for edge in edges:
            if edge.workspace_id != row.workspace_id:
                problems.append(f"edge {edge.edge_id!r} from foreign workspace")
            if edge.kernel_commit_id > row.kernel_commit_id:
                problems.append(
                    f"edge {edge.edge_id!r} leaks commit "
                    f"{edge.kernel_commit_id} > cut {row.kernel_commit_id}"
                )

        new_state = (
            GENERATION_STATE_VALIDATED if not problems else GENERATION_STATE_FAILED
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelGeneration)
                    .where(
                        KernelGeneration.generation_id == generation_id,
                        KernelGeneration.state == GENERATION_STATE_STAGED,
                    )
                    .values(
                        state=new_state,
                        validated_at=datetime.now(timezone.utc)
                        if new_state == GENERATION_STATE_VALIDATED
                        else None,
                    )
                )
                if result.rowcount != 1:
                    # A concurrent validator moved the state first. Both
                    # validators computed deterministically over the same
                    # staged bytes, so agreement is the only acceptable
                    # outcome; anything else is an integrity fault.
                    moved = await session.get(KernelGeneration, generation_id)
                    advanced = new_state == GENERATION_STATE_VALIDATED and moved is not None and (
                        moved.state in (GENERATION_STATE_VALIDATED, GENERATION_STATE_ACTIVE)
                    )
                    if moved is not None and (moved.state == new_state or advanced):
                        winner = moved
                    else:
                        raise GenerationStateError(
                            f"generation={generation_id}: state changed to "
                            f"{None if moved is None else moved.state!r} "
                            "concurrently during validation"
                        )
                else:
                    winner = await session.get(KernelGeneration, generation_id)
        if problems:
            raise GenerationIntegrityError(
                f"generation={generation_id}: validation rejected: "
                + "; ".join(problems)
            )
        assert winner is not None
        return _ref(winner)

    # -- validate (public resume step) --------------------------------------

    async def validate(
        self, generation_id: str, *, _inject_fault_at: str | None = None
    ) -> GenerationRef:
        """Validate one staged generation (resume after a staged crash).

        ``build()`` already validates before returning; this explicit
        step exists so a generation left ``staged`` by a crash between
        staging and validation can be validated — or honestly rejected —
        without rebuilding.
        """
        if _inject_fault_at is not None and _inject_fault_at != PHASE_GEN_VALIDATE_BEGIN:
            raise KernelError(
                f"fault phase {_inject_fault_at!r} does not apply to validate"
            )
        await self._ensure_ready()
        if _inject_fault_at == PHASE_GEN_VALIDATE_BEGIN:
            raise InjectedFaultError(PHASE_GEN_VALIDATE_BEGIN)
        return await self._retry(lambda: self._validate_transaction(generation_id))

    # -- activate ----------------------------------------------------------

    async def activate(
        self, generation_id: str, *, _inject_fault_at: str | None = None
    ) -> GenerationRef:
        """Atomically make one validated generation the current read model."""
        if _inject_fault_at is not None and _inject_fault_at not in _ACTIVATE_PHASES:
            raise KernelError(
                f"fault phase {_inject_fault_at!r} does not apply to activate"
            )
        await self._ensure_ready()
        return await self._retry(
            lambda: self._activate(generation_id, fault=_inject_fault_at)
        )

    async def _activate(self, generation_id: str, *, fault: str | None) -> GenerationRef:
        def maybe_inject(phase: str) -> None:
            if fault == phase:
                raise InjectedFaultError(phase)

        async with self._session_factory() as session:
            async with session.begin():
                gen = await session.get(KernelGeneration, generation_id)
                if gen is None:
                    raise UnknownGenerationError(
                        f"generation={generation_id}: no such generation"
                    )
                if gen.state == GENERATION_STATE_ACTIVE:
                    head = await session.scalar(
                        select(KernelGenerationHead.current_generation_id).where(
                            KernelGenerationHead.workspace_id == gen.workspace_id
                        )
                    )
                    if head == generation_id:
                        return _ref(gen)  # idempotent activation
                    raise GenerationStateError(
                        f"generation={generation_id}: active but not current; "
                        "the pointer moved concurrently"
                    )
                if gen.state != GENERATION_STATE_VALIDATED:
                    raise GenerationStateError(
                        f"generation={generation_id}: cannot activate from state "
                        f"{gen.state!r}"
                    )
                maybe_inject(PHASE_GEN_PRE_ACTIVATE)

                observed = await session.scalar(
                    select(KernelGenerationHead.current_generation_id).where(
                        KernelGenerationHead.workspace_id == gen.workspace_id
                    )
                )
                if observed is not None and observed != generation_id:
                    result = await session.execute(
                        update(KernelGeneration)
                        .where(
                            KernelGeneration.generation_id == observed,
                            KernelGeneration.state == GENERATION_STATE_ACTIVE,
                        )
                        .values(state=GENERATION_STATE_SUPERSEDED)
                    )
                    if result.rowcount != 1:
                        raise _ConcurrentPointerMove(
                            f"previous generation {observed} was not active; "
                            "retrying with a fresh pointer observation"
                        )
                if observed is None:
                    await session.execute(
                        sqlite_insert(KernelGenerationHead)
                        .values(
                            workspace_id=gen.workspace_id,
                            current_generation_id=generation_id,
                            updated_at=datetime.now(timezone.utc),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[KernelGenerationHead.workspace_id]
                        )
                    )
                    written = await session.scalar(
                        select(KernelGenerationHead.current_generation_id).where(
                            KernelGenerationHead.workspace_id == gen.workspace_id
                        )
                    )
                    if written != generation_id:
                        raise _ConcurrentPointerMove(
                            "generation head appeared concurrently; retrying"
                        )
                else:
                    result = await session.execute(
                        update(KernelGenerationHead)
                        .where(
                            KernelGenerationHead.workspace_id == gen.workspace_id,
                            KernelGenerationHead.current_generation_id.is_(observed),
                        )
                        .values(
                            current_generation_id=generation_id,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    if result.rowcount != 1:
                        raise _ConcurrentPointerMove(
                            "current-generation pointer moved concurrently; retrying"
                        )
                result = await session.execute(
                    update(KernelGeneration)
                    .where(
                        KernelGeneration.generation_id == generation_id,
                        KernelGeneration.state == GENERATION_STATE_VALIDATED,
                    )
                    .values(
                        state=GENERATION_STATE_ACTIVE,
                        activated_at=datetime.now(timezone.utc),
                    )
                )
                if result.rowcount != 1:
                    raise _ConcurrentPointerMove(
                        "generation state moved concurrently; retrying"
                    )
            # session.begin() exit == COMMIT == the activation
            # linearization point.

        maybe_inject(PHASE_GEN_POST_ACTIVATE)
        ref = await _load_generation(self._session_factory, generation_id)
        assert ref is not None
        return ref

    # -- reads -------------------------------------------------------------

    async def get_generation(self, generation_id: str) -> GenerationRef:
        ref = await _load_generation(self._session_factory, generation_id)
        if ref is None:
            raise UnknownGenerationError(
                f"generation={generation_id}: no such generation"
            )
        return ref

    async def list_generations(
        self,
        *,
        workspace_id: str | None = None,
        state: str | None = None,
    ) -> list[GenerationRef]:
        """Generations in build order (stale-staging identification etc.)."""
        stmt = select(KernelGeneration).order_by(KernelGeneration.created_at.asc())
        if workspace_id is not None:
            stmt = stmt.where(KernelGeneration.workspace_id == workspace_id)
        if state is not None:
            stmt = stmt.where(KernelGeneration.state == state)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_ref(row) for row in rows]

    # -- retry plumbing ----------------------------------------------------

    async def _retry(self, operation: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._busy_retry_attempts):
            try:
                return await operation()
            except _ConcurrentPointerMove:
                last_error = None
            except OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_error = exc
            except IntegrityError as exc:
                # Concurrent build/activation of the same identity: the
                # winner's rows are durable; re-reading resolves idempotently.
                text = str(exc).lower()
                if "kernel_generation" not in text:
                    raise
                last_error = exc
            await asyncio.sleep(_retry_delay(self._busy_retry_base_delay, attempt))
        raise KernelError(
            f"generation operation did not converge after "
            f"{self._busy_retry_attempts} attempts: {last_error or 'pointer moved'}"
        )


# ---------------------------------------------------------------------------
# Current-generation resolution (restart recovery / new readers)
# ---------------------------------------------------------------------------


async def resolve_current_generation(
    session_factory: async_sessionmaker, workspace_id: str
) -> GenerationRef | None:
    """The current accepted read generation, from durable state only.

    A fresh process (or a restart) recovers the active generation here:
    no process memory participates. ``None`` means the workspace has
    never activated a generation.
    """
    from app.kernel.commit import validate_workspace_id

    validate_workspace_id(workspace_id)
    async with session_factory() as session:
        current_id = await session.scalar(
            select(KernelGenerationHead.current_generation_id).where(
                KernelGenerationHead.workspace_id == workspace_id
            )
        )
        if current_id is None:
            return None
        row = await session.get(KernelGeneration, current_id)
    if row is None:
        raise GenerationIntegrityError(
            f"workspace={workspace_id!r}: current generation pointer names "
            f"{current_id!r} but no such generation row exists"
        )
    return _ref(row)


def open_generation(
    session_factory: async_sessionmaker, generation_id: str
) -> GenerationReader:
    """Pin one generation by identity for bounded reads (no GC lease)."""
    return GenerationReader(session_factory, generation_id)


async def open_pinned_generation(
    session_factory: async_sessionmaker,
    generation_id: str,
    *,
    lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS,
) -> GenerationReader:
    """Open a generation under a durable reader pin (PR65B).

    The acquired lease is an active retention root: collection cannot
    retire this generation or payload bytes its declared class requires
    until the pin is released (``reader.close()``) or the lease lapses.
    Long reads renew via ``reader.renew()``. A crashed reader's pin
    expires on its own — safety never depends on process memory.
    """
    pin = await acquire_reader_pin(
        session_factory, generation_id, lease_seconds=lease_seconds
    )
    return GenerationReader(session_factory, generation_id, pin_id=pin.pin_id)


async def open_current_generation(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    pin_lease_seconds: float | None = None,
) -> GenerationReader | None:
    """Resolve the current generation, then pin it for this reader.

    ``pin_lease_seconds`` optionally acquires a durable GC lease. The
    current generation is structurally never collected, so pining it
    only matters for callers that also rely on payload bytes staying
    inspectable/replayable across collection passes.
    """
    current = await resolve_current_generation(session_factory, workspace_id)
    if current is None:
        return None
    if pin_lease_seconds is None:
        return GenerationReader(session_factory, current.generation_id)
    pin = await acquire_reader_pin(
        session_factory, current.generation_id, lease_seconds=pin_lease_seconds
    )
    return GenerationReader(
        session_factory, current.generation_id, pin_id=pin.pin_id
    )


# ---------------------------------------------------------------------------
# Generation-pinned bounded reader
# ---------------------------------------------------------------------------


class GenerationReader:
    """Bounded, generation-pinned read surface.

    The reader never replays kernel history: every query is a bounded
    lookup/enumeration over one generation's materialized rows, filtered
    by the pinned ``generation_id``. Record reads re-verify each record's
    semantic identity hash, so tampered materialized rows fail loudly
    instead of serving as valid state.

    GC protection (PR65B): a reader constructed with ``pin_id`` holds a
    durable lease that makes this generation an active retention root
    until :meth:`close` releases it (or the lease lapses). Unpinned
    readers rely on their generation staying current — a superseded
    generation being read without a pin is collectible by retention.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        generation_id: str,
        *,
        workspace_id: str | None = None,
        pin_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.generation_id = generation_id
        self._workspace_id = workspace_id
        self.pin_id = pin_id

    async def __aenter__(self) -> GenerationReader:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    @property
    def pinned(self) -> bool:
        return self.pin_id is not None

    async def renew(self, *, lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS) -> None:
        """Extend this reader's GC lease from now (long reads)."""
        if self.pin_id is None:
            raise KernelError(
                "reader holds no pin; open with open_pinned_generation to renew"
            )
        await renew_reader_pin(
            self._session_factory, self.pin_id, lease_seconds=lease_seconds
        )

    async def close(self) -> None:
        """Release this reader's pin (unpinned readers: no-op)."""
        if self.pin_id is not None:
            await release_reader_pin(self._session_factory, self.pin_id)
            self.pin_id = None

    async def summary(self) -> GenerationRef:
        ref = await _load_generation(self._session_factory, self.generation_id)
        if ref is None:
            raise UnknownGenerationError(
                f"generation={self.generation_id}: no such generation"
            )
        if self._workspace_id is not None and ref.workspace_id != self._workspace_id:
            raise GenerationIntegrityError(
                f"generation={self.generation_id}: belongs to workspace "
                f"{ref.workspace_id!r}, not {self._workspace_id!r}"
            )
        return ref

    async def get_record(self, record_id: str) -> GenerationRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KernelGenerationRecord).where(
                    KernelGenerationRecord.generation_id == self.generation_id,
                    KernelGenerationRecord.record_id == record_id,
                )
            )
        if row is None:
            return None
        return _checked_record_view(row)

    async def list_records(
        self,
        *,
        record_class: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[GenerationRecord, ...]:
        if limit <= 0 or offset < 0:
            raise KernelError("limit must be positive and offset non-negative")
        stmt = (
            select(KernelGenerationRecord)
            .where(KernelGenerationRecord.generation_id == self.generation_id)
            .order_by(
                KernelGenerationRecord.kernel_commit_id.asc(),
                KernelGenerationRecord.record_id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if record_class is not None:
            stmt = stmt.where(
                KernelGenerationRecord.record_class == record_class
            )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return tuple(_checked_record_view(row) for row in rows)

    async def count_records(self, *, record_class: str | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(KernelGenerationRecord)
            .where(KernelGenerationRecord.generation_id == self.generation_id)
        )
        if record_class is not None:
            stmt = stmt.where(KernelGenerationRecord.record_class == record_class)
        async with self._session_factory() as session:
            return await session.scalar(stmt) or 0

    async def list_edges(
        self,
        *,
        record_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[GenerationEdge, ...]:
        if limit <= 0 or offset < 0:
            raise KernelError("limit must be positive and offset non-negative")
        stmt = (
            select(KernelGenerationEdge)
            .where(KernelGenerationEdge.generation_id == self.generation_id)
            .order_by(
                KernelGenerationEdge.kernel_commit_id.asc(),
                KernelGenerationEdge.edge_id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if record_id is not None:
            stmt = stmt.where(
                (KernelGenerationEdge.source_record_id == record_id)
                | (KernelGenerationEdge.target_record_id == record_id)
            )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return tuple(
            GenerationEdge(
                edge_id=row.edge_id,
                workspace_id=row.workspace_id,
                kernel_commit_id=row.kernel_commit_id,
                edge_kind=row.edge_kind,
                source_record_id=row.source_record_id,
                target_record_id=row.target_record_id,
            )
            for row in rows
        )

    async def verify(self) -> GenerationVerification:
        """Full integrity verification of this pinned generation."""
        return await verify_generation(self._session_factory, self.generation_id)


def _checked_record_view(row: KernelGenerationRecord) -> GenerationRecord:
    try:
        payload = json.loads(row.payload_json)
        recomputed = record_identity_hash(
            record_type=row.record_type,
            schema_version=row.schema_version,
            payload=to_json_ready(payload),
        )
    except Exception as exc:
        raise GenerationIntegrityError(
            f"generation={row.generation_id} record={row.record_id!r}: payload "
            f"unreadable/rejected: {exc}"
        ) from exc
    if recomputed != row.identity_hash:
        raise GenerationIntegrityError(
            f"generation={row.generation_id} record={row.record_id!r}: identity "
            f"hash mismatch (stored {row.identity_hash}, recomputed {recomputed}); "
            "materialized row was tampered with"
        )
    return GenerationRecord(
        record_id=row.record_id,
        workspace_id=row.workspace_id,
        kernel_commit_id=row.kernel_commit_id,
        record_class=row.record_class,
        record_type=row.record_type,
        schema_version=row.schema_version,
        identity_hash=row.identity_hash,
        payload=payload,
        payload_json=row.payload_json,
        payload_byte_hash=row.payload_byte_hash,
        payload_length=row.payload_length,
    )


@dataclass(frozen=True)
class GenerationVerification:
    generation_id: str
    ok: bool
    problems: tuple[str, ...]
    checked_records: int
    checked_edges: int


async def verify_generation(
    session_factory: async_sessionmaker, generation_id: str
) -> GenerationVerification:
    """Recompute one generation's full content digest from its rows.

    This is the explicit deep-verification surface (activation already
    validated before exposure; bounded reads re-verify per-record
    identity). Catches post-activation tampering of materialized rows
    or the manifest row.
    """
    problems: list[str] = []
    async with session_factory() as session:
        row = await session.get(KernelGeneration, generation_id)
        if row is None:
            raise UnknownGenerationError(
                f"generation={generation_id}: no such generation"
            )
        records = (
            (
                await session.execute(
                    select(KernelGenerationRecord)
                    .where(KernelGenerationRecord.generation_id == generation_id)
                    .order_by(*_GEN_RECORD_ORDER)
                )
            )
            .scalars()
            .all()
        )
        edges = (
            (
                await session.execute(
                    select(KernelGenerationEdge)
                    .where(KernelGenerationEdge.generation_id == generation_id)
                    .order_by(*_GEN_EDGE_ORDER)
                )
            )
            .scalars()
            .all()
        )

    recomputed, class_counts, record_count, edge_count = _content_digest(
        workspace_id=row.workspace_id,
        cut=row.kernel_commit_id,
        record_entries=[_materialized_record_entry(r) for r in records],
        edge_entries=[_materialized_edge_entry(e) for e in edges],
    )
    if recomputed != row.content_digest:
        problems.append(
            f"content digest mismatch: manifest {row.content_digest}, "
            f"recomputed {recomputed}"
        )
    if (record_count, edge_count) != (row.record_count, row.edge_count):
        problems.append(
            f"counts mismatch: manifest {row.record_count}/{row.edge_count}, "
            f"found {record_count}/{edge_count}"
        )
    if class_counts != json.loads(row.record_class_counts_json):
        problems.append("record class counts mismatch")

    for record in records:
        try:
            payload = json.loads(record.payload_json)
            recomputed_identity = record_identity_hash(
                record_type=record.record_type,
                schema_version=record.schema_version,
                payload=to_json_ready(payload),
            )
        except Exception as exc:
            problems.append(
                f"record {record.record_id!r} payload rejected: {exc}"
            )
            continue
        if recomputed_identity != record.identity_hash:
            problems.append(
                f"record {record.record_id!r} identity hash mismatch"
            )

    return GenerationVerification(
        generation_id=generation_id,
        ok=not problems,
        problems=tuple(problems),
        checked_records=record_count,
        checked_edges=edge_count,
    )


_default_service: GenerationService | None = None


def default_generation_service() -> GenerationService:
    """Process-wide service bound to the production engine.

    Fails closed until ``verify_database_ready`` passes. Payload
    verification for snapshot resolution stays with the caller's
    ``resolve_snapshot`` payload store; this service only consumes
    already-pinned snapshots.
    """
    global _default_service
    if _default_service is None:
        from app.database import async_session_factory
        from app.db_migration import verify_database_ready

        _default_service = GenerationService(
            async_session_factory,
            readiness_check=verify_database_ready,
        )
    return _default_service
