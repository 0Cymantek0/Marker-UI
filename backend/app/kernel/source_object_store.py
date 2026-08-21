"""S3-compatible source artifact store (V3.2 PR83B3, industrial profile).

An object-store implementation of the :class:`~app.kernel.source_store.
SourceArtifactStore` contract: acquired source documents live in a
shared S3-compatible namespace a fresh process or replacement node can
resolve from committed ``ContentRevisionRecord`` truth — no node-local
``SOURCE_STORE_ROOT`` dependency, no re-trust of the original external
path.

Built on the PR83B1 payload-store machinery (SigV4 over ``httpx``,
conditional creation, full-byte verification, credential-free locators)
with source-specific semantics:

* **ownership separation**: the namespace prefix (default
  ``kernel-sources``) is disjoint from the kernel payload namespace, so
  payload GC/list/delete scopes can never touch source artifacts. The
  store holds bytes only — kernel records remain the one source truth;
* **suffix-carrying keys**: ``<prefix>/<aa>/<hex>.<suffix>`` keeps the
  converter-routing extension in the durable key, mirroring the local
  store's layout;
* **single-open streamed staging**: the local source file is opened
  once, hashed in a first streamed pass, uploaded in a second streamed
  pass from the *same* descriptor (signed with the first pass's
  SHA-256, so the server verifies the body end to end), with pre/post
  handle-identity stats around both passes — the PR70/71 TOCTOU
  discipline, applied across the network boundary;
* **conditional create + convergence**: PUT carries ``If-None-Match:
  *``; a 412 loser verifies the winner's bytes by full read-back hash
  before reporting success; wrong bytes at a claimed content identity
  fail closed and are never overwritten;
* **read-back verification**: every fresh stage re-reads the object and
  re-hashes it; ETag is never a content proof;
* **verified materialization**: ``materialize_to`` streams the object
  into a caller-owned destination while hashing — corrupt or truncated
  objects are refused, so a converter-facing working copy is only ever
  the committed bytes.

Availability honesty: HEAD length is a cheap presence gate
(``available_length``), while acceptance and consumption are always
content-verified. An unreachable endpoint raises
:class:`~app.kernel.source_store.SourceStoreError`; there is no
fallback to any other authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping

import httpx

from app.kernel.errors import InjectedFaultError
from app.kernel.object_store import S3StoreConfig, s3_request_headers, s3_url
from app.kernel.source_store import (
    BLOB_HEX_PATTERN,
    BLOB_KEY_PATTERN,
    SUFFIX_PATTERN,
    IncoherentSourceError,
    SourceStatEvidence,
    SourceStoreError,
    StagedSource,
    errno_eLOOP,
)

__all__ = [
    "S3_SOURCE_STORE_PROFILE",
    "S3SourceStore",
    "SOURCE_S3_FAULT_PHASES",
]

#: Profile name persisted with revisions acquired through this store.
S3_SOURCE_STORE_PROFILE = "marker.kernel.source.s3.v1"

_S3_XML_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_CHUNK_SIZE = 1024 * 1024

# Phase names up to ``after-read`` deliberately match the local store so
# TOCTOU conformance runs against both profiles unchanged.
PHASE_AFTER_RESOLVE = "after-resolve"
PHASE_AFTER_OPEN = "after-open"
PHASE_DURING_READ = "during-read"
PHASE_AFTER_READ = "after-read"
PHASE_BEFORE_PUT = "before-put"
PHASE_AFTER_PUT = "after-put"
PHASE_AFTER_VERIFY = "after-verify"

#: Faults raised along the staging protocol (exception-style, for
#: crash-window tests). Mutation-style injection uses ``hooks`` instead.
SOURCE_S3_FAULT_PHASES = frozenset(
    {
        PHASE_AFTER_RESOLVE,
        PHASE_AFTER_OPEN,
        PHASE_DURING_READ,
        PHASE_AFTER_READ,
        PHASE_BEFORE_PUT,
        PHASE_AFTER_PUT,
        PHASE_AFTER_VERIFY,
    }
)


class S3SourceStore:
    """Content-addressed source artifact store over an S3-compatible service.

    Instances hold one ``httpx.AsyncClient``; share an instance per
    endpoint within a process and ``close()`` it exactly once.
    """

    profile = S3_SOURCE_STORE_PROFILE

    def __init__(
        self,
        config: S3StoreConfig,
        *,
        fault_phases: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(timeout=config.timeout)
        self._namespace_lock = asyncio.Lock()
        self._namespace_ready = False
        self._faults = frozenset(fault_phases)
        self.stage_calls = 0
        self.dedup_hits = 0
        self.bytes_read = 0
        self.bytes_written = 0
        self.bytes_read_back = 0
        self.bytes_materialized = 0

    @classmethod
    def build_default(
        cls,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        prefix: str,
    ) -> "S3SourceStore":
        """Construct from environment-shaped parts (factory entrypoint)."""
        return cls(
            S3StoreConfig(
                endpoint_url=endpoint_url,
                bucket=bucket,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                region=region,
                prefix=prefix,
            )
        )

    async def close(self) -> None:
        """Release the HTTP pool; optionally remove a test namespace."""
        try:
            if self._config.delete_namespace_on_close:
                for key in await self._list_raw_keys():
                    await self._send("DELETE", key)
                await self._send("DELETE", f"/{self._config.bucket}")
        finally:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # key/locator derivation (validated digest + suffix only)
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
                f"invalid artifact suffix: {suffix!r} must match a dotted extension"
            )
        return suffix

    def locator_for(self, blob_key: str, suffix: str) -> str:
        """Credential-free locator: ``s3://<bucket>/<prefix>/<aa>/<hex><ext>``."""
        hex_digest = self.validate_blob_key(blob_key).removeprefix("sha256:")
        ext = self.validate_suffix(suffix)
        return f"s3://{self._config.bucket}/{self._config.prefix}/{hex_digest[:2]}/{hex_digest}{ext}"

    def _object_path(self, blob_key: str, suffix: str) -> str:
        hex_digest = self.validate_blob_key(blob_key).removeprefix("sha256:")
        ext = self.validate_suffix(suffix)
        return (
            f"/{self._config.bucket}/{self._config.prefix}/"
            f"{hex_digest[:2]}/{hex_digest}{ext}"
        )

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        content: AsyncIterator[bytes] | None = None,
        content_sha256: str | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        signed = s3_request_headers(
            self._config,
            method,
            path,
            query=query,
            body=body,
            extra_headers=headers,
            content_sha256=content_sha256,
        )
        url = s3_url(self._config, path, query=query)
        if stream:
            return await self._client.send(
                self._client.build_request(
                    method, url, headers=signed, content=content
                ),
                stream=True,
            )
        return await self._client.request(
            method, url, headers=signed, content=body
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._ensure_namespace()
        return await self._send(method, path, **kwargs)

    async def _status(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self._request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SourceStoreError(
                f"object store {method} transport failure: {exc}"
            ) from exc

    async def _ensure_namespace(self) -> None:
        if self._namespace_ready:
            return
        async with self._namespace_lock:
            if self._namespace_ready:
                return
            response = await self._send("PUT", f"/{self._config.bucket}")
            # 409: MinIO's BucketAlreadyOwnedByYou; AWS answers 200 for
            # an idempotent re-create by the owner.
            if response.status_code not in (200, 201, 409):
                raise SourceStoreError(
                    f"namespace ensure failed for bucket "
                    f"{self._config.bucket}: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
            self._namespace_ready = True

    def _maybe_inject(self, phase: str) -> None:
        if phase in self._faults:
            raise InjectedFaultError(phase)

    @staticmethod
    def _run_hook(hooks: Mapping[str, Callable[[], None]], phase: str) -> None:
        hook = hooks.get(phase)
        if hook is not None:
            hook()

    # ------------------------------------------------------------------
    # acquisition (stable-handle streaming into the shared object store)
    # ------------------------------------------------------------------

    async def stage_from_path(
        self,
        source: Path,
        *,
        suffix: str,
        hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> StagedSource:
        """Acquire *source* coherently and publish it content-addressed.

        Same TOCTOU contract and hook seams as the local store's
        ``stage_from_path``: one open descriptor, pre/post identity
        stats, streamed hashing; a source that mutates between or during
        the hashing and upload passes is rejected as incoherent and
        never staged.
        """
        ext = self.validate_suffix(suffix)
        hooks = hooks or {}
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

        try:
            # Pass 1 (same discipline as the local store): stream-hash
            # from the single open descriptor with identity evidence.
            digest_hex, total, pre, post1 = await asyncio.to_thread(
                self._hash_fd, fd, hooks
            )
            self.bytes_read += total
            self._maybe_inject(PHASE_AFTER_READ)
            self._run_hook(hooks, PHASE_AFTER_READ)
            if total != pre["size"]:
                raise IncoherentSourceError(
                    f"source {resolved} changed size during read: opened at "
                    f"{pre['size']} bytes, read {total} bytes"
                )
            if post1 != pre:
                changed = sorted(k for k in post1 if post1[k] != pre[k])
                raise IncoherentSourceError(
                    f"source {resolved} identity changed during read (fields: {changed}); "
                    "pre/post handle evidence mismatch"
                )

            blob_key = f"sha256:{digest_hex}"
            object_path = self._object_path(blob_key, ext)
            locator = self.locator_for(blob_key, ext)

            head = await self._status("HEAD", object_path)
            if head.status_code == 200:
                # Dedup: identical bytes were acquired before. Confirm
                # by full read-back hash — never trust presence alone.
                existing = await self._stream_digest(object_path)
                if existing is not None:
                    if existing[0] == digest_hex:
                        self.dedup_hits += 1
                        return StagedSource(
                            blob_key=blob_key,
                            byte_length=total,
                            locator=locator,
                            artifact_path=None,
                            already_present=True,
                            pre_stat=dict(pre),
                            post_stat=dict(post1),
                        )
                    raise SourceStoreError(
                        f"object {blob_key}{ext} occupied by different content; "
                        "refusing to overwrite"
                    )
                # Vanished between HEAD and GET: treat as absent and
                # let the conditional create below re-establish it.
            elif head.status_code != 404:
                raise SourceStoreError(
                    f"object store HEAD {blob_key}{ext} failed: HTTP "
                    f"{head.status_code} {head.text[:200]}"
                )

            self._maybe_inject(PHASE_BEFORE_PUT)

            # Pass 2: upload from the SAME descriptor. The upload is
            # signed with pass 1's hash, so the server verifies the
            # body end to end; a re-hash during the upload catches a
            # mutation between passes before the read-back would.
            os.lseek(fd, 0, os.SEEK_SET)
            pre2 = SourceStatEvidence(os.fstat(fd))
            if pre2 != post1:
                raise IncoherentSourceError(
                    f"source {resolved} identity changed between hashing and "
                    "upload; refusing to stage mixed evidence"
                )

            response = await self._put_stream(fd, object_path, total, digest_hex)
            if response.status_code in (200, 201):
                pass  # created by this call
            elif response.status_code == 412:
                # Conditional-create loser: verify the winner's bytes
                # by full read-back instead of overwriting them.
                winner = await self._stream_digest(object_path)
                if winner is None or winner[0] != digest_hex:
                    raise SourceStoreError(
                        f"object {blob_key}{ext} occupied by different content; "
                        "refusing to overwrite"
                    )
                self.dedup_hits += 1
                return StagedSource(
                    blob_key=blob_key,
                    byte_length=total,
                    locator=locator,
                    artifact_path=None,
                    already_present=True,
                    pre_stat=dict(pre),
                    post_stat=dict(post1),
                )
            else:
                raise SourceStoreError(
                    f"object store PUT {blob_key}{ext} failed: HTTP "
                    f"{response.status_code} {response.text[:200]}"
                )
            self.bytes_written += total

            post2 = SourceStatEvidence(os.fstat(fd))
            if post2 != pre2:
                raise IncoherentSourceError(
                    f"source {resolved} identity changed during upload; "
                    "refusing to accept mixed evidence"
                )

            self._maybe_inject(PHASE_AFTER_PUT)

            read_back = await self._stream_digest(object_path)
            self.bytes_read_back += total
            if read_back is None or read_back[0] != digest_hex:
                raise SourceStoreError(
                    f"read-back verification failed for {blob_key}{ext}: stored "
                    "bytes differ from staged bytes"
                )
            self._maybe_inject(PHASE_AFTER_VERIFY)
        finally:
            os.close(fd)

        return StagedSource(
            blob_key=blob_key,
            byte_length=total,
            locator=locator,
            artifact_path=None,
            already_present=False,
            pre_stat=dict(pre),
            post_stat=dict(post2),
        )

    def _hash_fd(
        self, fd: int, hooks: Mapping[str, Callable[[], None]]
    ) -> tuple[str, int, dict[str, int], dict[str, int]]:
        """First streamed pass: hash + identity evidence from one fd."""
        digest = hashlib.sha256()
        total = 0
        pre = SourceStatEvidence(os.fstat(fd))
        self._maybe_inject(PHASE_AFTER_OPEN)
        self._run_hook(hooks, PHASE_AFTER_OPEN)
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
            total += len(chunk)
        post = SourceStatEvidence(os.fstat(fd))
        return digest.hexdigest(), total, pre, post

    async def _put_stream(
        self, fd: int, object_path: str, length: int, digest_hex: str
    ) -> httpx.Response:
        """Streamed conditional PUT signed with the pre-computed hash."""
        upload_digest = hashlib.sha256()

        async def _body() -> AsyncIterator[bytes]:
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = length
            while remaining > 0:
                chunk = await asyncio.to_thread(
                    os.read, fd, min(_CHUNK_SIZE, remaining)
                )
                if not chunk:
                    break
                upload_digest.update(chunk)
                remaining -= len(chunk)
                yield chunk
            if remaining != 0:
                raise IncoherentSourceError(
                    "source shrank while being uploaded; refusing partial staging"
                )
            if upload_digest.hexdigest() != digest_hex:
                raise IncoherentSourceError(
                    "source bytes changed between hashing and upload; "
                    "uploaded body does not match the staged digest"
                )

        return await self._status(
            "PUT",
            object_path,
            headers={"If-None-Match": "*", "Content-Length": str(length)},
            content=_body(),
            content_sha256=digest_hex,
            stream=True,
        )

    # ------------------------------------------------------------------
    # verification / availability
    # ------------------------------------------------------------------

    async def _stream_digest(self, object_path: str) -> tuple[str, int] | None:
        """Full-body streamed GET + hash; ``None`` when absent."""
        response = await self._status("GET", object_path, stream=True)
        try:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise SourceStoreError(
                    f"object store GET {object_path} failed: HTTP "
                    f"{response.status_code} {response.text[:200]}"
                )
            digest = hashlib.sha256()
            total = 0
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                digest.update(chunk)
                total += len(chunk)
            return digest.hexdigest(), total
        finally:
            await response.aclose()

    async def verify_artifact(
        self, blob_key: str, suffix: str, expected_length: int | None = None
    ) -> bool:
        """Full re-hash verification of one artifact (availability truth)."""
        try:
            object_path = self._object_path(blob_key, suffix)
        except SourceStoreError:
            return False
        try:
            observed = await self._stream_digest(object_path)
        except SourceStoreError:
            return False
        if observed is None:
            return False
        hex_digest, total = observed
        if expected_length is not None and total != expected_length:
            return False
        return hex_digest == blob_key.removeprefix("sha256:")

    async def artifact_exists(self, blob_key: str, suffix: str) -> bool:
        try:
            object_path = self._object_path(blob_key, suffix)
        except SourceStoreError:
            return False
        response = await self._status("HEAD", object_path)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise SourceStoreError(
            f"object store HEAD {blob_key}{suffix} failed: HTTP {response.status_code}"
        )

    async def available_length(self, blob_key: str, suffix: str) -> int | None:
        """Byte length if the artifact is present, else ``None`` (HEAD)."""
        try:
            object_path = self._object_path(blob_key, suffix)
        except SourceStoreError:
            return None
        response = await self._status("HEAD", object_path)
        if response.status_code != 200:
            return None
        try:
            return int(response.headers["Content-Length"])
        except (KeyError, ValueError):
            return None  # present but length unknown: not verifiable here

    # ------------------------------------------------------------------
    # verified materialization (converter-facing working copies)
    # ------------------------------------------------------------------

    async def materialize_to(self, blob_key: str, suffix: str, destination: Path) -> Path:
        """Stream the object into *destination* while verifying content.

        The bytes are hashed as they stream; a truncated or tampered
        object raises before any file is published. The destination is
        written through tmp -> fsync -> atomic replace and marked
        read-only, mirroring the local store's publish discipline. The
        result is a working copy owned by the caller — deleting it can
        never affect the durable shared object.
        """
        object_path = self._object_path(blob_key, suffix)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.parent / f"{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        total = 0
        response = await self._status("GET", object_path, stream=True)
        try:
            if response.status_code == 404:
                raise SourceStoreError(
                    f"source object {blob_key}{suffix} is missing from the "
                    "object store; cannot materialize"
                )
            if response.status_code != 200:
                raise SourceStoreError(
                    f"object store GET {blob_key}{suffix} failed: HTTP "
                    f"{response.status_code} {response.text[:200]}"
                )
            with open(tmp_path, "xb") as out:
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    digest.update(chunk)
                    out.write(chunk)
                    total += len(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            self._discard_quietly(tmp_path)
            raise
        finally:
            await response.aclose()

        if digest.hexdigest() != blob_key.removeprefix("sha256:"):
            self._discard_quietly(tmp_path)
            raise SourceStoreError(
                f"source object {blob_key}{suffix} failed content verification "
                f"at materialization ({total} bytes); refusing to publish"
            )
        for _ in range(4):
            try:
                os.replace(tmp_path, destination)
                break
            except PermissionError:
                if destination.exists():
                    self._clear_readonly(destination)
        else:
            os.replace(tmp_path, destination)
        self.bytes_materialized += total
        self._mark_readonly(destination)
        return destination

    # ------------------------------------------------------------------
    # maintenance (test namespace cleanup only; no source GC authority)
    # ------------------------------------------------------------------

    async def _list_raw_keys(self) -> list[str]:
        """Raw object key paths under the store prefix (cleanup scope)."""
        keys: list[str] = []
        continuation: str | None = None
        prefix = f"{self._config.prefix}/"
        while True:
            params: list[tuple[str, str]] = [("list-type", "2"), ("prefix", prefix)]
            if continuation:
                params.append(("continuation-token", continuation))
            query = "&".join(
                f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                for k, v in sorted(params)
            )
            response = await self._status(
                "GET", f"/{self._config.bucket}", query=query
            )
            if response.status_code != 200:
                raise SourceStoreError(
                    f"object store LIST failed: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
            root = ET.fromstring(response.content)
            for content in root.findall(f"{_S3_XML_NS}Contents"):
                key_el = content.find(f"{_S3_XML_NS}Key")
                if key_el is None or not key_el.text:
                    continue
                keys.append(f"/{self._config.bucket}/{key_el.text}")
            token_el = root.find(f"{_S3_XML_NS}NextContinuationToken")
            truncated_el = root.find(f"{_S3_XML_NS}IsTruncated")
            if (
                truncated_el is not None
                and (truncated_el.text or "").lower() == "true"
                and token_el is not None
                and token_el.text
            ):
                continuation = token_el.text
                continue
            return sorted(keys)

    async def list_blob_keys(self) -> list[str]:
        """Blob keys present under the store prefix (``sha256:<hex>``)."""
        keys: list[str] = []
        for raw in await self._list_raw_keys():
            name = raw.rsplit("/", 1)[-1].split(".")[0]
            if BLOB_HEX_PATTERN.fullmatch(name):
                keys.append(f"sha256:{name}")
        return sorted(set(keys))

    @staticmethod
    def _discard_quietly(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _mark_readonly(path: Path) -> None:
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
