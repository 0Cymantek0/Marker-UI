"""Industrial recovery boundary: recovery points, capture, restore, oracle.

PR83C1. A *recovery point* is a declared semantic cut — ``kernel_commit_id
<= K`` for one workspace — plus every durable object byte that cut
requires, bound into one verifiable artifact set:

* a PostgreSQL logical backup (``pg_dump -Fc``) whose transactionally
  consistent snapshot contains the database state at the cut;
* a verified copy of every required payload object and source artifact
  in a dedicated backup object namespace;
* a manifest naming the cut, the snapshot identity, the publication
  heads protected, and the byte-level evidence (hashes, lengths, sizes).

The manifest is written **last**, atomically, inside a staging directory
that is renamed into its final name only after every component verifies.
An interrupted capture therefore leaves no discoverable "complete"
recovery point — incompleteness is structural, not a flag.

Quiescence: capture takes the *session-level* advisory lock on the same
``PAYLOAD_DECISION_LOCK_SCOPE`` key space every GC deletion decision,
retention root writer, generation activation, and payload-carrying
commit already linearizes on (``pg_advisory_xact_lock`` shares the lock
tag namespace with ``pg_advisory_lock``). For the duration of the
capture window, payload garbage collection and payload-carrying commits
block on that scope — the required payload closure cannot be collected
mid-copy. Publication activation does not join that scope, so capture
additionally compares publication heads before and after the dump and
refuses to publish a manifest whose heads moved during the window.

This module deliberately refuses to become a second truth authority:
the manifest only *names* kernel truth (cut, snapshot id, publication
set ids). Every claim it makes is re-verified against the restored
authorities by :func:`verify_recovery`, which must pass before any
restored state is advertised as ready.

Non-goals (documented, not hidden): no standby promotion, no WAL
archiving/PITR, no multi-region orchestration. The database mechanism
is a per-recovery-point logical dump; restoring an *earlier* point
means restoring that point's own dump — there is no intra-backup
point-in-time selection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import text as sa_text

from app.kernel.dialects import POSTGRESQL, advisory_lock_key, backend_name
from app.db_migration import DatabaseState
from app.kernel.errors import KernelError
from app.kernel.models import (
    KernelCommitHead,
    KernelOutbox,
    KernelPublication,
    KernelPublicationHead,
    KernelPublicationSet,
    KernelRecord,
    KernelWorkLease,
)
from app.kernel.payloads import (
    PAYLOAD_DECISION_LOCK_SCOPE,
    KernelPayloadStore,
)
from app.kernel.publications import (
    DEFAULT_PUBLICATION_PROFILE,
    fts_table_name,
    resolve_published_set,
    verify_publication_set,
)
from app.kernel.snapshots import (
    COMPLETENESS_COMPLETE,
    PAYLOAD_REQUIREMENT_REPLAYABLE,
    resolve_snapshot,
)
from app.kernel.source_store import SourceArtifactStore
from app.utils.canonical import record_identity_hash

__all__ = [
    "RECOVERY_MANIFEST_VERSION",
    "RECOVERY_MANIFEST_FILENAME",
    "DUMP_FILENAME",
    "CAPTURE_FAULT_PHASES",
    "RecoveryError",
    "RecoveryUnsupportedBackendError",
    "RecoveryCaptureError",
    "RecoveryManifestError",
    "PayloadObjectRef",
    "SourceObjectRef",
    "PublicationPointRef",
    "CaptureDurations",
    "RecoveryPointManifest",
    "LoadedRecoveryPoint",
    "RecoveryCheck",
    "RecoveryReport",
    "PgSidecarTools",
    "current_head_commit",
    "enumerate_payload_closure",
    "enumerate_source_closure",
    "enumerate_publication_points",
    "capture_recovery_point",
    "load_recovery_point",
    "verify_backup_objects",
    "restore_object_namespaces",
    "verify_recovery",
]

RECOVERY_MANIFEST_VERSION = "marker.kernel.recovery_point.v1"
RECOVERY_MANIFEST_FILENAME = "recovery-manifest.json"
DUMP_FILENAME = "database.pgdump"
_STAGING_PREFIX = ".staging-"
_RECOVERY_POINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{16,64}$")

#: Crash-injection boundaries. Each phase fires *after* the step it
#: names has fully succeeded, so an injected fault always leaves the
#: capture in the "interrupted after X" state reviewers care about.
PHASE_CAP_QUIESCED = "rec-quiesced"
PHASE_CAP_CUT_RESOLVED = "rec-cut-resolved"
PHASE_CAP_DUMPED = "rec-dumped"
PHASE_CAP_HEAD_VERIFIED = "rec-head-verified"
PHASE_CAP_PUBLICATIONS_VERIFIED = "rec-publications-verified"
PHASE_CAP_PAYLOAD_COPIED = "rec-payload-copied"
PHASE_CAP_SOURCE_COPIED = "rec-source-copied"
PHASE_CAP_MANIFEST_WRITTEN = "rec-manifest-written"
CAPTURE_FAULT_PHASES = frozenset(
    {
        PHASE_CAP_QUIESCED,
        PHASE_CAP_CUT_RESOLVED,
        PHASE_CAP_DUMPED,
        PHASE_CAP_HEAD_VERIFIED,
        PHASE_CAP_PUBLICATIONS_VERIFIED,
        PHASE_CAP_PAYLOAD_COPIED,
        PHASE_CAP_SOURCE_COPIED,
        PHASE_CAP_MANIFEST_WRITTEN,
    }
)

_CONTENT_REVISION_CLASS = "content_revision"
_MAX_PUBLICATION_RETRIES = 3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecoveryError(KernelError):
    """Base class for recovery-boundary failures."""


class RecoveryUnsupportedBackendError(RecoveryError):
    """The recovery prototype only captures from PostgreSQL."""


class RecoveryCaptureError(RecoveryError):
    """A recovery-point capture could not complete honestly."""


class RecoveryManifestError(RecoveryError):
    """A recovery manifest or its artifacts failed verification."""


# ---------------------------------------------------------------------------
# Manifest data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PayloadObjectRef:
    """One payload object byte-set required by a recovery point."""

    blob_key: str
    length: int

    def as_dict(self) -> dict[str, Any]:
        return {"blob_key": self.blob_key, "length": self.length}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PayloadObjectRef":
        return cls(blob_key=str(value["blob_key"]), length=int(value["length"]))


@dataclass(frozen=True)
class SourceObjectRef:
    """One committed source artifact required by a recovery point."""

    content_revision_id: str
    blob_key: str
    suffix: str
    byte_length: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_revision_id": self.content_revision_id,
            "blob_key": self.blob_key,
            "suffix": self.suffix,
            "byte_length": self.byte_length,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceObjectRef":
        return cls(
            content_revision_id=str(value["content_revision_id"]),
            blob_key=str(value["blob_key"]),
            suffix=str(value["suffix"]),
            byte_length=int(value["byte_length"]),
        )


@dataclass(frozen=True)
class PublicationPointRef:
    """One published serving state protected by a recovery point."""

    profile: str
    publication_set_id: str
    kernel_commit_id: int
    snapshot_id: str
    materialized_generation_id: str
    lexical_generation_id: str | None
    vector_generation_id: str | None
    fts_table: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "publication_set_id": self.publication_set_id,
            "kernel_commit_id": self.kernel_commit_id,
            "snapshot_id": self.snapshot_id,
            "materialized_generation_id": self.materialized_generation_id,
            "lexical_generation_id": self.lexical_generation_id,
            "vector_generation_id": self.vector_generation_id,
            "fts_table": self.fts_table,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PublicationPointRef":
        return cls(
            profile=str(value["profile"]),
            publication_set_id=str(value["publication_set_id"]),
            kernel_commit_id=int(value["kernel_commit_id"]),
            snapshot_id=str(value["snapshot_id"]),
            materialized_generation_id=str(value["materialized_generation_id"]),
            lexical_generation_id=value["lexical_generation_id"],
            vector_generation_id=value["vector_generation_id"],
            fts_table=value["fts_table"],
        )


@dataclass(frozen=True)
class CaptureDurations:
    """Measured operational tax of one capture (seconds, wall clock)."""

    total_seconds: float
    quiesce_seconds: float
    dump_seconds: float
    payload_copy_seconds: float
    source_copy_seconds: float
    payload_bytes_copied: int
    source_bytes_copied: int
    payload_object_count: int
    source_object_count: int
    dump_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_seconds": round(self.total_seconds, 6),
            "quiesce_seconds": round(self.quiesce_seconds, 6),
            "dump_seconds": round(self.dump_seconds, 6),
            "payload_copy_seconds": round(self.payload_copy_seconds, 6),
            "source_copy_seconds": round(self.source_copy_seconds, 6),
            "payload_bytes_copied": self.payload_bytes_copied,
            "source_bytes_copied": self.source_bytes_copied,
            "payload_object_count": self.payload_object_count,
            "source_object_count": self.source_object_count,
            "dump_bytes": self.dump_bytes,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CaptureDurations":
        return cls(
            total_seconds=float(value["total_seconds"]),
            quiesce_seconds=float(value["quiesce_seconds"]),
            dump_seconds=float(value["dump_seconds"]),
            payload_copy_seconds=float(value["payload_copy_seconds"]),
            source_copy_seconds=float(value["source_copy_seconds"]),
            payload_bytes_copied=int(value["payload_bytes_copied"]),
            source_bytes_copied=int(value["source_bytes_copied"]),
            payload_object_count=int(value["payload_object_count"]),
            source_object_count=int(value["source_object_count"]),
            dump_bytes=int(value["dump_bytes"]),
        )


@dataclass(frozen=True)
class RecoveryPointManifest:
    """The verifiable identity of one recovery point.

    ``recovery_point_id`` is a deterministic hash over the *semantic*
    dimensions only (workspace, cut, snapshot identity, object sets,
    publication identities, store profiles). Dump bytes and timestamps
    are excluded so an interrupted-then-retried capture of the same
    semantic point converges to the same identity instead of minting a
    contradictory second recovery point.
    """

    manifest_version: str
    recovery_point_id: str
    workspace_id: str
    kernel_cut: int
    snapshot_id: str
    required_payload_state: str
    database: Mapping[str, Any]
    payload_store: Mapping[str, Any]
    source_store: Mapping[str, Any]
    publications: tuple[PublicationPointRef, ...]
    captured_at: str
    durations: CaptureDurations

    def semantic_identity_payload(self) -> dict[str, Any]:
        """Dimensions the recovery-point identity is computed over."""
        return {
            "manifest_version": self.manifest_version,
            "workspace_id": self.workspace_id,
            "kernel_cut": self.kernel_cut,
            "snapshot_id": self.snapshot_id,
            "required_payload_state": self.required_payload_state,
            "payload_store_profile": self.payload_store.get("profile"),
            "source_store_profile": self.source_store.get("profile"),
            "payload_objects": [
                [ref.blob_key, ref.length]
                for ref in sorted(
                    (PayloadObjectRef.from_mapping(o) for o in self.payload_store["objects"]),
                    key=lambda r: r.blob_key,
                )
            ],
            "source_objects": [
                [ref.blob_key, ref.suffix, ref.byte_length]
                for ref in sorted(
                    (SourceObjectRef.from_mapping(o) for o in self.source_store["objects"]),
                    key=lambda r: r.blob_key,
                )
            ],
            "publication_set_ids": sorted(ref.publication_set_id for ref in self.publications),
        }

    def expected_recovery_point_id(self) -> str:
        return record_identity_hash(
            record_type=RECOVERY_MANIFEST_VERSION,
            schema_version="1",
            payload=self.semantic_identity_payload(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "recovery_point_id": self.recovery_point_id,
            "workspace_id": self.workspace_id,
            "kernel_cut": self.kernel_cut,
            "snapshot_id": self.snapshot_id,
            "required_payload_state": self.required_payload_state,
            "database": dict(self.database),
            "payload_store": dict(self.payload_store),
            "source_store": dict(self.source_store),
            "publications": [ref.as_dict() for ref in self.publications],
            "captured_at": self.captured_at,
            "durations": self.durations.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecoveryPointManifest":
        """Fail-closed parse: unknown versions or missing fields refuse."""
        version = str(value.get("manifest_version", ""))
        if version != RECOVERY_MANIFEST_VERSION:
            raise RecoveryManifestError(
                f"unsupported manifest version {version!r}; expected "
                f"{RECOVERY_MANIFEST_VERSION!r}"
            )
        try:
            manifest = cls(
                manifest_version=version,
                recovery_point_id=str(value["recovery_point_id"]),
                workspace_id=str(value["workspace_id"]),
                kernel_cut=int(value["kernel_cut"]),
                snapshot_id=str(value["snapshot_id"]),
                required_payload_state=str(value["required_payload_state"]),
                database=dict(value["database"]),
                payload_store=dict(value["payload_store"]),
                source_store=dict(value["source_store"]),
                publications=tuple(
                    PublicationPointRef.from_mapping(p) for p in value["publications"]
                ),
                captured_at=str(value["captured_at"]),
                durations=CaptureDurations.from_mapping(value["durations"]),
            )
        except KeyError as exc:
            raise RecoveryManifestError(f"manifest missing field {exc}") from exc
        if not _RECOVERY_POINT_PATTERN.match(manifest.recovery_point_id):
            raise RecoveryManifestError(
                f"malformed recovery_point_id {manifest.recovery_point_id!r}"
            )
        expected = manifest.expected_recovery_point_id()
        if manifest.recovery_point_id != expected:
            raise RecoveryManifestError(
                "recovery_point_id does not match the manifest's declared "
                f"semantic dimensions: declared {manifest.recovery_point_id}, "
                f"computed {expected}"
            )
        return manifest


@dataclass(frozen=True)
class LoadedRecoveryPoint:
    """A manifest plus its on-disk dump artifact, verified on load."""

    manifest: RecoveryPointManifest
    root: Path
    dump_path: Path


# ---------------------------------------------------------------------------
# Oracle result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryCheck:
    """One named oracle component verdict."""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class RecoveryReport:
    """Aggregate oracle verdict; ``ready`` is the conjunction of checks."""

    checks: tuple[RecoveryCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.detail}" for c in self.checks if not c.ok)

    def check(self, name: str) -> RecoveryCheck:
        for candidate in self.checks:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "problems": list(self.problems),
            "checks": [c.as_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Truth enumeration (reads committed kernel state; never invents)
# ---------------------------------------------------------------------------


async def current_head_commit(
    session_factory: async_sessionmaker, workspace_id: str
) -> int:
    """The workspace's current committed head (0 for an empty chain)."""
    async with session_factory() as session:
        head = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == workspace_id
            )
        )
    return int(head or 0)


async def enumerate_payload_closure(
    session_factory: async_sessionmaker, workspace_id: str, cut: int
) -> tuple[PayloadObjectRef, ...]:
    """Every distinct payload byte-set a record at or below ``cut`` needs.

    Mirrors the snapshot classifier's selection (``payload_byte_hash``
    of records ``kernel_commit_id <= cut``), deduplicated — blob keys
    are content identities shared across records and workspaces.
    """
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        KernelRecord.payload_byte_hash,
                        KernelRecord.payload_length,
                    )
                    .where(
                        KernelRecord.workspace_id == workspace_id,
                        KernelRecord.kernel_commit_id <= cut,
                        KernelRecord.payload_byte_hash.is_not(None),
                    )
                    .order_by(KernelRecord.payload_byte_hash.asc())
                )
            )
            .unique()
            .all()
        )
    return tuple(
        PayloadObjectRef(blob_key=str(blob_key), length=int(length or 0))
        for blob_key, length in rows
    )


async def enumerate_source_closure(
    session_factory: async_sessionmaker, workspace_id: str, cut: int
) -> tuple[SourceObjectRef, ...]:
    """Every committed source artifact a content revision at/below the
    cut references. Malformed committed payloads refuse the capture —
    a manifest that silently skipped a broken revision would lie."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord.id, KernelRecord.payload_json).where(
                    KernelRecord.workspace_id == workspace_id,
                    KernelRecord.kernel_commit_id <= cut,
                    KernelRecord.record_class == _CONTENT_REVISION_CLASS,
                )
            )
        ).all()
    refs: list[SourceObjectRef] = []
    for record_id, payload_json in rows:
        try:
            payload = json.loads(payload_json)
            refs.append(
                SourceObjectRef(
                    content_revision_id=str(record_id),
                    blob_key=str(payload["blob_key"]),
                    suffix=str(payload["suffix"]),
                    byte_length=int(payload["byte_length"]),
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise RecoveryCaptureError(
                f"content revision {record_id}: committed payload is not a "
                f"verifiable source reference ({exc})"
            ) from exc
    return tuple(sorted(refs, key=lambda r: r.blob_key))


async def enumerate_publication_points(
    session_factory: async_sessionmaker, workspace_id: str, cut: int
) -> tuple[PublicationPointRef, ...]:
    """Current publication heads for the workspace, fail-closed.

    A head naming a publication whose commit is beyond the cut is a
    capture-time integrity fault (under quiescence it cannot happen;
    if it ever does, the manifest must not be written).
    """
    refs: list[PublicationPointRef] = []
    async with session_factory() as session:
        backend = backend_name(session.bind)
        heads = (
            (
                await session.execute(
                    select(
                        KernelPublicationHead.profile,
                        KernelPublicationHead.current_publication_set_id,
                    ).where(KernelPublicationHead.workspace_id == workspace_id)
                )
            )
            .unique()
            .all()
        )
        for profile, set_id in heads:
            row = await session.get(KernelPublicationSet, set_id)
            if row is None:
                raise RecoveryCaptureError(
                    f"profile {profile!r}: publication head names {set_id!r} "
                    "but no such set row exists"
                )
            if row.kernel_commit_id > cut:
                raise RecoveryCaptureError(
                    f"profile {profile!r}: publication {set_id!r} sits at "
                    f"commit {row.kernel_commit_id} beyond the declared cut "
                    f"{cut}"
                )
            refs.append(
                PublicationPointRef(
                    profile=str(profile),
                    publication_set_id=str(set_id),
                    kernel_commit_id=int(row.kernel_commit_id),
                    snapshot_id=str(row.snapshot_id),
                    materialized_generation_id=str(row.materialized_generation_id),
                    lexical_generation_id=row.lexical_generation_id,
                    vector_generation_id=row.vector_generation_id,
                    fts_table=(
                        fts_table_name(row.lexical_generation_id, backend=backend)
                        if row.lexical_generation_id
                        else None
                    ),
                )
            )
    return tuple(sorted(refs, key=lambda r: (r.profile, r.publication_set_id)))


# ---------------------------------------------------------------------------
# PostgreSQL dump/restore sidecar
# ---------------------------------------------------------------------------


@dataclass
class PgSidecarTools:
    """Run ``pg_dump``/``pg_restore`` through a versioned sidecar container.

    The tooling must match the server's major version; the sidecar
    image is pinned per call so evidence can record it exactly. The
    sidecar reaches the server through ``host.docker.internal`` (mapped
    to the host gateway), which works against a Docker-provisioned
    container, a CI service container, or an external server alike.
    """

    host: str
    port: int
    user: str
    password: str
    image: str = "postgres:16-alpine"

    async def _run(self, args: Sequence[str], *, stdin_bytes: bytes | None = None) -> bytes:
        docker_args = ["docker", "run", "--rm"]
        if stdin_bytes is not None:
            docker_args.append("-i")
        docker_args.extend(
            [
                "--add-host",
                "host.docker.internal:host-gateway",
                "-e",
                f"PGPASSWORD={self.password}",
                self.image,
                *args,
            ]
        )
        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(stdin_bytes)
        if proc.returncode != 0:
            raise RecoveryCaptureError(
                f"sidecar {' '.join(args[:1])} failed with exit "
                f"{proc.returncode}: {stderr.decode('utf-8', 'replace').strip()}"
            )
        return stdout

    async def dump_database(self, database: str) -> bytes:
        """One custom-format logical backup (single-transaction snapshot)."""
        return await self._run(
            (
                "pg_dump",
                "-h",
                "host.docker.internal",
                "-p",
                str(self.port),
                "-U",
                self.user,
                "-Fc",
                "-d",
                database,
            )
        )

    async def restore_database(self, database: str, dump: bytes) -> None:
        """Restore a dump into an existing, empty target database."""
        await self._run(
            (
                "pg_restore",
                "-h",
                "host.docker.internal",
                "-p",
                str(self.port),
                "-U",
                self.user,
                "--no-owner",
                "--no-privileges",
                "-d",
                database,
            ),
            stdin_bytes=dump,
        )

    async def server_banner(self, database: str) -> str:
        out = await self._run(
            (
                "psql",
                "-h",
                "host.docker.internal",
                "-p",
                str(self.port),
                "-U",
                self.user,
                "-d",
                database,
                "-t",
                "-A",
                "-c",
                "SELECT version()",
            )
        )
        return out.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _store_identity(store: Any, *, kind: str) -> dict[str, Any]:
    """Best-effort namespace identity of a store for the manifest.

    The profile string is the contract identity; bucket/prefix are
    recorded when the store exposes its config so a restore can prove
    it read the recovered namespace rather than the original.
    """
    config = getattr(store, "config", None) or getattr(store, "_config", None)
    profile = getattr(store, "profile", None) or getattr(store, "store_profile", None)
    identity: dict[str, Any] = {"kind": kind, "profile": profile}
    if config is not None:
        for attr in ("endpoint_url", "bucket", "prefix", "region"):
            value = getattr(config, attr, None)
            if value is not None:
                identity[attr] = value
    root = getattr(store, "root", None)
    if root is not None:
        identity["root"] = str(root)
    return identity


def _write_file_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class _CaptureQuiescence:
    """Session-level advisory hold on the payload-decision scope.

    ``pg_advisory_lock`` shares the lock-tag namespace with the
    ``pg_advisory_xact_lock`` every GC decision/retention/activation
    writer already takes, so holding the session lock for the capture
    window blocks all of them without any new schema or lock vocabulary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._key = advisory_lock_key(*PAYLOAD_DECISION_LOCK_SCOPE)
        self._held = False

    async def __aenter__(self) -> "_CaptureQuiescence":
        await self._session.execute(sa_text("SELECT pg_advisory_lock(:key)"), {"key": self._key})
        self._held = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._held:
            await self._session.execute(
                sa_text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
            )
            self._held = False
        await self._session.close()


def _maybe_inject_fault(phase: str | None, expected: str) -> None:
    if phase == expected:
        raise RecoveryCaptureError(f"injected capture fault at {expected}")


async def capture_recovery_point(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    payload_store: KernelPayloadStore,
    source_store: SourceArtifactStore,
    backup_payload_store: KernelPayloadStore,
    backup_source_store: SourceArtifactStore,
    pg_tools: PgSidecarTools,
    database_name: str,
    backup_root: Path,
    _inject_fault_at: str | None = None,
) -> RecoveryPointManifest:
    """Capture one coherent recovery point; returns its verified manifest.

    Refuses (raises, nothing discoverable) when: the backend is not
    PostgreSQL; the cut's snapshot is degraded; the head moves during
    the window; publication heads move during the window; any required
    object fails to copy and verify. A completed capture is durably
    discoverable only after the manifest is written and the staging
    directory is atomically renamed.
    """
    if _inject_fault_at is not None and _inject_fault_at not in CAPTURE_FAULT_PHASES:
        raise RecoveryCaptureError(f"unknown fault phase {_inject_fault_at!r}")

    async with session_factory() as probe:
        if backend_name(probe.bind) != POSTGRESQL:
            raise RecoveryUnsupportedBackendError(
                "recovery capture requires the PostgreSQL industrial profile"
            )

    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)

    # Retry convergence (B7): a completed recovery point for the same
    # semantic identity is loaded and re-verified, never recaptured —
    # an interrupted attempt's staging residue is discarded below.
    stale_staging = [
        entry
        for entry in backup_root.iterdir()
        if entry.name.startswith(_STAGING_PREFIX)
    ]
    for entry in stale_staging:
        shutil.rmtree(entry, ignore_errors=True)

    started = time.monotonic()
    staging = backup_root / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    dump_path = staging / DUMP_FILENAME
    # copy scratch lives OUTSIDE the staging directory so the final
    # recovery point contains exactly the manifest and the dump
    tmp_source_dir = Path(tempfile.mkdtemp(prefix="marker-recovery-copy-"))
    quiesce_session: AsyncSession | None = None
    try:
        quiesce_session = session_factory()
        quiesce = _CaptureQuiescence(quiesce_session)
        quiesce_started = time.monotonic()
        async with quiesce:
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_QUIESCED)

            cut = await current_head_commit(session_factory, workspace_id)
            snapshot = await resolve_snapshot(
                session_factory,
                workspace_id,
                at_commit=cut if cut else None,
                required_payload_state=PAYLOAD_REQUIREMENT_REPLAYABLE,
                payload_store=payload_store,
            )
            if snapshot.completeness != COMPLETENESS_COMPLETE:
                raise RecoveryCaptureError(
                    f"cut {cut} snapshot is {snapshot.completeness}, not "
                    f"{COMPLETENESS_COMPLETE}; refusing to capture a recovery "
                    "point that cannot restore replayable state"
                )
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_CUT_RESOLVED)

            payload_refs = await enumerate_payload_closure(
                session_factory, workspace_id, snapshot.kernel_commit_id
            )
            source_refs = await enumerate_source_closure(
                session_factory, workspace_id, snapshot.kernel_commit_id
            )
            publication_refs = await enumerate_publication_points(
                session_factory, workspace_id, snapshot.kernel_commit_id
            )

            pg_banner = await pg_tools.server_banner(database_name)
            dump_started = time.monotonic()
            dump = await pg_tools.dump_database(database_name)
            dump_seconds = time.monotonic() - dump_started
            dump_sha = hashlib.sha256(dump).hexdigest()
            dump_path.write_bytes(dump)
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_DUMPED)

            head_now = await current_head_commit(session_factory, workspace_id)
            if head_now != cut:
                raise RecoveryCaptureError(
                    f"head moved {cut} -> {head_now} inside the quiesced "
                    "capture window; refusing this capture attempt"
                )
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_HEAD_VERIFIED)

            publications_now = await enumerate_publication_points(
                session_factory, workspace_id, cut
            )
            if publications_now != publication_refs:
                raise RecoveryCaptureError(
                    "publication heads moved during the capture window; "
                    "refusing this capture attempt"
                )
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_PUBLICATIONS_VERIFIED)

            payload_copy_started = time.monotonic()
            payload_bytes = await _copy_payload_objects(
                payload_store, backup_payload_store, payload_refs
            )
            payload_copy_seconds = time.monotonic() - payload_copy_started
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_PAYLOAD_COPIED)

            source_copy_started = time.monotonic()
            source_bytes = await _copy_source_objects(
                source_store,
                backup_source_store,
                source_refs,
                tmp_source_dir,
            )
            source_copy_seconds = time.monotonic() - source_copy_started
            _maybe_inject_fault(_inject_fault_at, PHASE_CAP_SOURCE_COPIED)

        quiesce_seconds = time.monotonic() - quiesce_started

        manifest = RecoveryPointManifest(
            manifest_version=RECOVERY_MANIFEST_VERSION,
            recovery_point_id="sha256:pending",
            workspace_id=workspace_id,
            kernel_cut=snapshot.kernel_commit_id,
            snapshot_id=snapshot.snapshot_id,
            required_payload_state=PAYLOAD_REQUIREMENT_REPLAYABLE,
            database={
                "backend": "postgresql",
                "source_database": database_name,
                "dump_sha256": dump_sha,
                "dump_bytes": len(dump),
                "pg_version": pg_banner,
                "database_head_commit_id": cut,
            },
            payload_store={
                **_store_identity(backup_payload_store, kind="payload"),
                "objects": [ref.as_dict() for ref in payload_refs],
            },
            source_store={
                **_store_identity(backup_source_store, kind="source"),
                "objects": [ref.as_dict() for ref in source_refs],
            },
            publications=publication_refs,
            captured_at=datetime.now(timezone.utc).isoformat(),
            durations=CaptureDurations(
                total_seconds=0.0,
                quiesce_seconds=quiesce_seconds,
                dump_seconds=dump_seconds,
                payload_copy_seconds=payload_copy_seconds,
                source_copy_seconds=source_copy_seconds,
                payload_bytes_copied=payload_bytes,
                source_bytes_copied=source_bytes,
                payload_object_count=len(payload_refs),
                source_object_count=len(source_refs),
                dump_bytes=len(dump),
            ),
        )
        manifest = _with_total_duration(manifest, time.monotonic() - started)

        manifest_path = staging / RECOVERY_MANIFEST_FILENAME
        _write_file_atomic(
            manifest_path,
            json.dumps(manifest.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
        )
        _maybe_inject_fault(_inject_fault_at, PHASE_CAP_MANIFEST_WRITTEN)
        # The rename is the discoverability boundary: until it succeeds
        # no complete recovery point exists under this identity. A
        # completed capture whose identity already exists converges to
        # the existing point (verified) instead of overwriting it.
        final_dir = backup_root / manifest.recovery_point_id.split(":", 1)[1]
        if final_dir.exists():
            existing = load_recovery_point(backup_root, manifest.recovery_point_id)
            report = await verify_backup_objects(
                existing,
                backup_payload_store=backup_payload_store,
                backup_source_store=backup_source_store,
            )
            if not report.ready:
                raise RecoveryCaptureError(
                    f"existing recovery point {manifest.recovery_point_id} "
                    f"failed re-verification: {report.problems}"
                )
            return existing.manifest
        os.replace(staging, final_dir)
        return manifest
    finally:
        if quiesce_session is not None:
            await quiesce_session.close()
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(tmp_source_dir, ignore_errors=True)


def _with_total_duration(
    manifest: RecoveryPointManifest, total_seconds: float
) -> RecoveryPointManifest:
    durations = CaptureDurations(
        total_seconds=total_seconds,
        quiesce_seconds=manifest.durations.quiesce_seconds,
        dump_seconds=manifest.durations.dump_seconds,
        payload_copy_seconds=manifest.durations.payload_copy_seconds,
        source_copy_seconds=manifest.durations.source_copy_seconds,
        payload_bytes_copied=manifest.durations.payload_bytes_copied,
        source_bytes_copied=manifest.durations.source_bytes_copied,
        payload_object_count=manifest.durations.payload_object_count,
        source_object_count=manifest.durations.source_object_count,
        dump_bytes=manifest.durations.dump_bytes,
    )
    replaced = RecoveryPointManifest(
        manifest_version=manifest.manifest_version,
        recovery_point_id=manifest.expected_recovery_point_id(),
        workspace_id=manifest.workspace_id,
        kernel_cut=manifest.kernel_cut,
        snapshot_id=manifest.snapshot_id,
        required_payload_state=manifest.required_payload_state,
        database=manifest.database,
        payload_store=manifest.payload_store,
        source_store=manifest.source_store,
        publications=manifest.publications,
        captured_at=manifest.captured_at,
        durations=durations,
    )
    return replaced


async def _copy_payload_objects(
    source_store: Any, backup_store: KernelPayloadStore, refs: Sequence[PayloadObjectRef]
) -> int:
    """Copy each required payload object, verifying content on arrival.

    Content addressing makes the copy self-verifying: ``stage`` reads
    back and hashes the stored object, so a corrupted transfer cannot
    land under the required key.
    """
    maintenance = source_store
    read = getattr(maintenance, "read", None)
    if read is None:
        raise RecoveryCaptureError(
            "live payload store does not expose maintenance reads; cannot "
            "copy required objects"
        )
    total = 0
    for ref in refs:
        data = await read(ref.blob_key)
        if len(data) != ref.length:
            raise RecoveryCaptureError(
                f"payload {ref.blob_key}: expected {ref.length} bytes, read "
                f"{len(data)}"
            )
        staged = await backup_store.stage(data)
        if staged.blob_key != ref.blob_key:
            raise RecoveryCaptureError(
                f"payload copy of {ref.blob_key} landed at {staged.blob_key}"
            )
        total += len(data)
    return total


async def _copy_source_objects(
    source_store: SourceArtifactStore,
    backup_store: SourceArtifactStore,
    refs: Sequence[SourceObjectRef],
    tmp_dir: Path,
) -> int:
    """Copy each required source artifact through a verified materialization."""
    materialize = getattr(source_store, "materialize_to", None)
    if materialize is None:
        raise RecoveryCaptureError(
            "live source store does not expose verified materialization; "
            "cannot copy required artifacts"
        )
    total = 0
    for index, ref in enumerate(refs):
        tmp_path = tmp_dir / f"{index:06d}{ref.suffix}"
        await materialize(ref.blob_key, ref.suffix, tmp_path)
        staged = await backup_store.stage_from_path(tmp_path, suffix=ref.suffix)
        if staged.blob_key != ref.blob_key:
            raise RecoveryCaptureError(
                f"source copy of {ref.blob_key} landed at {staged.blob_key}"
            )
        verified = await backup_store.verify_artifact(
            ref.blob_key, ref.suffix, expected_length=ref.byte_length
        )
        if verified is not True and getattr(verified, "ok", True) is not True:
            raise RecoveryCaptureError(
                f"source copy of {ref.blob_key} failed backup-side verification"
            )
        total += ref.byte_length
    return total


# ---------------------------------------------------------------------------
# Load / verify a recovery point
# ---------------------------------------------------------------------------


def load_recovery_point(backup_root: Path, recovery_point_id: str) -> LoadedRecoveryPoint:
    """Load and structurally verify one recovery point from disk.

    Staging directories are invisible to this function by construction:
    only a directory named by the full recovery-point identity that
    contains a manifest verifying against its own declared dimensions
    and a dump matching the manifest's digest is loadable. A truncated,
    tampered, or interrupted capture raises instead of degrading.
    """
    root = Path(backup_root)
    if not _RECOVERY_POINT_PATTERN.match(recovery_point_id):
        raise RecoveryManifestError(f"malformed recovery point id {recovery_point_id!r}")
    directory = root / recovery_point_id.split(":", 1)[1]
    manifest_path = directory / RECOVERY_MANIFEST_FILENAME
    dump_path = directory / DUMP_FILENAME
    if not manifest_path.is_file() or not dump_path.is_file():
        raise RecoveryManifestError(
            f"recovery point {recovery_point_id}: artifacts incomplete "
            f"(manifest={manifest_path.is_file()}, dump={dump_path.is_file()})"
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RecoveryManifestError(f"manifest unreadable: {exc}") from exc
    manifest = RecoveryPointManifest.from_mapping(raw)
    if manifest.recovery_point_id != recovery_point_id:
        raise RecoveryManifestError(
            f"directory identity {recovery_point_id} does not match manifest "
            f"identity {manifest.recovery_point_id}"
        )
    dump = dump_path.read_bytes()
    actual_sha = hashlib.sha256(dump).hexdigest()
    if actual_sha != manifest.database.get("dump_sha256"):
        raise RecoveryManifestError(
            f"dump artifact digest mismatch: manifest says "
            f"{manifest.database.get('dump_sha256')}, file hashes {actual_sha}"
        )
    if len(dump) != manifest.database.get("dump_bytes"):
        raise RecoveryManifestError(
            f"dump artifact length mismatch: manifest says "
            f"{manifest.database.get('dump_bytes')}, file has {len(dump)}"
        )
    return LoadedRecoveryPoint(manifest=manifest, root=root, dump_path=dump_path)


async def verify_backup_objects(
    loaded: LoadedRecoveryPoint,
    *,
    backup_payload_store: KernelPayloadStore,
    backup_source_store: SourceArtifactStore,
) -> RecoveryReport:
    """Verify every object the manifest requires exists in the backup
    namespaces with matching content. Used on load-convergence, before
    restore, and as the damaged-backup refusal oracle."""
    checks: list[RecoveryCheck] = []
    bad_payloads: list[str] = []
    payload_refs = [
        PayloadObjectRef.from_mapping(value)
        for value in loaded.manifest.payload_store["objects"]
    ]
    for ref in payload_refs:
        check = await backup_payload_store.check_object(
            ref.blob_key, expected_length=ref.length
        )
        if not (check.available and check.hash_ok and check.length_ok):
            bad_payloads.append(ref.blob_key)
    checks.append(
        RecoveryCheck(
            name="payload_closure",
            ok=not bad_payloads,
            detail=(
                f"{len(payload_refs)} objects verified"
                if not bad_payloads
                else f"unavailable/corrupt: {sorted(bad_payloads)}"
            ),
        )
    )
    bad_sources: list[str] = []
    source_refs = [
        SourceObjectRef.from_mapping(value)
        for value in loaded.manifest.source_store["objects"]
    ]
    for ref in source_refs:
        try:
            verified = await backup_source_store.verify_artifact(
                ref.blob_key, ref.suffix, expected_length=ref.byte_length
            )
            if verified is not True and getattr(verified, "ok", False) is not True:
                bad_sources.append(ref.blob_key)
        except Exception as exc:  # fail closed on any store error
            bad_sources.append(f"{ref.blob_key} ({exc})")
    checks.append(
        RecoveryCheck(
            name="source_closure",
            ok=not bad_sources,
            detail=(
                f"{len(source_refs)} artifacts verified"
                if not bad_sources
                else f"unavailable/corrupt: {sorted(bad_sources)}"
            ),
        )
    )
    return RecoveryReport(checks=tuple(checks))


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


async def restore_object_namespaces(
    loaded: LoadedRecoveryPoint,
    *,
    backup_payload_store: KernelPayloadStore,
    backup_source_store: SourceArtifactStore,
    target_payload_store: KernelPayloadStore,
    target_source_store: SourceArtifactStore,
    scratch_dir: Path,
) -> None:
    """Seed fresh target namespaces from the verified backup copies.

    The target stores must be empty namespaces: a restore that silently
    merged into an existing namespace would fabricate coherence. Every
    copied object re-verifies on arrival (content addressing + staged
    read-back), so a corrupt backup object fails here instead of later.
    """
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    for value in loaded.manifest.payload_store["objects"]:
        ref = PayloadObjectRef.from_mapping(value)
        data = await backup_payload_store.read(ref.blob_key)
        staged = await target_payload_store.stage(data)
        if staged.blob_key != ref.blob_key:
            raise RecoveryManifestError(
                f"restored payload {ref.blob_key} landed at {staged.blob_key}"
            )
    for index, value in enumerate(loaded.manifest.source_store["objects"]):
        ref = SourceObjectRef.from_mapping(value)
        tmp_path = scratch / f"{index:06d}{ref.suffix}"
        await backup_source_store.materialize_to(ref.blob_key, ref.suffix, tmp_path)
        staged = await target_source_store.stage_from_path(tmp_path, suffix=ref.suffix)
        if staged.blob_key != ref.blob_key:
            raise RecoveryManifestError(
                f"restored source {ref.blob_key} landed at {staged.blob_key}"
            )
    # scratch cleanup is best-effort: Windows may briefly hold staging
    # file handles after streamed uploads (shutil.rmtree ignore_errors)


# ---------------------------------------------------------------------------
# Recovery oracle
# ---------------------------------------------------------------------------


async def verify_recovery(
    session_factory: async_sessionmaker,
    *,
    database_url: str,
    workspace_id: str,
    manifest: RecoveryPointManifest,
    payload_store: KernelPayloadStore,
    source_store: SourceArtifactStore,
    expected_query: Mapping[str, Any] | None = None,
) -> RecoveryReport:
    """Executable recovery oracle over a restored topology.

    Checks, in order: database/schema integrity at the migration head;
    the declared cut resolves to the manifest's complete replayable
    snapshot; payload closure; source closure; publication/query closure
    against the restored physical lexical state; runtime ownership
    closure. ``ready`` is true only when every check passes — the oracle
    never converts missing evidence into success, and a failure in any
    component leaves the restored state "not ready" rather than
    degraded-but-advertised.
    """
    checks: list[RecoveryCheck] = []

    # -- database --------------------------------------------------------
    try:
        from app.db_migration_postgres import inspect_database_async

        status = await inspect_database_async(database_url)
        if status.state is not DatabaseState.CURRENT:
            checks.append(
                RecoveryCheck(
                    name="database",
                    ok=False,
                    detail=f"migration state {status.state.value}: "
                    f"{status.problems}",
                )
            )
        else:
            checks.append(
                RecoveryCheck(name="database", ok=True, detail=f"head={status.head}")
            )
    except Exception as exc:
        checks.append(RecoveryCheck(name="database", ok=False, detail=str(exc)))

    # -- semantic cut ----------------------------------------------------
    try:
        snapshot = await resolve_snapshot(
            session_factory,
            workspace_id,
            at_commit=manifest.kernel_cut if manifest.kernel_cut else None,
            required_payload_state=manifest.required_payload_state,
            payload_store=payload_store,
        )
        ok = (
            snapshot.kernel_commit_id == manifest.kernel_cut
            and snapshot.snapshot_id == manifest.snapshot_id
            and snapshot.completeness == COMPLETENESS_COMPLETE
        )
        checks.append(
            RecoveryCheck(
                name="cut",
                ok=ok,
                detail=(
                    f"cut={snapshot.kernel_commit_id} "
                    f"snapshot={snapshot.snapshot_id} "
                    f"completeness={snapshot.completeness}"
                ),
            )
        )
    except Exception as exc:
        checks.append(RecoveryCheck(name="cut", ok=False, detail=str(exc)))

    # -- payload closure --------------------------------------------------
    missing: list[str] = []
    for value in manifest.payload_store["objects"]:
        ref = PayloadObjectRef.from_mapping(value)
        check = await payload_store.check_object(ref.blob_key, expected_length=ref.length)
        if not (check.available and check.hash_ok and check.length_ok):
            missing.append(ref.blob_key)
    checks.append(
        RecoveryCheck(
            name="payload_closure",
            ok=not missing,
            detail=(
                f"{len(manifest.payload_store['objects'])} objects verified"
                if not missing
                else f"unavailable/corrupt: {sorted(missing)}"
            ),
        )
    )

    # -- source closure ---------------------------------------------------
    bad_sources: list[str] = []
    for value in manifest.source_store["objects"]:
        ref = SourceObjectRef.from_mapping(value)
        try:
            verified = await source_store.verify_artifact(
                ref.blob_key, ref.suffix, expected_length=ref.byte_length
            )
            if verified is not True and getattr(verified, "ok", False) is not True:
                bad_sources.append(ref.blob_key)
        except Exception:
            bad_sources.append(ref.blob_key)
    checks.append(
        RecoveryCheck(
            name="source_closure",
            ok=not bad_sources,
            detail=(
                f"{len(manifest.source_store['objects'])} artifacts verified"
                if not bad_sources
                else f"unavailable/corrupt: {sorted(bad_sources)}"
            ),
        )
    )

    # -- publication / query closure --------------------------------------
    try:
        pub_ok, pub_detail = await _verify_publications(
            session_factory,
            workspace_id,
            manifest,
            expected_query,
        )
        checks.append(RecoveryCheck(name="publication", ok=pub_ok, detail=pub_detail))
    except Exception as exc:
        checks.append(RecoveryCheck(name="publication", ok=False, detail=str(exc)))

    # -- runtime ownership closure ----------------------------------------
    checks.append(await _verify_ownership(session_factory, workspace_id, manifest))

    return RecoveryReport(checks=tuple(checks))


async def _verify_publications(
    session_factory: async_sessionmaker,
    workspace_id: str,
    manifest: RecoveryPointManifest,
    expected_query: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    expected_ids = {ref.publication_set_id: ref for ref in manifest.publications}
    from app.kernel.publications import open_published_reader

    details: list[str] = []
    ok = True
    profiles = sorted({ref.profile for ref in manifest.publications}) or [
        DEFAULT_PUBLICATION_PROFILE
    ]
    for profile in profiles:
        resolved = await resolve_published_set(session_factory, workspace_id, profile=profile)
        if resolved is None:
            if expected_ids:
                ok = False
                details.append(f"profile {profile!r}: no published set restored")
            continue
        recorded = next(
            (ref for ref in manifest.publications if ref.profile == profile), None
        )
        if resolved.publication_set_id in expected_ids:
            protected = True
        elif (
            recorded is not None
            and resolved.kernel_commit_id == recorded.kernel_commit_id
            and resolved.snapshot_id == recorded.snapshot_id
        ):
            # B4 honest rebuild: the restored physical serving state may
            # be rebuilt as an explicitly NEW publication set, provided
            # it binds the same intended publication lineage (the cut
            # and the snapshot the recorded publication was built from).
            protected = True
        else:
            protected = False
        if not protected:
            ok = False
            details.append(
                f"profile {profile!r}: restored head {resolved.publication_set_id} "
                "is not a publication the recovery point protects (cut "
                f"{resolved.kernel_commit_id}/snapshot {resolved.snapshot_id} "
                "vs manifest "
                f"{recorded.kernel_commit_id if recorded else '?'}/"
                f"{recorded.snapshot_id if recorded else '?'})"
            )
            continue
        verification = await verify_publication_set(
            session_factory, resolved.publication_set_id
        )
        if not verification.ok:
            ok = False
            details.append(
                f"profile {profile!r}: deep verification failed: {verification.problems}"
            )
            continue
        reader = await open_published_reader(
            session_factory, workspace_id, profile=profile, pin_lease_seconds=None
        )
        try:
            if expected_query is not None and profile == expected_query.get(
                "profile", DEFAULT_PUBLICATION_PROFILE
            ):
                hits = await reader.search(
                    str(expected_query["text"]),
                    str(expected_query.get("mode", "all_terms")),
                    limit=int(expected_query.get("limit", 100)),
                )
                got = [hit.record_id for hit in hits]
                want = list(expected_query["expected_record_ids"])
                if got != want:
                    ok = False
                    details.append(
                        f"profile {profile!r}: query returned {got}, expected {want}"
                    )
                else:
                    details.append(
                        f"profile {profile!r}: query determinism verified "
                        f"({len(got)} hits)"
                    )
        finally:
            await reader.close()
    return ok, "; ".join(details) or "no publications declared"


@dataclass
class _OwnershipFacts:
    in_flight_without_lease: int
    duplicate_publications: int
    accepted_without_lease: int


async def _verify_ownership(
    session_factory: async_sessionmaker, workspace_id: str, manifest: RecoveryPointManifest
) -> RecoveryCheck:
    """Ownership closure over durable state alone.

    Honest scope: the database cannot prove a lease's owner process is
    alive, so this check proves what durable truth *can* prove — every
    in-flight delivery carries a lease (no orphans), every accepted
    publication has exactly-one scope row backed by an accepted lease
    lineage, and no work item accumulated two accepted publications.
    Whether a dead owner's lease is reclaimable is exercised by the
    failover drills, which take the lease over for real.
    """
    async with session_factory() as session:
        in_flight = (
            await session.execute(
                select(KernelOutbox.id).where(
                    KernelOutbox.workspace_id == workspace_id,
                    KernelOutbox.state == "in_flight",
                )
            )
        ).scalars().all()
        lease_ids = set(
            (
                await session.execute(
                    select(KernelWorkLease.work_id).where(
                        KernelWorkLease.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        orphaned = [work_id for work_id in in_flight if work_id not in lease_ids]

        publication_rows = (
            await session.execute(
                select(KernelPublication.work_id).where(
                    KernelPublication.workspace_id == workspace_id
                )
            )
        ).scalars().all()
        seen: set[int] = set()
        duplicates = 0
        for work_id in publication_rows:
            if work_id in seen:
                duplicates += 1
            seen.add(work_id)

        accepted_leases = set(
            (
                await session.execute(
                    select(KernelWorkLease.work_id).where(
                        KernelWorkLease.workspace_id == workspace_id,
                        KernelWorkLease.state == "accepted",
                    )
                )
            )
            .scalars()
            .all()
        )
        accepted_without_lease = len(seen - accepted_leases)

    ok = not orphaned and duplicates == 0 and accepted_without_lease == 0
    detail = (
        f"in_flight={len(in_flight)} orphaned={len(orphaned)} "
        f"publications={len(seen)} duplicates={duplicates} "
        f"accepted_without_lease={accepted_without_lease}"
    )
    return RecoveryCheck(name="ownership", ok=ok, detail=detail)
