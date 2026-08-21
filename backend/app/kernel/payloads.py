"""Durable immutable payload staging (V3.2 PR64).

Content-addressed local blob store backing Truth Kernel payload
references. The store is NOT a second truth authority: it holds exact
payload bytes whose availability the database registry describes. The
database commit remains the only linearization point for accepted truth.

Layout under the store root:

* ``objects/<hex[0:2]>/<hex>`` — immutable final objects; the file name
  is the hex sha256 of the exact bytes, so a partial write can never be
  mistaken for a valid object (the name itself is the integrity claim);
* ``tmp/<uuid>.tmp`` — staging scratch; never referenced by truth;
* ``quarantine/<hex>.<n>`` — corrupt final objects displaced when exact
  bytes are re-supplied and verified (tamper evidence, never reused).

Publish protocol (crash-safe, one-sided):

1. write bytes to an exclusive tmp file, flush, ``fsync`` the file;
2. atomically ``os.replace`` tmp onto the final content-addressed path
   (atomic on POSIX and Windows; concurrent publishers of identical
   bytes cannot interleave partial content);
3. ``fsync`` the parent directory where the platform allows it so the
   rename itself is durable (POSIX; Windows cannot fsync directories —
   documented residual caveat);
4. re-open the final path and re-read/re-hash the bytes: only a
   verified read-back may be referenced as available;
5. mark the object read-only as a tamper hint (enforcement stays with
   verification — mutation is always detectable by re-hash).

Crash ordering: any interruption before step 2 completes leaves at most
an unreachable tmp file or an unreachable-but-complete object; no
database reference to it can exist yet. A committed reference therefore
always points at bytes that were published and verified first.

Fault injection: deterministic test hooks raise ``InjectedFaultError``
at named protocol phases; see ``PAYLOAD_FAULT_PHASES``.

Availability truth: ``check_object`` classifies an object that exists
but cannot be read (permission loss, sharing violation, filesystem
error) as present with failed verification — the corrupt bucket —
because availability claims require a verified read. A read failure is
never converted into an "available" or "missing" answer.

Physical retirement (PR65B): ``delete_object`` unlinks one authorized
object idempotently. The store itself never decides what may be
deleted — a durable GC tombstone from the kernel database must already
authorize it, and re-staging the exact bytes after deletion is always a
valid re-publication (the dedup/heal path covers it).
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.kernel.errors import InjectedFaultError, PayloadStageError
from app.utils.canonical import payload_byte_hash

__all__ = [
    "BLOB_HEX_PATTERN",
    "BLOB_KEY_PATTERN",
    "DeleteResult",
    "KERNEL_PAYLOAD_STORE_PROTOCOL",
    "LOCAL_STORE_PROFILE",
    "KernelPayloadStore",
    "ObjectCheck",
    "ObjectStat",
    "PAYLOAD_DECISION_LOCK_SCOPE",
    "PAYLOAD_FAULT_PHASES",
    "PAYLOAD_MAINTENANCE_STORE_PROTOCOL",
    "LocalPayloadStore",
    "PayloadMaintenanceStore",
    "StagedBlob",
]

#: Profile name persisted with every registered payload reference.
LOCAL_STORE_PROFILE = "marker.kernel.payload.local_file.v1"

BLOB_KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
BLOB_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LOCATOR_PATTERN = re.compile(r"^objects/[0-9a-f]{2}/[0-9a-f]{64}$")

PHASE_BEFORE_WRITE = "stage-before-write"
PHASE_MID_WRITE = "stage-mid-write"
PHASE_AFTER_WRITE = "stage-after-write"  # after write, before fsync
PHASE_AFTER_FSYNC = "stage-after-fsync"  # durable tmp, before publish
PHASE_AFTER_PUBLISH = "stage-after-publish"  # renamed, before verify
PHASE_AFTER_VERIFY = "stage-after-verify"  # verified, before caller
PHASE_DELETE_BEFORE_UNLINK = "delete-before-unlink"  # object intact, crash window
PHASE_DELETE_AFTER_UNLINK = "delete-after-unlink"  # unlinked, outcome unrecorded

#: Faults raised along the staging/publication protocol (commit path).
STAGING_FAULT_PHASES = frozenset(
    {
        PHASE_BEFORE_WRITE,
        PHASE_MID_WRITE,
        PHASE_AFTER_WRITE,
        PHASE_AFTER_FSYNC,
        PHASE_AFTER_PUBLISH,
        PHASE_AFTER_VERIFY,
    }
)

#: All fault phases this store can raise (staging + retirement sweep).
PAYLOAD_FAULT_PHASES = STAGING_FAULT_PHASES | frozenset(
    {PHASE_DELETE_BEFORE_UNLINK, PHASE_DELETE_AFTER_UNLINK}
)


@dataclass(frozen=True)
class StagedBlob:
    """Result of publishing (or re-using) one content-addressed object."""

    blob_key: str
    payload_length: int
    locator: str
    #: True when an existing verified object was reused (byte dedup).
    already_present: bool


@dataclass(frozen=True)
class ObjectCheck:
    """Verification outcome for one object in the store."""

    blob_key: str
    locator: str
    exists: bool
    length_ok: bool
    hash_ok: bool
    #: observed byte length of the object (0 when absent)
    length: int = 0

    @property
    def available(self) -> bool:
        return self.exists and self.length_ok and self.hash_ok


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of one authorized object deletion (PR65B GC sweep).

    ``existed`` is False when the object was already physically absent —
    an idempotent retry converging on the same truthful outcome, not an
    error. The database tombstone, not this result, is the authority.
    """

    blob_key: str
    existed: bool
    deleted: bool


@dataclass(frozen=True)
class ObjectStat:
    """Cheap physical metadata for one object (maintenance accounting).

    Presence probe plus length and age, without the verification
    authority of :class:`ObjectCheck`: GC orphan accounting and
    min-age grace consume this. ``last_modified_epoch`` is UTC epoch
    seconds under the store's own clock (filesystem mtime / S3
    Last-Modified); it is operational metadata, never causal truth.
    """

    blob_key: str
    length: int
    last_modified_epoch: float


@runtime_checkable
class KernelPayloadStore(Protocol):
    """The payload-store behavior the kernel commit path depends on
    (PR83A Workstream 2).

    Exactly the operations ``KernelCommitService`` uses around the
    database visibility boundary — nothing more — so an industrial
    object-store implementation can satisfy the same semantics without
    forking kernel truth. Content identity, idempotent republication,
    verified availability claims, and cheap existence probes are the
    contract; the local filesystem store below is the reference
    implementation, and ``tests/test_payload_store_conformance.py`` is
    the reusable behavioral suite any implementation must pass.
    """

    async def stage(self, data: bytes) -> StagedBlob:
        """Publish exact bytes durably; content-addressed, idempotent."""
        ...

    async def check_object(
        self, blob_key: str, *, expected_length: int | None = None
    ) -> ObjectCheck:
        """Verified availability of one object (never a bare stat)."""
        ...

    async def object_exists(self, blob_key: str) -> bool:
        """Cheap existence probe used inside the commit transaction."""
        ...


#: Human-readable name of the behavioral contract (evidence metadata).
KERNEL_PAYLOAD_STORE_PROTOCOL = "marker.kernel.payload.store.v1"


@runtime_checkable
class PayloadMaintenanceStore(Protocol):
    """The payload-store behavior lifecycle maintenance depends on
    (PR83B1 Workstream 6): reconciliation, snapshot verification beyond
    the commit capability, and the PR65B collector.

    Deliberately separate from :class:`KernelPayloadStore` so the commit
    path never gains delete authority just because GC needs it. Local
    scratch-residue operations (``list_tmp``/``cleanup_tmp``) are an
    optional further capability only stores that can create staging
    residue implement — the S3 single-PUT profile cannot, so callers
    feature-detect rather than the protocol lying about it.
    """

    async def read(self, blob_key: str) -> bytes:
        """Re-open and return verified bytes for one object."""
        ...

    async def list_objects(self) -> list[str]:
        """All blob keys physically present in the store's namespace."""
        ...

    async def stat_object(self, blob_key: str) -> ObjectStat | None:
        """Cheap physical metadata; None when the object is absent."""
        ...

    async def delete_object(self, blob_key: str) -> DeleteResult:
        """Retire one object a durable GC tombstone authorized."""
        ...


#: Human-readable name of the maintenance contract (evidence metadata).
PAYLOAD_MAINTENANCE_STORE_PROTOCOL = "marker.kernel.payload.maintenance.v1"

#: Advisory-lock scope serializing payload-retention decisions with the
#: GC deletion decision (PR83B1 Workstream 6). Participants: the GC
#: recheck/tombstone transaction, retention root/pin creation, generation
#: head activation, and payload-carrying commits (registry adoption +
#: tombstone rescue). On SQLite the single-writer model already supplies
#: this serialization; on PostgreSQL every participant takes
#: ``pg_advisory_xact_lock`` on this scope inside its transaction.
PAYLOAD_DECISION_LOCK_SCOPE = ("kernel-payloads", "gc-decision")


class LocalPayloadStore:
    """Filesystem-backed immutable payload store for one database.

    Blocking filesystem work runs on worker threads; instances are safe
    to share across tasks. All durable paths are derived exclusively
    from validated hex digests — record ids and caller strings never
    reach path construction.

    ``KernelPayloadStore`` implementations declare their persisted
    profile name via this attribute so the registry records where the
    bytes actually live (the local store's value, for reference).
    """

    store_profile = LOCAL_STORE_PROFILE

    def __init__(
        self,
        root: Path | str,
        *,
        fault_phases: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.root = Path(root)
        self._faults = frozenset(fault_phases)
        self._objects_dir = self.root / "objects"
        self._tmp_dir = self.root / "tmp"
        self._quarantine_dir = self.root / "quarantine"
        # Windows cannot atomically replace a file another task holds
        # open (no FILE_SHARE_DELETE), so operations touching final
        # objects serialize within one process. The local kernel profile
        # is single-process by construction, mirroring SQLite's own
        # single-writer model.
        self._io_lock = asyncio.Lock()
        # Observability counters (characterization workstream F).
        self.stage_calls = 0
        self.dedup_hits = 0
        self.heal_replacements = 0
        self.bytes_logical = 0
        self.bytes_written = 0
        self.bytes_read_back = 0

    # ------------------------------------------------------------------
    # path derivation (hex-validated only)
    # ------------------------------------------------------------------

    @staticmethod
    def blob_key_for(data: bytes) -> str:
        return payload_byte_hash(data)

    @staticmethod
    def validate_blob_key(blob_key: str) -> str:
        if not isinstance(blob_key, str) or not BLOB_KEY_PATTERN.match(blob_key):
            raise PayloadStageError(
                f"invalid blob key: {blob_key!r} must match {BLOB_KEY_PATTERN.pattern}"
            )
        return blob_key

    def locator_for(self, blob_key: str) -> str:
        hex_digest = self.validate_blob_key(blob_key).removeprefix("sha256:")
        return f"objects/{hex_digest[:2]}/{hex_digest}"

    def object_path(self, blob_key: str) -> Path:
        locator = self.locator_for(blob_key)
        path = (self.root / locator).resolve()
        root = self.root.resolve()
        if root not in (path, *path.parents):
            raise PayloadStageError(f"derived path escapes store root: {locator}")
        return path

    def path_for_locator(self, locator: str) -> Path:
        """Resolve a persisted ``storage_locator`` defensively."""
        if not isinstance(locator, str) or not _LOCATOR_PATTERN.match(locator):
            raise PayloadStageError(
                f"refusing hostile storage locator: {locator!r}"
            )
        path = (self.root / locator).resolve()
        root = self.root.resolve()
        if root not in (path, *path.parents):
            raise PayloadStageError(f"storage locator escapes store root: {locator!r}")
        return path

    # ------------------------------------------------------------------
    # staging
    # ------------------------------------------------------------------

    async def stage(self, data: bytes) -> StagedBlob:
        """Publish exact bytes durably and verify them.

        Idempotent: re-staging identical bytes re-verifies the existing
        object and never rewrites it unless verification fails (heal by
        replacement with re-verification).
        """
        if not isinstance(data, (bytes, bytearray)):
            raise PayloadStageError("payload bytes must be bytes")
        data = bytes(data)
        async with self._io_lock:
            return await asyncio.to_thread(self._stage_sync, data)

    def _stage_sync(self, data: bytes) -> StagedBlob:
        self.stage_calls += 1
        self.bytes_logical += len(data)
        blob_key = payload_byte_hash(data)
        final_path = self.object_path(blob_key)
        locator = self.locator_for(blob_key)

        check = self._check_object_sync(blob_key)
        if check.available:
            # Byte dedup: verified existing object reused as-is.
            self.dedup_hits += 1
            return StagedBlob(
                blob_key=blob_key,
                payload_length=len(data),
                locator=locator,
                already_present=True,
            )
        if check.exists and not check.available:
            # A corrupt/tampered object occupies the name. Quarantine it
            # so the verified replacement below is a fresh publication,
            # never a silent rewrite of tampered evidence.
            self._quarantine_sync(blob_key)
            self.heal_replacements += 1
            if final_path.exists():
                self._clear_readonly(final_path)

        tmp_path = self._tmp_dir / f"{uuid.uuid4().hex}.tmp"
        try:
            self._ensure_dirs(final_path.parent)
            self._maybe_inject(PHASE_BEFORE_WRITE)
            with open(tmp_path, "xb") as handle:
                # Write in two chunks so fault tests can observe a real
                # partial file at the mid-write boundary.
                mid = len(data) // 2
                handle.write(data[:mid])
                self._maybe_inject(PHASE_MID_WRITE)
                handle.write(data[mid:])
                self.bytes_written += len(data)
                self._maybe_inject(PHASE_AFTER_WRITE)
                handle.flush()
                os.fsync(handle.fileno())
            self._maybe_inject(PHASE_AFTER_FSYNC)
            self._replace_atomic(tmp_path, final_path)
            self._fsync_dir(final_path.parent)
            self._maybe_inject(PHASE_AFTER_PUBLISH)

            read_back = self._read_and_hash(final_path)
            self.bytes_read_back += len(read_back)
            if read_back != data:
                raise PayloadStageError(
                    f"read-back verification failed for {blob_key}: stored bytes "
                    "differ from staged bytes"
                )
            # Verified publication: read-only hint, verification stays
            # the real integrity authority.
            self._mark_readonly(final_path)
            self._maybe_inject(PHASE_AFTER_VERIFY)
        except InjectedFaultError:
            self._discard_quietly(tmp_path)
            raise
        except PayloadStageError:
            self._discard_quietly(tmp_path)
            raise
        except OSError as exc:
            self._discard_quietly(tmp_path)
            raise PayloadStageError(
                f"payload staging failed for {blob_key}: {exc}"
            ) from exc

        return StagedBlob(
            blob_key=blob_key,
            payload_length=len(data),
            locator=locator,
            already_present=False,
        )

    # ------------------------------------------------------------------
    # verification / scanning
    # ------------------------------------------------------------------

    async def check_object(self, blob_key: str, *, expected_length: int | None = None) -> ObjectCheck:
        async with self._io_lock:
            return await asyncio.to_thread(
                self._check_object_sync, blob_key, expected_length
            )

    def _check_object_sync(
        self, blob_key: str, expected_length: int | None = None
    ) -> ObjectCheck:
        path = self.object_path(blob_key)
        locator = self.locator_for(blob_key)
        if not path.is_file():
            return ObjectCheck(
                blob_key=blob_key, locator=locator,
                exists=False, length_ok=False, hash_ok=False, length=0,
            )
        try:
            actual = self._read_and_hash(path)
        except PayloadStageError:
            # Bytes exist but cannot be read (permission loss, sharing
            # violation, filesystem error): availability cannot be proven,
            # so the object is reported as present-but-unverifiable — the
            # corrupt bucket — never as available. Availability claims
            # require a verified read.
            return ObjectCheck(
                blob_key=blob_key, locator=locator,
                exists=True, length_ok=False, hash_ok=False, length=0,
            )
        length_ok = expected_length is None or len(actual) == expected_length
        hash_ok = payload_byte_hash(actual) == blob_key
        return ObjectCheck(
            blob_key=blob_key, locator=locator, exists=True,
            length_ok=length_ok, hash_ok=hash_ok, length=len(actual),
        )

    async def read(self, blob_key: str) -> bytes:
        """Re-open and return verified bytes for one object."""
        async with self._io_lock:
            return await asyncio.to_thread(self._read_sync, blob_key)

    def _read_sync(self, blob_key: str) -> bytes:
        path = self.object_path(blob_key)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PayloadStageError(f"payload read failed for {blob_key}: {exc}") from exc
        if payload_byte_hash(data) != blob_key:
            raise PayloadStageError(
                f"payload read-back hash mismatch for {blob_key}"
            )
        return data

    async def list_objects(self) -> list[str]:
        """All blob keys physically present under ``objects/``."""
        return await asyncio.to_thread(self._list_objects_sync)

    def _list_objects_sync(self) -> list[str]:
        keys: list[str] = []
        if not self._objects_dir.is_dir():
            return keys
        for shard in sorted(self._objects_dir.iterdir()):
            if not shard.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                continue
            for obj in sorted(shard.iterdir()):
                if obj.is_file() and BLOB_HEX_PATTERN.fullmatch(obj.name):
                    keys.append(f"sha256:{obj.name}")
        return keys

    async def list_tmp(self) -> list[Path]:
        """Staging scratch files currently present."""
        return await asyncio.to_thread(self._list_tmp_sync)

    def _list_tmp_sync(self) -> list[Path]:
        if not self._tmp_dir.is_dir():
            return []
        return sorted(p for p in self._tmp_dir.iterdir() if p.is_file())

    async def cleanup_tmp(self, *, older_than_seconds: float) -> list[Path]:
        """Delete stale staging scratch files.

        Never touches ``objects/``. Live publishers make staging windows
        milliseconds long, so an age threshold keeps concurrent staging
        safe; callers choose the threshold explicitly.
        """
        return await asyncio.to_thread(self._cleanup_tmp_sync, older_than_seconds)

    def _cleanup_tmp_sync(self, older_than_seconds: float) -> list[Path]:
        removed: list[Path] = []
        now = datetime.now(timezone.utc).timestamp()
        for path in self._list_tmp_sync():
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age >= older_than_seconds and self._unlink_quietly(path):
                removed.append(path)
        return removed

    # ------------------------------------------------------------------
    # retirement (PR65B GC sweep only; never called by the commit path)
    # ------------------------------------------------------------------

    async def object_exists(self, blob_key: str) -> bool:
        """Cheap existence probe (stat only, no hashing).

        Used inside the commit transaction's tombstone-rescue check,
        where a full verify would hold the SQLite writer lock too long.
        Existence is not availability: verification stays the authority.
        """
        async with self._io_lock:
            return await asyncio.to_thread(self.object_path(blob_key).is_file)

    async def stat_object(self, blob_key: str) -> ObjectStat | None:
        """Physical metadata (size + mtime) for maintenance accounting.

        A bare stat, never an availability claim: GC's orphan age/size
        accounting consumes it. None when the object is absent.
        """
        async with self._io_lock:
            return await asyncio.to_thread(self._stat_object_sync, blob_key)

    def _stat_object_sync(self, blob_key: str) -> ObjectStat | None:
        path = self.object_path(blob_key)
        try:
            info = path.stat()
        except OSError:
            return None
        return ObjectStat(
            blob_key=blob_key,
            length=info.st_size,
            last_modified_epoch=info.st_mtime,
        )

    async def delete_object(self, blob_key: str) -> DeleteResult:
        """Unlink one object; idempotent and honest about absence.

        Callers must already hold a durable retirement authorization
        (a GC tombstone) — the store never decides retention policy.
        An already-absent object returns ``existed=False`` without
        error, so crash recovery and retries converge. ``OSError``
        failures raise :class:`PayloadStageError` so the caller records
        a retryable failure instead of a false success. Injected faults
        ``delete-before-unlink`` / ``delete-after-unlink`` bracket the
        unlink for crash-window tests.
        """
        async with self._io_lock:
            return await asyncio.to_thread(self._delete_object_sync, blob_key)

    def _delete_object_sync(self, blob_key: str) -> DeleteResult:
        path = self.object_path(blob_key)
        existed = path.is_file()
        if not existed:
            return DeleteResult(blob_key=blob_key, existed=False, deleted=False)
        self._maybe_inject(PHASE_DELETE_BEFORE_UNLINK)
        # Windows refuses to unlink the read-only tamper hint; clearing
        # it is safe here because the unlink itself is already authorized.
        self._clear_readonly(path)
        try:
            path.unlink()
        except OSError as exc:
            raise PayloadStageError(
                f"payload deletion failed for {blob_key}: {exc}"
            ) from exc
        self._fsync_dir(path.parent)
        self._maybe_inject(PHASE_DELETE_AFTER_UNLINK)
        return DeleteResult(blob_key=blob_key, existed=True, deleted=True)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ensure_dirs(self, objects_shard: Path) -> None:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        objects_shard.mkdir(parents=True, exist_ok=True)

    def _maybe_inject(self, phase: str) -> None:
        if phase in self._faults:
            raise InjectedFaultError(phase)

    def _read_and_hash(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PayloadStageError(f"payload read failed at {path.name}: {exc}") from exc

    def _quarantine_sync(self, blob_key: str) -> None:
        hex_digest = blob_key.removeprefix("sha256:")
        source = self.object_path(blob_key)
        try:
            # Windows refuses to replace/move a read-only target; the
            # bytes are already untrustworthy so clearing the hint is
            # safe. Verification, not permissions, is the authority.
            os.chmod(source, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        n = 0
        while True:
            target = self._quarantine_dir / f"{hex_digest}.{n}"
            if not target.exists():
                break
            n += 1
        try:
            os.replace(source, target)
        except OSError:
            # Quarantine is best-effort tamper evidence; the verified
            # replacement below overwrites the name regardless.
            pass

    def _replace_atomic(self, tmp_path: Path, final_path: Path) -> None:
        """Atomic publish, robust against a concurrent publisher's
        read-only hint on the destination.

        POSIX replaces onto read-only targets freely; Windows refuses.
        Concurrent publishers of identical bytes are expected, so on
        refusal the hint is cleared and the replace retried. Content is
        byte-identical by construction, so whichever publisher wins, the
        result verifies.
        """
        for _ in range(4):
            try:
                os.replace(tmp_path, final_path)
                return
            except PermissionError:
                if final_path.exists():
                    self._clear_readonly(final_path)
        os.replace(tmp_path, final_path)  # final attempt; raise if it fails

    def _mark_readonly(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass  # verification, not permissions, is the authority

    @staticmethod
    def _clear_readonly(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass  # Windows cannot fsync directories; documented caveat
        finally:
            os.close(fd)

    def _discard_quietly(self, tmp_path: Path) -> None:
        self._unlink_quietly(tmp_path)

    @staticmethod
    def _unlink_quietly(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            return False
