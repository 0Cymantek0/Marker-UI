"""Shared source-artifact store conformance (PR83B3).

``app.kernel.source_store.SourceArtifactStore`` is the store-neutral
boundary the acquisition service and converter dispatch depend on:
content-addressed identity, idempotent publication, verified
availability, and cheap existence probes. PR83B3 adds an S3-compatible
implementation alongside the node-local one, so every semantic that the
kernel relies on must hold IDENTICALLY on both profiles — this suite
asserts exactly that, parametrized over store implementations, so any
source-artifact store the kernel accepts proves the same behavior
without kernel-side forks.

Adding an implementation: register a factory in ``STORE_FACTORIES``
(returning a fresh store over an isolated root/bucket).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.kernel.object_store import S3StoreConfig, s3_request_headers, s3_url
from app.kernel.source_object_store import S3_SOURCE_STORE_PROFILE, S3SourceStore
from app.kernel.source_store import (
    LOCAL_SOURCE_STORE_PROFILE,
    LocalSourceStore,
    SourceStoreError,
)
from app.utils.canonical import payload_byte_hash
from tests.s3_provisioning import (
    ACCESS_KEY_ENV,
    ENDPOINT_ENV,
    SECRET_KEY_ENV,
    require_s3_env,
    strict_mode,
    unique_bucket,
)

pytestmark = pytest.mark.asyncio

#: Profile identity each implementation declares (structural contract).
EXPECTED_PROFILES = {
    "local_file": LOCAL_SOURCE_STORE_PROFILE,
    "s3_minio": S3_SOURCE_STORE_PROFILE,
}


def _missing_s3_message() -> str:
    return (
        "S3 source-store conformance needs {endpoint} (object-store base "
        "URL) plus {access} and {secret} credentials; point them at a real "
        "S3-compatible server (MinIO, etc.)"
    ).format(endpoint=ENDPOINT_ENV, access=ACCESS_KEY_ENV, secret=SECRET_KEY_ENV)


def _local_factory(root: Path) -> LocalSourceStore:
    return LocalSourceStore(root)


def _s3_source_factory(root: Path) -> S3SourceStore:
    endpoint, access_key, secret_key = require_s3_env()
    return S3SourceStore(
        S3StoreConfig(
            endpoint_url=endpoint,
            bucket=unique_bucket(),
            access_key_id=access_key,
            secret_access_key=secret_key,
            prefix="kernel-sources",
            delete_namespace_on_close=True,
        )
    )


def _maybe_s3_source_factory():
    """Factory for the shared conformance registry, or ``None``.

    Registers only when the environment provides a real server; strict
    mode fails at collection instead of silently dropping the store
    from the suite (mirrors ``s3_provisioning.maybe_s3_store_factory``).
    """
    endpoint = os.getenv(ENDPOINT_ENV, "").strip()
    access_key = os.getenv(ACCESS_KEY_ENV, "").strip()
    secret_key = os.getenv(SECRET_KEY_ENV, "").strip()
    if not (endpoint and access_key and secret_key):
        if strict_mode():
            pytest.fail("strict mode refuses to skip: " + _missing_s3_message())
        return None
    return _s3_source_factory


#: Implementations that claim the source-artifact store contract. An
#: industrial object-store implementation registers itself here and
#: inherits this whole suite (PR83B3).
STORE_FACTORIES = {
    "local_file": _local_factory,
}

_s3_factory = _maybe_s3_source_factory()
if _s3_factory is not None:
    STORE_FACTORIES["s3_minio"] = _s3_factory


@pytest.fixture(params=sorted(STORE_FACTORIES), ids=sorted(STORE_FACTORIES))
def store_name(request) -> str:
    return request.param


class _StoreHarness:
    """One isolated source-store namespace plus factories for fresh and
    reopen instances over it.

    ``build()`` returns the primary test store (test namespaces are
    removed on close for S3); ``reopen()`` returns an independent store
    instance over the SAME namespace (same root, or same bucket with
    ``delete_namespace_on_close=False``) so durability can be observed
    from a second authority. Every instance is closed in teardown.
    """

    def __init__(self, name: str, tmp_path: Path):
        self.name = name
        self.root = tmp_path / "source_store"
        self._instances: list[LocalSourceStore | S3SourceStore] = []
        self._s3_parts: tuple[str, str, str] | None = None
        self._s3_bucket: str | None = None
        if name == "s3_minio":
            self._s3_parts = require_s3_env()
            self._s3_bucket = unique_bucket()

    def build(self) -> LocalSourceStore | S3SourceStore:
        store = self._make(delete_namespace_on_close=True)
        self._instances.append(store)
        return store

    def reopen(self) -> LocalSourceStore | S3SourceStore:
        store = self._make(delete_namespace_on_close=False)
        self._instances.append(store)
        return store

    def _make(self, delete_namespace_on_close: bool):
        if self.name == "local_file":
            return LocalSourceStore(self.root)
        endpoint, access_key, secret_key = self._s3_parts
        return S3SourceStore(
            S3StoreConfig(
                endpoint_url=endpoint,
                bucket=self._s3_bucket,
                access_key_id=access_key,
                secret_access_key=secret_key,
                prefix="kernel-sources",
                delete_namespace_on_close=delete_namespace_on_close,
            )
        )

    async def aclose(self) -> None:
        for store in reversed(self._instances):
            closer = getattr(store, "close", None)
            if closer is not None:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result


@pytest_asyncio.fixture
async def harness(store_name: str, tmp_path: Path):
    h = _StoreHarness(store_name, tmp_path)
    try:
        yield h
    finally:
        await h.aclose()


@pytest_asyncio.fixture
async def store(harness: _StoreHarness):
    store = harness.build()
    assert store.profile == EXPECTED_PROFILES[harness.name]  # structural contract
    return store


# ---------------------------------------------------------------------------
# profile-specific helpers (the only place a test's behavior may fork)
# ---------------------------------------------------------------------------


def _flip_one_byte(data: bytes) -> bytes:
    wrong = bytearray(data)
    wrong[0] ^= 0xFF
    return bytes(wrong)


async def _tamper_artifact(store, blob_key: str, suffix: str, original: bytes) -> None:
    """Corrupt the committed artifact in place, same size, by profile.

    A size-only check still passes; the content hash must not.
    """
    if isinstance(store, LocalSourceStore):
        path = store.artifact_path(blob_key, suffix)
        path.chmod(0o644)  # clear the read-only hint, then flip one byte
        with open(path, "r+b") as handle:
            handle.seek(0)
            current = handle.read(1)
            handle.seek(0)
            handle.write(bytes([current[0] ^ 0xFF]))
        return

    # S3: act as an independent (non-store) writer against the namespace.
    config = S3StoreConfig(
        endpoint_url=store._config.endpoint_url,
        bucket=store._config.bucket,
        access_key_id=store._config.access_key_id,
        secret_access_key=store._config.secret_access_key,
        prefix="kernel-sources",
    )
    hex_digest = blob_key.removeprefix("sha256:")
    path = f"/{config.bucket}/kernel-sources/{hex_digest[:2]}/{hex_digest}{suffix}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        bucket_path = f"/{config.bucket}"
        created = await client.put(
            s3_url(config, bucket_path),
            headers=s3_request_headers(config, "PUT", bucket_path),
        )
        assert created.status_code in (200, 201, 409), created.text
        wrong = _flip_one_byte(original)
        response = await client.put(
            s3_url(config, path),
            headers=s3_request_headers(config, "PUT", path, body=wrong),
            content=wrong,
        )
        assert response.status_code in (200, 201), response.text


async def _materialize_bytes(
    store, blob_key: str, suffix: str, tmp_path: Path
) -> bytes:
    """Return the committed artifact bytes for one store instance."""
    if isinstance(store, LocalSourceStore):
        return store.artifact_path(blob_key, suffix).read_bytes()
    destination = tmp_path / f"mat-{uuid.uuid4().hex}{suffix}"
    path = await store.materialize_to(blob_key, suffix, destination)
    return path.read_bytes()


async def _expect_materialize_failure(
    store, blob_key: str, suffix: str, tmp_path: Path, *, match: str | None = None
) -> None:
    """S3 materialization must refuse missing/corrupt objects; the local
    profile has no materialization step (verification is the authority)."""
    if isinstance(store, S3SourceStore):
        destination = tmp_path / f"mat-{uuid.uuid4().hex}{suffix}"
        with pytest.raises(SourceStoreError, match=match):
            await store.materialize_to(blob_key, suffix, destination)
        assert not destination.exists()


def _assert_one_durable_write(store, data_len: int) -> None:
    """S3: racing conditional creates converge on exactly one durable
    write. Local: per-call accounting is asserted profile-agnostically
    (``stage_calls``) in the test body."""
    if isinstance(store, S3SourceStore):
        assert store.bytes_written == data_len


# ---------------------------------------------------------------------------
# Content identity
# ---------------------------------------------------------------------------


class TestStagedIdentitySemantics:
    async def test_restage_same_bytes_is_idempotent(self, store, tmp_path):
        data = b"%PDF-1.4 identity-bytes\n"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        first = await store.stage_from_path(src, suffix=".pdf")
        second = await store.stage_from_path(src, suffix=".pdf")
        assert first.blob_key == second.blob_key == payload_byte_hash(data)
        assert second.already_present is True
        assert store.dedup_hits == 1

    async def test_distinct_bytes_get_distinct_keys(self, store, tmp_path):
        a = tmp_path / "a.pdf"
        b = tmp_path / "b.pdf"
        a.write_bytes(b"aaa")
        b.write_bytes(b"bbb")
        sa = await store.stage_from_path(a, suffix=".pdf")
        sb = await store.stage_from_path(b, suffix=".pdf")
        assert sa.blob_key != sb.blob_key
        assert await store.verify_artifact(sa.blob_key, ".pdf")
        assert await store.verify_artifact(sb.blob_key, ".pdf")

    async def test_identical_bytes_share_one_physical_identity(self, store, tmp_path):
        data = b"one-physical-identity"
        s1 = tmp_path / "one.pdf"
        s2 = tmp_path / "two.pdf"
        s1.write_bytes(data)
        s2.write_bytes(data)
        first = await store.stage_from_path(s1, suffix=".pdf")
        second = await store.stage_from_path(s2, suffix=".pdf")
        assert first.blob_key == second.blob_key
        assert await store.verify_artifact(first.blob_key, ".pdf")
        assert await store.available_length(first.blob_key, ".pdf") == len(data)
        assert await store.available_length(first.blob_key, ".pdf") == await (
            store.available_length(second.blob_key, ".pdf")
        )

    async def test_suffix_is_routing_truth(self, store, tmp_path):
        data = b"%PDF-1.4 dual-suffix-bytes"
        src = tmp_path / "doc.bin"
        src.write_bytes(data)
        as_pdf = await store.stage_from_path(src, suffix=".pdf")
        as_txt = await store.stage_from_path(src, suffix=".txt")
        key = payload_byte_hash(data)
        assert as_pdf.blob_key == as_txt.blob_key == key
        assert await store.artifact_exists(key, ".pdf")
        assert await store.artifact_exists(key, ".txt")

    async def test_staged_length_and_identity_evidence(self, store, tmp_path):
        data = b"length-and-stat-evidence"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        staged = await store.stage_from_path(src, suffix=".pdf")
        assert staged.byte_length == len(data)
        assert staged.pre_stat == staged.post_stat


# ---------------------------------------------------------------------------
# Reopen and durability
# ---------------------------------------------------------------------------


class TestReopenAndDurability:
    async def test_reopen_sees_durable_identity(self, store, harness, tmp_path):
        data = b"reopen-durable-bytes"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        staged = await store.stage_from_path(src, suffix=".pdf")

        independent = harness.reopen()
        assert await independent.verify_artifact(staged.blob_key, ".pdf") is True
        assert await independent.available_length(staged.blob_key, ".pdf") == len(data)
        assert await _materialize_bytes(independent, staged.blob_key, ".pdf", tmp_path) == data

    async def test_source_mutation_after_acquisition_does_not_affect_identity(
        self, store, tmp_path
    ):
        data = b"original-committed-bytes"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        staged = await store.stage_from_path(src, suffix=".pdf")
        src.write_bytes(b"mutated-completely-different-content")
        assert await store.verify_artifact(staged.blob_key, ".pdf") is True
        assert await store.available_length(staged.blob_key, ".pdf") == len(data)

    async def test_source_deletion_after_acquisition_does_not_affect_artifact(
        self, store, tmp_path
    ):
        data = b"deleted-source-durable"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        staged = await store.stage_from_path(src, suffix=".pdf")
        src.unlink()
        assert await store.verify_artifact(staged.blob_key, ".pdf") is True
        assert await store.artifact_exists(staged.blob_key, ".pdf") is True


# ---------------------------------------------------------------------------
# Fail-closed availability
# ---------------------------------------------------------------------------


class TestFailClosedAvailability:
    async def test_missing_blob_is_honestly_unavailable(self, store, tmp_path):
        missing = payload_byte_hash(b"never-staged")
        assert await store.artifact_exists(missing, ".pdf") is False
        assert await store.available_length(missing, ".pdf") is None
        assert await store.verify_artifact(missing, ".pdf") is False
        await _expect_materialize_failure(store, missing, ".pdf", tmp_path, match="missing")

    async def test_tampered_artifact_fails_verification(self, store, tmp_path):
        data = b"%PDF-1.4 tamper-target-bytes"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        staged = await store.stage_from_path(src, suffix=".pdf")
        await _tamper_artifact(store, staged.blob_key, ".pdf", data)
        # Same length: a size-only check would pass. The hash must not.
        assert await store.available_length(staged.blob_key, ".pdf") == len(data)
        assert await store.verify_artifact(
            staged.blob_key, ".pdf", expected_length=len(data)
        ) is False
        await _expect_materialize_failure(
            store, staged.blob_key, ".pdf", tmp_path, match="content verification"
        )

    async def test_hostile_inputs_rejected_identically(self, store):
        with pytest.raises(SourceStoreError):
            store.validate_blob_key("not-a-key")
        with pytest.raises(SourceStoreError):
            store.validate_suffix("no-dot")
        with pytest.raises(SourceStoreError):
            store.validate_suffix(".UPPER")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_identical_stages_converge(self, store, tmp_path):
        data = b"%PDF-1.4 concurrent-identical"
        src = tmp_path / "doc.pdf"
        src.write_bytes(data)
        barrier = asyncio.Barrier(8)

        async def worker():
            await barrier.wait()
            return await store.stage_from_path(src, suffix=".pdf")

        results = await asyncio.gather(*(worker() for _ in range(8)))
        keys = {r.blob_key for r in results}
        assert keys == {payload_byte_hash(data)}
        assert all(r.byte_length == len(data) for r in results)
        assert store.stage_calls == 8  # both profiles count every acquisition
        assert await store.verify_artifact(keys.pop(), ".pdf") is True
        _assert_one_durable_write(store, len(data))
