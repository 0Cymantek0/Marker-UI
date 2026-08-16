"""Local ArtifactHandle data plane for process-worker results (V3.2 PR68A).

A handle is a way to *locate and verify* data, never a second source of
truth. The conversion result itself remains the only logical truth; this
module moves its large immutable leaves out of the pickled
``WorkerEvent.payload`` control message and through a verified local
file reference instead.

Design in one paragraph: the worker encodes each eligible large field
(text, image bytes, pickled PIL objects, asset bytes) to bytes, writes
them once under a store-managed unique name, and emits a compact wire
envelope whose handles carry ``{name, length, sha256, encoding, kind,
slot, job_id}``. The parent validates the envelope, reads each blob,
verifies length and SHA-256, decodes by kind, rebuilds the exact logical
payload the finalizer already expects, and unlinks the blob. Small
payloads and the whole thread backend keep using queue-inline transport,
and the worker degrades gracefully back to inline if staging fails.

Ownership and lifecycle:

* blobs are EPHEMERAL: written with flush-and-close but no fsync, because
  a crash kills both producer and consumer of the handoff and the job is
  retried from source anyway (documented residual: a machine-level power
  loss can lose an unconsumed blob, which surfaces as an honest failure);
* one consumer per handoff: the parent unlinks after a verified read;
  duplicate delivery finds the blob missing and fails closed instead of
  reconstructing wrong data;
* names are fresh ``uuid4().hex`` per stage — no dedup, therefore no
  shared backing between jobs and no premature deletion of a blob a
  second reader might still need;
* orphans (producer crash mid-stage, consumer crash before consume,
  cancelled jobs) are reclaimed by an age-based sweep at parent startup
  and periodically in the drain thread — never by racing a live reader,
  because a sweep only unlinks files older than the configured age.

Failure honesty: the producer may degrade to inline, but the consumer is
strict. Any missing, oversized, truncated, hash-mismatched, malformed,
cross-job, or wrong-version handle raises :class:`ArtifactHandleError`
so the job fails with a truthful message rather than completing on
wrong bytes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_ENCODINGS",
    "ArtifactHandleError",
    "ArtifactHandleStore",
    "HandleRef",
    "HANDLE_VERSION",
    "HANDLE_WIRE_KEY",
    "default_store",
    "is_handle_envelope",
    "resolve_worker_payload",
    "stage_worker_payload",
]

logger = logging.getLogger(__name__)

#: Wire key marking a payload as an ArtifactHandle envelope.
HANDLE_WIRE_KEY = "artifact_handles"
#: Envelope contract version; receivers reject anything else.
HANDLE_VERSION = 1

#: uuid4 hex — the only string ever allowed to become a path component.
_NAME_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: What a handle points at (decides how the parent re-inserts the value).
ARTIFACT_KINDS = frozenset({"text", "image_bytes", "image_pil", "asset_data", "asset_pil"})
#: How the bytes on disk map back to the value.
ARTIFACT_ENCODINGS = frozenset({"raw", "utf8", "pickle"})

_SLOTS_BY_KIND = {
    "text": "text",
    "image_bytes": "images",
    "image_pil": "images",
    "asset_data": "assets",
    "asset_pil": "assets",
}


class ArtifactHandleError(Exception):
    """Base class for every artifact data-plane failure (fails closed)."""


class ArtifactHandleValidationError(ArtifactHandleError):
    """Malformed/hostile/incompatible handle metadata — never attach."""


class ArtifactMissingError(ArtifactHandleError):
    """The backing blob is absent (already consumed, swept, or never landed)."""


class ArtifactCorruptError(ArtifactHandleError):
    """Backing bytes fail the length or SHA-256 claim — tamper/truncation."""


@dataclass(frozen=True)
class HandleRef:
    """Verified reference to one staged blob.

    ``slot`` is the path of keys/indexes from the payload root to the
    field the value belongs in, e.g. ``("result", "images", "p3.png")``
    or ``("formats_payload", "html", "text")`` or
    ``("result", "assets", 0, "data")``.
    """

    slot: tuple[Any, ...]
    kind: str
    encoding: str
    name: str
    length: int
    sha256: str
    job_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "slot": list(self.slot),
            "kind": self.kind,
            "encoding": self.encoding,
            "name": self.name,
            "length": self.length,
            "sha256": self.sha256,
            "job_id": self.job_id,
        }

    @staticmethod
    def from_wire(data: Any) -> HandleRef:
        if not isinstance(data, dict):
            raise ArtifactHandleValidationError(f"handle must be a dict, got {type(data).__name__}")
        required = {"slot", "kind", "encoding", "name", "length", "sha256", "job_id"}
        missing = required - set(data)
        if missing:
            raise ArtifactHandleValidationError(f"handle missing fields: {sorted(missing)}")
        slot = data["slot"]
        if not isinstance(slot, list) or not slot:
            raise ArtifactHandleValidationError(f"handle slot must be a non-empty list, got {slot!r}")
        clean_slot: list[Any] = []
        for element in slot:
            if isinstance(element, str) and element:
                clean_slot.append(element)
            elif isinstance(element, int) and not isinstance(element, bool) and element >= 0:
                clean_slot.append(element)
            else:
                raise ArtifactHandleValidationError(f"invalid slot element: {element!r}")
        kind = data["kind"]
        if kind not in ARTIFACT_KINDS:
            raise ArtifactHandleValidationError(f"unknown handle kind: {kind!r}")
        encoding = data["encoding"]
        if encoding not in ARTIFACT_ENCODINGS:
            raise ArtifactHandleValidationError(f"unknown handle encoding: {encoding!r}")
        name = data["name"]
        if not isinstance(name, str) or not _NAME_PATTERN.match(name):
            raise ArtifactHandleValidationError(f"refusing hostile artifact name: {name!r}")
        length = data["length"]
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ArtifactHandleValidationError(f"invalid length claim: {length!r}")
        sha256 = data["sha256"]
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ArtifactHandleValidationError(f"invalid sha256 claim: {sha256!r}")
        job_id = data["job_id"]
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            raise ArtifactHandleValidationError(f"invalid job binding: {job_id!r}")
        return HandleRef(
            slot=tuple(clean_slot),
            kind=kind,
            encoding=encoding,
            name=name,
            length=length,
            sha256=sha256,
            job_id=job_id,
        )


class ArtifactHandleStore:
    """Ephemeral verified blob store backing one machine-local handoff.

    Unlike the PR64 kernel payload store this is NOT durable truth: no
    fsync by default (``fsync=True`` restores the durable profile for
    characterization), no content-addressed filenames, no dedup. Every
    stage creates a fresh uniquely-named blob so consumption can unlink
    without racing another job that happens to share bytes.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        fsync: bool = False,
        max_read_bytes: int = 1 << 30,
    ) -> None:
        self.root = Path(root)
        self._blobs_dir = self.root / "blobs"
        self._fsync = bool(fsync)
        self._max_read_bytes = int(max_read_bytes)
        # Observability counters (characterization workstream E).
        self.staged_count = 0
        self.staged_bytes = 0
        self.resolved_count = 0
        self.resolved_bytes = 0
        self.missing_rejects = 0
        self.corrupt_rejects = 0
        self.validation_rejects = 0
        self.failed_unlinks = 0
        self.swept_count = 0

    # ------------------------------------------------------------------
    # path derivation (validated uuid names only)
    # ------------------------------------------------------------------

    def _path_for(self, name: str) -> Path:
        if not isinstance(name, str) or not _NAME_PATTERN.match(name):
            raise ArtifactHandleValidationError(f"refusing hostile artifact name: {name!r}")
        # Race-free derivation: ``Path.resolve()`` realizes existing paths
        # through the OS (``\\?\`` device form on Windows) but not-yet-created
        # ones via string ops, so concurrent creators can see two different
        # "roots". ``os.path.abspath`` never touches the filesystem and is
        # stable across the create boundary; the strict hex validation above
        # is what actually makes traversal impossible.
        root_abs = os.path.abspath(self._blobs_dir)
        path_abs = os.path.abspath(os.path.join(root_abs, f"{name}.bin"))
        norm_root = os.path.normcase(root_abs)
        norm_path = os.path.normcase(path_abs)
        if norm_path != norm_root and not norm_path.startswith(norm_root + os.sep):
            raise ArtifactHandleValidationError(f"derived path escapes store root: {name!r}")
        return Path(path_abs)

    # ------------------------------------------------------------------
    # producer side
    # ------------------------------------------------------------------

    def stage(
        self,
        data: bytes,
        *,
        slot: tuple[Any, ...],
        kind: str,
        encoding: str,
        job_id: str,
    ) -> HandleRef:
        """Write one blob and return its verified reference.

        Raises :class:`OSError` on filesystem failure so the worker can
        degrade to inline transport; never returns a reference to bytes
        that were not fully handed to the OS.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise ArtifactHandleValidationError("artifact data must be bytes")
        data = bytes(data)
        if kind not in ARTIFACT_KINDS:
            raise ArtifactHandleValidationError(f"unknown handle kind: {kind!r}")
        if encoding not in ARTIFACT_ENCODINGS:
            raise ArtifactHandleValidationError(f"unknown handle encoding: {encoding!r}")
        name = uuid.uuid4().hex
        path = self._path_for(name)
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as handle:
            handle.write(data)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())
        if path.stat().st_size != len(data):
            # Partial write (ENOSPC-style): remove and refuse honestly.
            self._unlink_quietly(path)
            raise OSError(f"short artifact write for {name}: {path.stat().st_size} != {len(data)}")
        self.staged_count += 1
        self.staged_bytes += len(data)
        return HandleRef(
            slot=slot,
            kind=kind,
            encoding=encoding,
            name=name,
            length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            job_id=job_id,
        )

    # ------------------------------------------------------------------
    # consumer side
    # ------------------------------------------------------------------

    def resolve(self, ref: HandleRef) -> bytes:
        """Read one blob and verify every claim about it."""
        path = self._path_for(ref.name)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self.missing_rejects += 1
            raise ArtifactMissingError(f"artifact blob absent: {ref.name}") from None
        except OSError as exc:
            self.missing_rejects += 1
            raise ArtifactMissingError(f"artifact blob unavailable: {ref.name}: {exc}") from exc
        if size > self._max_read_bytes:
            self.validation_rejects += 1
            raise ArtifactHandleValidationError(
                f"artifact blob exceeds read bound: {ref.name} claims {size} bytes "
                f"(max {self._max_read_bytes})"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.corrupt_rejects += 1
            raise ArtifactCorruptError(f"artifact blob unreadable: {ref.name}: {exc}") from exc
        if len(data) != ref.length:
            self.corrupt_rejects += 1
            raise ArtifactCorruptError(
                f"artifact length mismatch for {ref.name}: claimed {ref.length}, found {len(data)}"
            )
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            self.corrupt_rejects += 1
            raise ArtifactCorruptError(f"artifact sha256 mismatch for {ref.name}")
        self.resolved_count += 1
        self.resolved_bytes += len(data)
        return data

    def consume(self, ref: HandleRef) -> bytes:
        """Resolve one blob, then unlink its backing file.

        Unlink failure is observable (``failed_unlinks`` counter + log),
        never silently treated as successful cleanup; the age-based sweep
        remains the backstop.
        """
        data = self.resolve(ref)
        path = self._path_for(ref.name)
        try:
            path.unlink()
        except OSError:
            self.failed_unlinks += 1
            logger.warning(
                "artifact unlink failed (sweep will retry): name=%s error suppressed",
                ref.name,
            )
        return data

    # ------------------------------------------------------------------
    # reclamation
    # ------------------------------------------------------------------

    def sweep(self, *, older_than_seconds: float) -> list[Path]:
        """Unlink blobs older than the given age. Never touches live young blobs.

        Live publishers complete staging in milliseconds, and stage→consume
        windows are seconds, so callers pick an age comfortably above both
        (default one hour); this is what makes the sweep safe against a
        still-active reader.
        """
        removed: list[Path] = []
        if not self._blobs_dir.is_dir():
            return removed
        now = time.time()
        for path in list(self._blobs_dir.iterdir()):
            if not path.is_file():
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age >= older_than_seconds and self._unlink_quietly(path):
                removed.append(path)
        self.swept_count += len(removed)
        return removed

    def count_blobs(self) -> int:
        if not self._blobs_dir.is_dir():
            return 0
        return sum(1 for p in self._blobs_dir.iterdir() if p.is_file())

    @staticmethod
    def _unlink_quietly(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Worker side: payload -> wire envelope
# ---------------------------------------------------------------------------


def is_handle_envelope(payload: Any) -> bool:
    """True when *payload* is an ArtifactHandle wire envelope."""
    return (
        isinstance(payload, dict)
        and HANDLE_WIRE_KEY in payload
        and isinstance(payload[HANDLE_WIRE_KEY], dict)
        and len(payload) == 1
    )


def _iter_result_dicts(payload: dict[str, Any]) -> Iterator[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Yield (slot prefix, result dict) for the payload's result shapes."""
    if isinstance(payload.get("result"), dict) or "formats_payload" in payload:
        result = payload.get("result")
        if isinstance(result, dict):
            yield ("result",), result
        formats = payload.get("formats_payload")
        if isinstance(formats, dict):
            for fmt, env in formats.items():
                if isinstance(env, dict):
                    yield ("formats_payload", fmt), env
    else:
        yield (), payload


def _decode_value(data: bytes, kind: str, encoding: str) -> Any:
    if encoding == "utf8":
        return data.decode("utf-8")
    if encoding == "pickle":
        return pickle.loads(data)
    return data


def stage_worker_payload(
    payload: dict[str, Any],
    *,
    store: ArtifactHandleStore,
    job_id: str,
    inline_limit: int = 256 * 1024,
    enabled: bool = True,
) -> dict[str, Any]:
    """Move eligible large fields of *payload* into verified file handles.

    Returns the wire payload to emit. When nothing is eligible (or the
    data plane is disabled) the original payload object is returned
    unchanged, preserving the pre-PR68A inline contract exactly. Staging
    failures degrade gracefully: already-staged fields stay as handles
    and everything not yet staged remains inline, so a failing disk can
    never fail the conversion itself.
    """
    if not enabled or not isinstance(payload, dict):
        return payload

    handles: list[dict[str, Any]] = []
    staging_live = True
    for prefix, result in _iter_result_dicts(payload):
            candidates = _collect_candidates(result, prefix, inline_limit)
            for container, key, kind, encoding, data, slot in candidates:
                if staging_live:
                    try:
                        ref = store.stage(data, slot=slot, kind=kind, encoding=encoding, job_id=job_id)
                    except Exception:  # noqa: BLE001 - the data plane must never fail the conversion
                        logger.exception(
                            "artifact staging failed for job %s at %s; remaining fields stay inline",
                            job_id,
                            "/".join(map(str, slot)),
                        )
                        staging_live = False
                        continue
                    handles.append(ref.to_wire())
                    _remove_field(container, key, kind)

    if not handles:
        return payload
    return {
        HANDLE_WIRE_KEY: {
            "version": HANDLE_VERSION,
            "inline": payload,
            "handles": handles,
        }
    }


def _collect_candidates(
    result: dict[str, Any],
    prefix: tuple[Any, ...],
    inline_limit: int,
) -> list[tuple[Any, Any, str, str, bytes, tuple[Any, ...]]]:
    """Find eligible large fields: (container, key, kind, encoding, bytes, slot)."""
    candidates: list[tuple[Any, Any, str, str, bytes, tuple[Any, ...]]] = []

    text = result.get("text")
    if isinstance(text, str):
        try:
            data = text.encode("utf-8")
            encoding = "utf8"
        except UnicodeEncodeError:
            # Lone-surrogate strings (not produced by marker, but never
            # crash the handoff): pickle round-trips them exactly.
            data = pickle.dumps(text)
            encoding = "pickle"
        if len(data) > inline_limit:
            candidates.append((result, "text", "text", encoding, data, (*prefix, "text")))

    images = result.get("images")
    if isinstance(images, dict):
        for name, value in images.items():
            if isinstance(value, (bytes, bytearray)):
                if len(value) > inline_limit:
                    candidates.append(
                        (images, name, "image_bytes", "raw", bytes(value), (*prefix, "images", name))
                    )
            elif value is not None:
                data = pickle.dumps(value)
                if len(data) > inline_limit:
                    candidates.append(
                        (images, name, "image_pil", "pickle", data, (*prefix, "images", name))
                    )

    assets = result.get("assets")
    if isinstance(assets, list):
        for index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                continue
            blob = asset.get("data")
            if isinstance(blob, (bytes, bytearray)) and len(blob) > inline_limit:
                candidates.append(
                    (asset, "data", "asset_data", "raw", bytes(blob), (*prefix, "assets", index, "data"))
                )
            pil = asset.get("pil")
            if pil is not None:
                data = pickle.dumps(pil)
                if len(data) > inline_limit:
                    candidates.append(
                        (asset, "pil", "asset_pil", "pickle", data, (*prefix, "assets", index, "pil"))
                    )

    return candidates


def _remove_field(container: dict[str, Any], key: Any, kind: str) -> None:
    try:
        container.pop(key, None)
    except TypeError:  # pragma: no cover - defensive
        logger.warning("failed to clear staged field %r (%s)", key, kind)


# ---------------------------------------------------------------------------
# Parent side: wire envelope -> payload
# ---------------------------------------------------------------------------


def resolve_worker_payload(
    wire_payload: dict[str, Any],
    *,
    store: ArtifactHandleStore,
    job_id: str,
) -> dict[str, Any]:
    """Rebuild the logical payload from a wire envelope, strictly.

    Any missing, corrupt, malformed, cross-job, or incompatible handle
    raises :class:`ArtifactHandleError`; the caller must fail the job.
    Blobs are unlinked as they are consumed, so a duplicate delivery of
    the same envelope fails closed (blob absent) instead of rebuilding
    anything twice.
    """
    if not is_handle_envelope(wire_payload):
        return wire_payload

    envelope = wire_payload[HANDLE_WIRE_KEY]
    version = envelope.get("version")
    if version != HANDLE_VERSION:
        raise ArtifactHandleValidationError(
            f"unsupported artifact envelope version: {version!r} (expected {HANDLE_VERSION})"
        )
    inline = envelope.get("inline")
    if not isinstance(inline, dict):
        raise ArtifactHandleValidationError("artifact envelope inline payload must be a dict")
    wire_handles = envelope.get("handles")
    if not isinstance(wire_handles, list):
        raise ArtifactHandleValidationError("artifact envelope handles must be a list")

    rebuilt = inline
    for wire_handle in wire_handles:
        ref = HandleRef.from_wire(wire_handle)
        if ref.job_id != job_id:
            raise ArtifactHandleValidationError(
                f"cross-job artifact handle for {ref.job_id!r} delivered on job {job_id!r}"
            )
        data = store.consume(ref)
        value = _decode_value(data, ref.kind, ref.encoding)
        _place_value(rebuilt, ref.slot, value)

    return rebuilt


def _place_value(root: dict[str, Any], slot: tuple[Any, ...], value: Any) -> None:
    """Walk *slot* from the payload root and set the final field to *value*.

    The inline skeleton retains every intermediate container (staging only
    removes leaves), so a slot that cannot be walked exactly is a corrupt
    envelope and fails closed.
    """
    node: Any = root
    for element in slot[:-1]:
        if isinstance(node, dict):
            if element not in node:
                raise ArtifactHandleValidationError(
                    f"artifact slot {slot!r} does not address the inline payload"
                )
            node = node[element]
        elif isinstance(node, list):
            if not isinstance(element, int) or element >= len(node):
                raise ArtifactHandleValidationError(
                    f"artifact slot {slot!r} does not address the inline payload"
                )
            node = node[element]
        else:
            raise ArtifactHandleValidationError(
                f"artifact slot {slot!r} does not address the inline payload"
            )
        if not isinstance(node, (dict, list)):
            raise ArtifactHandleValidationError(
                f"artifact slot {slot!r} does not address the inline payload"
            )
    last = slot[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list):
        if not isinstance(last, int) or last >= len(node):
            raise ArtifactHandleValidationError(
                f"artifact slot {slot!r} does not address the inline payload"
            )
        node[last] = value
    else:
        raise ArtifactHandleValidationError(
            f"artifact slot {slot!r} does not address the inline payload"
        )


# ---------------------------------------------------------------------------
# Process-local default store (config-driven)
# ---------------------------------------------------------------------------

_DEFAULT_STORE: ArtifactHandleStore | None = None


def default_store() -> ArtifactHandleStore:
    """Lazily-built store bound to the configured root for this process.

    Workers and the parent derive the same root from the same environment,
    which is what makes the file handle resolvable across the boundary.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        from app.core.config import (
            ARTIFACT_HANDLE_MAX_BYTES,
            ARTIFACT_HANDLE_ROOT,
        )

        _DEFAULT_STORE = ArtifactHandleStore(
            ARTIFACT_HANDLE_ROOT,
            max_read_bytes=ARTIFACT_HANDLE_MAX_BYTES,
        )
    return _DEFAULT_STORE
