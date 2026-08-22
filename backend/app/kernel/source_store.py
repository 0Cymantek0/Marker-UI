"""Content-addressed immutable source artifact store (V3.2 PR70/71 local
slice; PR83B3 store-neutral capability boundary).

Companion to :mod:`app.kernel.payloads`: the same crash-safe
publish discipline (tmp -> fsync -> atomic replace -> read-back verify ->
read-only hint), applied to *acquired source documents* rather than
kernel payload bytes. Two differences are deliberate:

* artifacts keep a validated ``.<suffix>`` extension because converter
  routing (``StreamInfo``/extension planning) cannot consume an
  extensionless content-addressed name — the alternative would be
  teaching every converter the source-revision contract;
* artifacts are owned by the source-truth subsystem, not the PR64/PR65B
  payload registry/GC — a revision's blob key is durable kernel record
  state (:class:`~app.kernel.records.ContentRevisionRecord`), so GC
  cannot sweep bytes that committed history still references.

PR83B3 splits the capability from the physical topology:
:class:`SourceArtifactStore` is the store-neutral protocol the
acquisition service and runtime consume. :class:`LocalSourceStore`
implements it for the node-local PR70/71 profile;
:class:`~app.kernel.source_object_store.S3SourceStore` implements it for
the industrial shared object-store profile. ``build_source_store()``
constructs the configured profile — fail-closed: an unresolvable
industrial configuration raises rather than degrading to local.

TOCTOU contract for ``stage_from_path``:

1. the *resolved* (realpath) source path is what the caller's policy
   check applies to — an unexpected symlink cannot re-point the open;
2. the file is opened once and every observation (pre/post identity
   stats, hashed bytes) comes from that one open descriptor, so a path
   swapped after the open cannot splice foreign content into the read;
3. byte length and (device, inode, mtime_ns) identity are compared
   before/after the streamed read — replacement, truncation, append, or
   in-place mutation during the read is detected and rejected as
   incoherent instead of being staged;
4. accepted bytes are published content-addressed, so probe/routing and
   conversion consume one immutable artifact rather than re-trusting
   the external path.

Streaming is chunked (1 MiB); no full-file in-memory copy exists.

Fault injection: ``SOURCE_FAULT_PHASES`` raise :class:`InjectedFaultError`
at named protocol points. Separately, ``hooks`` is a test-only mapping of
phase -> callable executed at deterministic acquisition boundaries; TOCTOU
tests mutate the source inside a hook rather than sleeping.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from app.kernel.errors import InjectedFaultError, KernelError

__all__ = [
    "BLOB_HEX_PATTERN",
    "IncoherentSourceError",
    "LOCAL_SOURCE_STORE_PROFILE",
    "LocalSourceStore",
    "SOURCE_FAULT_PHASES",
    "SourceArtifactStore",
    "SourceStatEvidence",
    "StagedSource",
    "build_source_store",
]

#: Profile name for source artifacts staged by this store.
LOCAL_SOURCE_STORE_PROFILE = "marker.kernel.source.local_file.v1"

BLOB_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
BLOB_KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SUFFIX_PATTERN = re.compile(r"^\.[a-z0-9]{1,10}$")

_CHUNK_SIZE = 1024 * 1024

PHASE_AFTER_RESOLVE = "after-resolve"
PHASE_AFTER_OPEN = "after-open"
PHASE_DURING_READ = "during-read"
PHASE_AFTER_READ = "after-read"
PHASE_BEFORE_WRITE = "before-write"
PHASE_AFTER_PUBLISH = "after-publish"
PHASE_AFTER_VERIFY = "after-verify"

#: Faults raised along the staging protocol (exception-style, for
#: crash-window tests). Mutation-style injection uses ``hooks`` instead.
SOURCE_FAULT_PHASES = frozenset(
    {
        PHASE_AFTER_RESOLVE,
        PHASE_AFTER_OPEN,
        PHASE_DURING_READ,
        PHASE_AFTER_READ,
        PHASE_BEFORE_WRITE,
        PHASE_AFTER_PUBLISH,
        PHASE_AFTER_VERIFY,
    }
)


class IncoherentSourceError(KernelError):
    """The source changed while being acquired; no revision is accepted.

    The message names the violated invariant (size mismatch, identity
    change, short read). The acquisition may be retried from the
    beginning, but mixed pre/post evidence must never be staged.
    """


class SourceStoreError(KernelError):
    """Invalid input or I/O failure in the source store."""


def _stat_evidence(st: os.stat_result) -> dict[str, int]:
    """Handle-bound file identity evidence (platform-native values)."""
    return {
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


#: Public alias — shared staging-discipline evidence used by every
#: source-artifact profile (the historical ``__all__`` name).
SourceStatEvidence = _stat_evidence


@dataclass(frozen=True)
class StagedSource:
    """Result of one coherent acquisition into the store.

    ``artifact_path`` is meaningful only for filesystem-backed stores;
    shared object-store profiles publish through their locator instead
    and set it to ``None``.
    """

    blob_key: str
    byte_length: int
    locator: str
    artifact_path: Path | None
    already_present: bool
    pre_stat: dict[str, int]
    post_stat: dict[str, int]


class SourceArtifactStore(Protocol):
    """Store-neutral source-artifact capability (PR83B3).

    The acquisition service, authorization, and dispatch consume this
    boundary; the physical topology (node-local filesystem or shared
    object store) stays behind it. ``blob_key`` semantics —
    ``sha256:<hex>`` of the exact acquired bytes — are profile-neutral:
    both profiles produce the same blob key for the same bytes, so a
    committed ``ContentRevisionRecord`` never needs migration when the
    topology changes; only resolution availability differs.
    """

    #: Declared physical-store profile identity (e.g.
    #: ``LOCAL_SOURCE_STORE_PROFILE``). Persisted with committed
    #: revisions so resolution can prove which topology holds the bytes.
    profile: str

    def validate_blob_key(self, blob_key: str) -> str: ...

    def validate_suffix(self, suffix: str) -> str: ...

    def locator_for(self, blob_key: str, suffix: str) -> str:
        """Credential-free locator for one artifact (identity, not URL)."""
        ...

    async def stage_from_path(
        self,
        source: Path,
        *,
        suffix: str,
        hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> StagedSource: ...

    async def stage_bytes(self, data: bytes, *, suffix: str) -> StagedSource:
        """Publish already-acquired in-memory bytes content-addressed.

        The connector convergence core uses this seam when a provider
        adapter has fetched authoritative bytes itself: provenance
        evidence then lives in the connector observation, not in local
        handle stats.
        """
        ...

    async def artifact_exists(self, blob_key: str, suffix: str) -> bool: ...

    async def available_length(self, blob_key: str, suffix: str) -> int | None:
        """Byte length if the artifact is present, else ``None``.

        Presence + length only — a cheap availability gate mirroring a
        filesystem ``stat``. Full content verification is
        :meth:`verify_artifact` / consumption-time verification.
        """
        ...

    async def verify_artifact(
        self, blob_key: str, suffix: str, expected_length: int | None = None
    ) -> bool:
        """Full re-hash verification of one artifact (availability truth)."""
        ...


class LocalSourceStore:
    """Filesystem-backed immutable artifact store for acquired sources.

    Layout under the root mirrors the payload store:
    ``objects/<hex[0:2]>/<hex>.<suffix>`` plus ``tmp/`` scratch. All
    final paths derive exclusively from a validated hex digest and a
    validated suffix — caller strings never reach path construction.
    """

    profile = LOCAL_SOURCE_STORE_PROFILE

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
        # Windows cannot atomically replace a file another task holds
        # open, so operations touching final artifacts serialize within
        # one process (the local kernel profile is single-process).
        self._io_lock = asyncio.Lock()
        self.stage_calls = 0
        self.dedup_hits = 0
        self.bytes_read = 0
        self.bytes_written = 0
        self.bytes_read_back = 0

    # ------------------------------------------------------------------
    # path derivation (validated digest + suffix only)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_blob_key(blob_key: str) -> str:
        if not isinstance(blob_key, str) or not BLOB_KEY_PATTERN.match(blob_key):
            raise SourceStoreError(
                f"invalid blob key: {blob_key!r} must match {BLOB_KEY_PATTERN.pattern}"
            )
        return blob_key

    @staticmethod
    def validate_suffix(suffix: str) -> str:
        if not isinstance(suffix, str) or not SUFFIX_PATTERN.match(suffix):
            raise SourceStoreError(
                f"invalid artifact suffix: {suffix!r} must match {SUFFIX_PATTERN.pattern}"
            )
        return suffix

    def locator_for(self, blob_key: str, suffix: str) -> str:
        hex_digest = self.validate_blob_key(blob_key).removeprefix("sha256:")
        ext = self.validate_suffix(suffix)
        return f"objects/{hex_digest[:2]}/{hex_digest}{ext}"

    def artifact_path(self, blob_key: str, suffix: str) -> Path:
        locator = self.locator_for(blob_key, suffix)
        path = (self.root / locator).resolve()
        root = self.root.resolve()
        if root not in (path, *path.parents):
            raise SourceStoreError(f"derived path escapes store root: {locator}")
        return path

    # ------------------------------------------------------------------
    # acquisition (stable-handle streaming into an immutable artifact)
    # ------------------------------------------------------------------

    async def stage_from_path(
        self,
        source: Path,
        *,
        suffix: str,
        hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> StagedSource:
        """Acquire *source* coherently and publish it content-addressed.

        ``hooks`` is a deterministic test seam: callables executed at
        named acquisition boundaries (``after-resolve``, ``after-open``,
        ``during-read`` — after the first streamed chunk, ``after-read``)
        where adversarial tests mutate/replace the external source.
        """
        ext = self.validate_suffix(suffix)
        async with self._io_lock:
            return await asyncio.to_thread(self._stage_sync, source, ext, hooks or {})

    def _stage_sync(
        self,
        source: Path,
        ext: str,
        hooks: Mapping[str, Callable[[], None]],
    ) -> StagedSource:
        self.stage_calls += 1
        resolved = Path(os.path.realpath(source))
        self._maybe_inject(PHASE_AFTER_RESOLVE)
        self._run_hook(hooks, PHASE_AFTER_RESOLVE)

        open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(resolved, open_flags | nofollow)
        except OSError as exc:
            if nofollow and exc.errno == errno_eLOOP():
                raise IncoherentSourceError(
                    f"source {resolved} is a symlink; refusing to follow at acquisition"
                ) from exc
            raise SourceStoreError(f"cannot open source {resolved}: {exc}") from exc

        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._tmp_dir / f"{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        total = 0
        pre: dict[str, int] = {}
        post: dict[str, int] = {}
        try:
            pre = _stat_evidence(os.fstat(fd))
            self._maybe_inject(PHASE_AFTER_OPEN)
            self._run_hook(hooks, PHASE_AFTER_OPEN)

            # One open descriptor: hash and stage in the same streamed
            # pass. There is no second path lookup between observation
            # and staged bytes, so nothing can be spliced in between.
            with open(tmp_path, "xb") as out:
                first_chunk = True
                while True:
                    chunk = os.read(fd, _CHUNK_SIZE)
                    if not chunk:
                        break
                    if first_chunk:
                        self._maybe_inject(PHASE_DURING_READ)
                        self._run_hook(hooks, PHASE_DURING_READ)
                        first_chunk = False
                    digest.update(chunk)
                    out.write(chunk)
                    total += len(chunk)
                    self.bytes_read += len(chunk)
                    self.bytes_written += len(chunk)
                post = _stat_evidence(os.fstat(fd))
                out.flush()
                os.fsync(out.fileno())
        except InjectedFaultError:
            self._discard_quietly(tmp_path)
            raise
        except OSError as exc:
            self._discard_quietly(tmp_path)
            raise SourceStoreError(f"source staging failed for {resolved}: {exc}") from exc
        finally:
            os.close(fd)

        self._run_hook(hooks, PHASE_AFTER_READ)

        try:
            self._maybe_inject(PHASE_AFTER_READ)
            if total != pre["size"]:
                raise IncoherentSourceError(
                    f"source {resolved} changed size during read: opened at "
                    f"{pre['size']} bytes, read {total} bytes"
                )
            if post != pre:
                changed = sorted(k for k in post if post[k] != pre[k])
                raise IncoherentSourceError(
                    f"source {resolved} identity changed during read (fields: {changed}); "
                    "pre/post handle evidence mismatch"
                )

            blob_key = f"sha256:{digest.hexdigest()}"
            final_path = self.artifact_path(blob_key, ext)
            locator = self.locator_for(blob_key, ext)

            if final_path.is_file():
                # Dedup: identical bytes were acquired before. The verified
                # tmp bytes and the existing artifact claim the same
                # content-addressed name; keep whichever proves real by
                # re-hash (a corrupt occupant is healed by replacement).
                if self._hash_file(final_path) == digest.hexdigest():
                    self.dedup_hits += 1
                    self._discard_quietly(tmp_path)
                    return StagedSource(
                        blob_key=blob_key,
                        byte_length=total,
                        locator=locator,
                        artifact_path=final_path,
                        already_present=True,
                        pre_stat=dict(pre),
                        post_stat=dict(post),
                    )
                self._clear_readonly(final_path)
                self._replace_atomic(tmp_path, final_path)
                self._fsync_dir(final_path.parent)
            else:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                self._maybe_inject(PHASE_BEFORE_WRITE)
                self._replace_atomic(tmp_path, final_path)
                self._fsync_dir(final_path.parent)
                self._maybe_inject(PHASE_AFTER_PUBLISH)

            read_back = self._hash_file(final_path)
            self.bytes_read_back += total
            if read_back != digest.hexdigest():
                raise SourceStoreError(f"read-back verification failed for {blob_key}")
            self._mark_readonly(final_path)
            self._maybe_inject(PHASE_AFTER_VERIFY)
        except (InjectedFaultError, IncoherentSourceError, SourceStoreError):
            # The tmp file is never truth; only a completed rename can
            # leave residue, and that residue is unreachable-but-valid
            # content (discarding it here is unnecessary and racy).
            if tmp_path.is_file():
                self._discard_quietly(tmp_path)
            raise

        return StagedSource(
            blob_key=blob_key,
            byte_length=total,
            locator=locator,
            artifact_path=final_path,
            already_present=False,
            pre_stat=dict(pre),
            post_stat=dict(post),
        )

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    async def stage_bytes(self, data: bytes, *, suffix: str) -> StagedSource:
        """Publish exact in-memory bytes as an immutable artifact.

        Same content-addressing, dedup, read-back verification, and
        read-only finalization as :meth:`stage_from_path`. The staging
        provenance is the caller's acquisition evidence (provider fetch
        with version/revision pinning), recorded in the observation that
        consumes this staging — not in filesystem handle stats.
        """
        ext = self.validate_suffix(suffix)
        if not isinstance(data, (bytes, bytearray)):
            raise SourceStoreError("stage_bytes requires bytes")
        async with self._io_lock:
            return await asyncio.to_thread(self._stage_bytes_sync, bytes(data), ext)

    def _stage_bytes_sync(self, data: bytes, ext: str) -> StagedSource:
        self.stage_calls += 1
        digest_hex = hashlib.sha256(data).hexdigest()
        total = len(data)
        evidence = {"byte_length": total}
        blob_key = f"sha256:{digest_hex}"
        final_path = self.artifact_path(blob_key, ext)
        locator = self.locator_for(blob_key, ext)

        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._tmp_dir / f"{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())

            if final_path.is_file():
                if self._hash_file(final_path) == digest_hex:
                    self.dedup_hits += 1
                    self._discard_quietly(tmp_path)
                    return StagedSource(
                        blob_key=blob_key,
                        byte_length=total,
                        locator=locator,
                        artifact_path=final_path,
                        already_present=True,
                        pre_stat=dict(evidence),
                        post_stat=dict(evidence),
                    )
                self._clear_readonly(final_path)
                self._replace_atomic(tmp_path, final_path)
                self._fsync_dir(final_path.parent)
            else:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                self._replace_atomic(tmp_path, final_path)
                self._fsync_dir(final_path.parent)

            read_back = self._hash_file(final_path)
            self.bytes_read_back += total
            if read_back != digest_hex:
                raise SourceStoreError(f"read-back verification failed for {blob_key}")
            self._mark_readonly(final_path)
        except (InjectedFaultError, SourceStoreError):
            if tmp_path.is_file():
                self._discard_quietly(tmp_path)
            raise

        return StagedSource(
            blob_key=blob_key,
            byte_length=total,
            locator=locator,
            artifact_path=final_path,
            already_present=False,
            pre_stat=dict(evidence),
            post_stat=dict(evidence),
        )

    async def verify_artifact(
        self, blob_key: str, suffix: str, expected_length: int | None = None
    ) -> bool:
        """Full re-hash verification of one artifact (availability truth)."""
        async with self._io_lock:
            return await asyncio.to_thread(
                self._verify_sync, blob_key, suffix, expected_length
            )

    def _verify_sync(
        self, blob_key: str, suffix: str, expected_length: int | None
    ) -> bool:
        try:
            path = self.artifact_path(blob_key, suffix)
        except SourceStoreError:
            return False
        if not path.is_file():
            return False
        try:
            if expected_length is not None and path.stat().st_size != expected_length:
                return False
            return self._hash_file(path) == blob_key.removeprefix("sha256:")
        except OSError:
            return False

    async def artifact_exists(self, blob_key: str, suffix: str) -> bool:
        try:
            path = self.artifact_path(blob_key, suffix)
        except SourceStoreError:
            return False
        return await asyncio.to_thread(path.is_file)

    async def available_length(self, blob_key: str, suffix: str) -> int | None:
        try:
            path = self.artifact_path(blob_key, suffix)
        except SourceStoreError:
            return None

        def _size() -> int | None:
            try:
                return path.stat().st_size
            except OSError:
                return None

        return await asyncio.to_thread(_size)

    async def list_artifacts(self) -> list[str]:
        """All blob keys physically present under ``objects/``."""

        def _scan() -> list[str]:
            keys: list[str] = []
            if not self._objects_dir.is_dir():
                return keys
            for shard in sorted(self._objects_dir.iterdir()):
                if not shard.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                    continue
                for obj in sorted(shard.iterdir()):
                    if obj.is_file():
                        hex_digest = obj.name.split(".")[0]
                        if BLOB_HEX_PATTERN.fullmatch(hex_digest):
                            keys.append(f"sha256:{hex_digest}")
            return keys

        return await asyncio.to_thread(_scan)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _maybe_inject(self, phase: str) -> None:
        if phase in self._faults:
            raise InjectedFaultError(phase)

    @staticmethod
    def _run_hook(hooks: Mapping[str, Callable[[], None]], phase: str) -> None:
        hook = hooks.get(phase)
        if hook is not None:
            hook()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _replace_atomic(self, tmp_path: Path, final_path: Path) -> None:
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

    @staticmethod
    def _discard_quietly(tmp_path: Path) -> None:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def errno_eLOOP() -> int:
    """ELOOP value for the current platform (Windows has none)."""
    return getattr(os, "ELOOP", -1)


def build_source_store():
    """Construct the configured source-artifact store (fail-closed).

    Profile selection is explicit and inspectable: ``local`` builds the
    PR70/71 node-local store; ``s3`` builds the PR83B3 industrial
    shared object-store profile and *requires* its full configuration.
    A missing or ambiguous industrial configuration raises — industrial
    source truth must never be an accidental side effect of a local
    default.
    """
    from app.core.config import (
        SOURCE_S3_ACCESS_KEY,
        SOURCE_S3_BUCKET,
        SOURCE_S3_ENDPOINT,
        SOURCE_S3_PREFIX,
        SOURCE_S3_REGION,
        SOURCE_S3_SECRET_KEY,
        SOURCE_STORE_PROFILE,
        SOURCE_STORE_ROOT,
    )

    profile = SOURCE_STORE_PROFILE.strip().lower()
    if profile in ("", "local", "local_file"):
        return LocalSourceStore(SOURCE_STORE_ROOT)
    if profile in ("s3", "object_store"):
        missing = [
            name
            for name, value in (
                ("MARKER_SOURCE_S3_ENDPOINT", SOURCE_S3_ENDPOINT),
                ("MARKER_SOURCE_S3_BUCKET", SOURCE_S3_BUCKET),
                ("MARKER_SOURCE_S3_ACCESS_KEY", SOURCE_S3_ACCESS_KEY),
                ("MARKER_SOURCE_S3_SECRET_KEY", SOURCE_S3_SECRET_KEY),
            )
            if not value
        ]
        if missing:
            raise SourceStoreError(
                "source store profile 's3' requires "
                + ", ".join(missing)
                + "; refusing to fall back to a local source store"
            )
        from app.kernel.source_object_store import S3SourceStore

        return S3SourceStore.build_default(
            endpoint_url=SOURCE_S3_ENDPOINT,
            bucket=SOURCE_S3_BUCKET,
            access_key_id=SOURCE_S3_ACCESS_KEY,
            secret_access_key=SOURCE_S3_SECRET_KEY,
            region=SOURCE_S3_REGION,
            prefix=SOURCE_S3_PREFIX or "kernel-sources",
        )
    raise SourceStoreError(
        f"unknown MARKER_SOURCE_STORE_PROFILE {SOURCE_STORE_PROFILE!r}; "
        "expected 'local' or 's3'"
    )
