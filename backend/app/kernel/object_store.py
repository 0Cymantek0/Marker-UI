"""S3-compatible kernel payload storage (V3.2 PR83B1, Workstream 5).

An industrial object-store implementation of the ``KernelPayloadStore``
contract (``app.kernel.payloads``): the same three commit-path
operations — ``stage`` / ``check_object`` / ``object_exists`` — plus the
maintenance surface retirement and reconciliation rely on (``read`` /
``list_objects`` / ``stat_object`` / ``delete_object``). Implemented
directly over the S3 REST API with AWS Signature Version 4 on
``httpx.AsyncClient``: no SDK dependency, exact control over the request
semantics the kernel's invariants depend on.

Store profile — declared, and what the conformance suites prove
against a real service (MinIO by default, any S3v4 endpoint accepted):

* **conditional create**: ``stage`` issues ``PUT`` with
  ``If-None-Match: *``. A 412 loser VERIFIES the winner's bytes by
  full read-back hash before reporting success — concurrent writers of
  identical bytes converge, an unexpected occupant with different bytes
  fails closed, and nothing is ever blindly overwritten;
* **verification strategy**: full-body GET + SHA-256. ETag is not a
  content proof (multipart and vendor transforms make it unreliable);
  HEAD metadata proves presence, not identity;
* **visibility**: strong read-after-write is required by this profile
  and is asserted by the stage→verify sequence itself;
* **upload mode**: single PUT per object, no multipart — no orphaned
  part-upload lifecycle exists in this profile;
* **deletion**: unversioned buckets. ``DELETE`` of a missing key is
  convergent (idempotent); a versioned bucket violates the profile (a
  DELETE would create a delete marker instead of removing bytes);
* **locators**: ``s3://<bucket>/<prefix>/<hex>`` — credentials never
  appear in locators, manifests, or evidence.

Content addressing is identical to the local store: the object name is
the payload's hex sha256, so both profiles produce the same blob keys
for the same bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import httpx

from app.kernel.errors import PayloadStageError
from app.kernel.payloads import (
    BLOB_HEX_PATTERN,
    BLOB_KEY_PATTERN,
    DeleteResult,
    ObjectCheck,
    ObjectStat,
    StagedBlob,
)
from app.utils.canonical import payload_byte_hash

__all__ = [
    "S3_OBJECT_STORE_PROFILE",
    "S3PayloadStore",
    "S3StoreConfig",
    "s3_request_headers",
    "s3_url",
]

#: Profile name persisted with every registered payload reference.
S3_OBJECT_STORE_PROFILE = "marker.kernel.payload.s3.v1"

_S3_XML_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


@dataclass(frozen=True)
class S3StoreConfig:
    """Connection + namespace declaration for one S3-compatible store.

    Credentials live here only in memory; they never reach locators,
    manifests, or logs. Production deployments inject them via
    environment variables, never source files.
    """

    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "us-east-1"
    prefix: str = "kernel-payloads"
    #: Per-request budget (seconds). Kernel payloads, not user
    #: archives: one generous budget covers metadata and full reads.
    timeout: float = 60.0
    #: Test/bootstrap aid: empty and remove the bucket on ``close()``.
    #: Production stores never set this.
    delete_namespace_on_close: bool = False

    @property
    def base_url(self) -> str:
        return self.endpoint_url.rstrip("/")


# ---------------------------------------------------------------------------
# SigV4 request signing (exported so tests and ops tooling can act as
# independent writers against the same namespace)
# ---------------------------------------------------------------------------


def s3_url(config: S3StoreConfig, path: str, *, query: str = "") -> str:
    """Full request URL for one canonical object-API path."""
    url = f"{config.base_url}{path}"
    return f"{url}?{query}" if query else url


def s3_request_headers(
    config: S3StoreConfig,
    method: str,
    path: str,
    *,
    query: str = "",
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    content_sha256: str | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4 headers for one S3 request.

    Path-style addressing; the payload hash is always known (we hash
    what we upload, the empty string otherwise), so unsigned-payload
    streaming is never needed. ``content_sha256`` lets a caller sign a
    streamed body whose SHA-256 was computed in a prior pass (the hex
    digest without the ``sha256:`` prefix); the server then verifies the
    received bytes against the signed hash end to end.
    """
    parsed = urllib.parse.urlsplit(f"{config.base_url}{path}")
    canonical_uri = urllib.parse.quote(path, safe="/")
    if content_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise PayloadStageError(
            "content_sha256 must be a bare 64-hex sha256 digest for signing"
        )
    payload_sha256 = content_sha256 or hashlib.sha256(body).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    # SigV4 signs lowercased header names; extras are normalized in.
    headers: dict[str, str] = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        for name, value in extra_headers.items():
            headers[name.lower()] = value
    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            query,
            canonical_headers,
            signed_names,
            payload_sha256,
        ]
    )
    scope = f"{date_stamp}/{config.region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    signing_key = _hmac(
        _hmac(
            _hmac(
                _hmac(
                    f"AWS4{config.secret_access_key}".encode("utf-8"),
                    date_stamp,
                ),
                config.region,
            ),
            "s3",
        ),
        "aws4_request",
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={config.access_key_id}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return headers


def _canonical_query(params: Iterable[tuple[str, str]]) -> str:
    """S3 canonical query string: keys sorted, RFC 3986 encoded."""
    return "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params)
    )


class S3PayloadStore:
    """Content-addressed payload store over an S3-compatible service.

    Instances hold one ``httpx.AsyncClient``; share an instance per
    endpoint within a process and ``close()`` it exactly once.
    """

    store_profile = S3_OBJECT_STORE_PROFILE

    def __init__(self, config: S3StoreConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(timeout=config.timeout)
        self._namespace_lock = asyncio.Lock()
        self._namespace_ready = False

    async def close(self) -> None:
        """Release the HTTP pool; optionally remove a test namespace."""
        try:
            if self._config.delete_namespace_on_close:
                for key in await self.list_objects():
                    await self._send("DELETE", self._object_path(key))
                await self._send("DELETE", self._bucket_path())
        finally:
            await self._client.aclose()

    # -- key/locator derivation (hex-validated only) -----------------------

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
        return f"s3://{self._config.bucket}/{self._config.prefix}/{hex_digest}"

    def _object_path(self, blob_key: str) -> str:
        hex_digest = self.validate_blob_key(blob_key).removeprefix("sha256:")
        return f"/{self._config.bucket}/{self._config.prefix}/{hex_digest}"

    def _bucket_path(self) -> str:
        return f"/{self._config.bucket}"

    def _prefix(self) -> str:
        return f"{self._config.prefix}/"

    # -- transport ----------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> httpx.Response:
        signed = s3_request_headers(
            self._config, method, path, query=query, body=body, extra_headers=headers
        )
        url = s3_url(self._config, path, query=query)
        return await self._client.request(method, url, headers=signed, content=body)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._ensure_namespace()
        return await self._send(method, path, **kwargs)

    async def _status(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> httpx.Response:
        try:
            return await self._request(
                method, path, query=query, headers=headers, body=body
            )
        except httpx.HTTPError as exc:
            raise PayloadStageError(
                f"object store {method} transport failure: {exc}"
            ) from exc

    async def _ensure_namespace(self) -> None:
        """Create the bucket once, lazily and idempotently.

        Production buckets are provisioned externally; test namespaces
        get one-step isolation.
        """
        if self._namespace_ready:
            return
        async with self._namespace_lock:
            if self._namespace_ready:
                return
            response = await self._send("PUT", self._bucket_path())
            # 409: MinIO's BucketAlreadyOwnedByYou; AWS answers 200 for
            # an idempotent re-create by the owner.
            if response.status_code not in (200, 201, 409):
                raise PayloadStageError(
                    f"namespace ensure failed for bucket "
                    f"{self._config.bucket}: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
            self._namespace_ready = True

    # -- commit capability (KernelPayloadStore protocol) --------------------

    async def _read_body(self, blob_key: str) -> bytes | None:
        """Full-body GET; ``None`` when the object is absent."""
        response = await self._status("GET", self._object_path(blob_key))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PayloadStageError(
                f"object store GET {blob_key} failed: HTTP "
                f"{response.status_code} {response.text[:200]}"
            )
        return response.content

    async def stage(self, data: bytes) -> StagedBlob:
        """Publish exact bytes durably and verify them.

        Idempotent like the local store: an existing verified object is
        reused (byte dedup), a corrupt occupant is healed by verified
        replacement, and a concurrent publisher of the same bytes is
        converged with via read-back verification — never a blind
        overwrite.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise PayloadStageError("payload bytes must be bytes")
        data = bytes(data)
        blob_key = payload_byte_hash(data)
        locator = self.locator_for(blob_key)

        existing = await self._read_body(blob_key)
        if existing == data:
            return StagedBlob(
                blob_key=blob_key,
                payload_length=len(data),
                locator=locator,
                already_present=True,
            )
        heal = existing is not None  # occupant fails verification

        put_headers = {} if heal else {"If-None-Match": "*"}
        response = await self._status(
            "PUT", self._object_path(blob_key), headers=put_headers, body=data
        )
        if response.status_code in (200, 201):
            pass  # created (or healed) by this call
        elif response.status_code == 412 and not heal:
            # Conditional-create loser: verify the winner's bytes
            # instead of overwriting them.
            winner = await self._read_body(blob_key)
            if winner != data:
                raise PayloadStageError(
                    f"object {blob_key} occupied by different content; "
                    "refusing to overwrite"
                )
            return StagedBlob(
                blob_key=blob_key,
                payload_length=len(data),
                locator=locator,
                already_present=True,
            )
        else:
            raise PayloadStageError(
                f"object store PUT {blob_key} failed: HTTP "
                f"{response.status_code} {response.text[:200]}"
            )

        if await self._read_body(blob_key) != data:
            raise PayloadStageError(
                f"read-back verification failed for {blob_key}: stored "
                "bytes differ from staged bytes"
            )
        return StagedBlob(
            blob_key=blob_key,
            payload_length=len(data),
            locator=locator,
            already_present=False,
        )

    async def check_object(
        self, blob_key: str, *, expected_length: int | None = None
    ) -> ObjectCheck:
        """Verified availability (full read-back hash, never a bare stat)."""
        self.validate_blob_key(blob_key)
        locator = self.locator_for(blob_key)
        try:
            body = await self._read_body(blob_key)
        except PayloadStageError:
            # Present but unverifiable (transport/read failure): the
            # corrupt bucket, never "available" or "missing".
            return ObjectCheck(
                blob_key=blob_key, locator=locator,
                exists=True, length_ok=False, hash_ok=False, length=0,
            )
        if body is None:
            return ObjectCheck(
                blob_key=blob_key, locator=locator,
                exists=False, length_ok=False, hash_ok=False, length=0,
            )
        length_ok = expected_length is None or len(body) == expected_length
        hash_ok = payload_byte_hash(body) == blob_key
        return ObjectCheck(
            blob_key=blob_key, locator=locator, exists=True,
            length_ok=length_ok, hash_ok=hash_ok, length=len(body),
        )

    async def object_exists(self, blob_key: str) -> bool:
        """Cheap existence probe (HEAD) for use inside transactions."""
        self.validate_blob_key(blob_key)
        response = await self._status("HEAD", self._object_path(blob_key))
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise PayloadStageError(
            f"object store HEAD {blob_key} failed: HTTP {response.status_code}"
        )

    async def stat_object(self, blob_key: str) -> ObjectStat | None:
        """Physical metadata (size + Last-Modified) for GC accounting.

        A bare HEAD probe, never an availability claim — mirrors the
        local store's stat. ``None`` when the object is absent.
        """
        self.validate_blob_key(blob_key)
        response = await self._status("HEAD", self._object_path(blob_key))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PayloadStageError(
                f"object store HEAD {blob_key} failed: HTTP "
                f"{response.status_code}"
            )
        try:
            length = int(response.headers["Content-Length"])
        except (KeyError, ValueError) as exc:
            raise PayloadStageError(
                f"object store HEAD {blob_key} returned no usable "
                f"Content-Length: {response.headers.get('Content-Length')!r}"
            ) from exc
        modified = response.headers.get("Last-Modified")
        try:
            assert modified is not None
            modified_at = datetime.strptime(
                modified, "%a, %d %b %Y %H:%M:%S %Z"
            ).replace(tzinfo=timezone.utc).timestamp()
        except (AssertionError, ValueError):
            modified_at = 0.0  # age unknown: report epoch zero, never guess
        return ObjectStat(
            blob_key=blob_key,
            length=length,
            last_modified_epoch=modified_at,
        )

    # -- maintenance capability (GC / reconcile; never on the commit path) --

    async def read(self, blob_key: str) -> bytes:
        """Re-open and return verified bytes for one object."""
        body = await self._read_body(blob_key)
        if body is None:
            raise PayloadStageError(f"payload read failed for {blob_key}: not found")
        if payload_byte_hash(body) != blob_key:
            raise PayloadStageError(f"payload read-back hash mismatch for {blob_key}")
        return body

    async def list_objects(self) -> list[str]:
        """All blob keys under the configured namespace."""
        keys: list[str] = []
        continuation: str | None = None
        while True:
            params: list[tuple[str, str]] = [
                ("list-type", "2"),
                ("prefix", self._prefix()),
            ]
            if continuation:
                params.append(("continuation-token", continuation))
            response = await self._status(
                "GET", self._bucket_path(), query=_canonical_query(params)
            )
            if response.status_code != 200:
                raise PayloadStageError(
                    f"object store LIST failed: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
            root = ET.fromstring(response.content)
            for content in root.findall(f"{_S3_XML_NS}Contents"):
                key_el = content.find(f"{_S3_XML_NS}Key")
                if key_el is None or key_el.text is None:
                    continue
                name = key_el.text.rsplit("/", 1)[-1]
                if BLOB_HEX_PATTERN.fullmatch(name):
                    keys.append(f"sha256:{name}")
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

    async def delete_object(self, blob_key: str) -> DeleteResult:
        """Delete one object a durable GC tombstone already authorized.

        Idempotent and honest about absence, mirroring the local store:
        the database tombstone, not this result, is the authority.
        """
        self.validate_blob_key(blob_key)
        existed = await self.object_exists(blob_key)
        if not existed:
            return DeleteResult(blob_key=blob_key, existed=False, deleted=False)
        response = await self._status("DELETE", self._object_path(blob_key))
        if response.status_code not in (200, 204):
            raise PayloadStageError(
                f"payload deletion failed for {blob_key}: HTTP "
                f"{response.status_code} {response.text[:200]}"
            )
        return DeleteResult(blob_key=blob_key, existed=True, deleted=True)
