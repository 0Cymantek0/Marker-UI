"""Verified source materialization cache (V3.2 PR83B3, Workstream D).

Converter-facing bridge for shared (non-local) source-artifact
profiles: existing converters consume filesystem paths, so an
industrial source revision must become a local working copy before
probe/parse/conversion — without turning that copy into a second
source authority.

Rules that make the bridge honest:

* the durable shared object is the ONLY authority; this cache is a
  rebuildable working-copy layer;
* every cache hit is re-verified by full content hash before reuse —
  a stale, truncated, or tampered local cache file is replaced by a
  fresh verified materialization from durable truth, never trusted;
* materialization itself streams and hashes while writing (see
  :meth:`app.kernel.source_object_store.S3SourceStore.materialize_to`),
  so corrupt remote bytes are refused before publication;
* cache paths derive exclusively from a validated digest and suffix
  (``<root>/<aa>/<hex>.<suffix>``), mirroring the local store's layout
  discipline;
* deleting cache files can never affect the durable shared object.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from app.kernel.source_store import SourceStoreError

__all__ = ["VerifiedSourceMaterializer"]

_CHUNK_SIZE = 1024 * 1024


class VerifiedSourceMaterializer:
    """Node-local content-addressed cache of verified working copies."""

    def __init__(self, store, cache_root: Path | str) -> None:
        #: Any store exposing ``materialize_to`` (shared profiles).
        self._store = store
        self.root = Path(cache_root)
        self.cache_hits = 0
        self.cache_rebuilds = 0
        self.materializations = 0

    # ------------------------------------------------------------------
    # path derivation (validated digest + suffix only)
    # ------------------------------------------------------------------

    def _cache_path(self, blob_key: str, suffix: str) -> Path:
        if not isinstance(blob_key, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", blob_key
        ):
            raise SourceStoreError(f"invalid blob key: {blob_key!r}")
        if not isinstance(suffix, str) or not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            raise SourceStoreError(f"invalid artifact suffix: {suffix!r}")
        hex_digest = blob_key.removeprefix("sha256:")
        path = (
            self.root / hex_digest[:2] / f"{hex_digest}{suffix}"
        ).resolve()
        root = self.root.resolve()
        if root not in (path, *path.parents):
            raise SourceStoreError(f"derived cache path escapes cache root: {path}")
        return path

    # ------------------------------------------------------------------
    # consumption
    # ------------------------------------------------------------------

    async def path_for(
        self,
        blob_key: str,
        suffix: str,
        *,
        expected_length: int | None = None,
    ) -> Path:
        """Return a verified local path holding exactly *blob_key*'s bytes.

        Re-verify the cached copy by full content hash; rebuild from
        durable shared truth when it is absent or does not verify. A
        rebuild that fails verification raises — corrupt durable bytes
        are unavailable truth, not a cache problem to paper over.
        """
        path = self._cache_path(blob_key, suffix)
        if await self._cached_copy_verifies(path, blob_key, expected_length):
            self.cache_hits += 1
            return path
        if path.exists():
            self.cache_rebuilds += 1
        await self._store.materialize_to(blob_key, suffix, path)
        self.materializations += 1
        return path

    async def _cached_copy_verifies(
        self, path: Path, blob_key: str, expected_length: int | None
    ) -> bool:
        try:
            if expected_length is not None and path.stat().st_size != expected_length:
                return False
            return await _hash_file_async(path) == blob_key.removeprefix("sha256:")
        except OSError:
            return False


async def _hash_file_async(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = await asyncio.to_thread(handle.read, _CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
