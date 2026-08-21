"""S3-specific source-artifact store semantics (V3.2 PR83B3).

The shared dual-profile suite (``test_source_store_conformance.py``)
proves the store-neutral contract on both profiles. These tests
falsify the S3 profile's own claims against a real server (MinIO by
default):

* staging really streams (multi-chunk sources), verifies read-back,
  and converges duplicates by full-byte comparison — never presence;
* the single-open TOCTOU discipline holds across the network boundary:
  truncation/append/mutation during the hashing pass, and mutation
  between the hashing and upload passes, are rejected as incoherent;
* conditional create is server-enforced: an independent writer's
  identical bytes converge, different bytes at a claimed content
  identity fail closed and are never overwritten;
* an ambiguous PUT outcome (fault after PUT, before read-back) is
  re-observed on retry and converges onto durable truth;
* availability is honest: missing / truncated / tampered objects fail
  content verification, and materialization refuses to publish them;
* a fresh process reopening the namespace from environment alone sees
  the same verified truth;
* the source namespace is disjoint from the kernel payload namespace:
  payload listing/deletion cannot see or remove source artifacts;
* unreachable endpoints fail closed — no fallback authority;
* locators never leak credentials.

Everything runs against the server pointed at by ``MARKER_TEST_S3_*``;
strict mode refuses to skip.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.kernel.errors import InjectedFaultError
from app.kernel.object_store import (
    S3PayloadStore,
    S3StoreConfig,
    s3_request_headers,
    s3_url,
)
from app.kernel.source_object_store import (
    PHASE_AFTER_PUT,
    PHASE_AFTER_READ,
    PHASE_DURING_READ,
    S3SourceStore,
)
from app.kernel.source_store import (
    IncoherentSourceError,
    SourceStoreError,
    build_source_store,
)
from app.utils.canonical import payload_byte_hash
from tests.s3_provisioning import require_s3_env, unique_bucket

pytestmark = pytest.mark.asyncio

_LOCATOR_PATTERN = re.compile(
    r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/kernel-sources/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{1,10}$"
)

PDF = b"%PDF-1.4 industrial source artifact content\n%\xe2\xe3\xcf\xd3\n"
#: > 2 chunks (1 MiB each) so the streamed staging and materialization
#: paths exercise their chunk loops against the real service.
LARGE = (b"A" * 600_000 + b"B" * 600_000 + b"C" * 600_000 + b"D" * 600_000)


def _source_store_config(bucket: str, *, cleanup: bool = True) -> S3StoreConfig:
    endpoint, access_key, secret_key = require_s3_env()
    return S3StoreConfig(
        endpoint_url=endpoint,
        bucket=bucket,
        access_key_id=access_key,
        secret_access_key=secret_key,
        prefix="kernel-sources",
        delete_namespace_on_close=cleanup,
    )


@pytest_asyncio.fixture
async def store():
    s3 = S3SourceStore(_source_store_config(unique_bucket()))
    try:
        yield s3
    finally:
        await s3.close()


async def _stage(s3: S3SourceStore, tmp: Path, data: bytes, suffix: str = ".pdf"):
    src = tmp / f"src{suffix}"
    src.write_bytes(data)
    return await s3.stage_from_path(src, suffix=suffix)


async def _raw_put(bucket: str, blob_key: str, suffix: str, data: bytes, prefix: str = "kernel-sources") -> None:
    """Act as an independent (non-store) writer against the namespace."""
    endpoint, access_key, secret_key = require_s3_env()
    config = S3StoreConfig(
        endpoint_url=endpoint,
        bucket=bucket,
        access_key_id=access_key,
        secret_access_key=secret_key,
        prefix=prefix,
    )
    hex_digest = blob_key.removeprefix("sha256:")
    path = f"/{bucket}/{prefix}/{hex_digest[:2]}/{hex_digest}{suffix}"
    bucket_path = f"/{bucket}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        created = await client.put(
            s3_url(config, bucket_path),
            headers=s3_request_headers(config, "PUT", bucket_path),
        )
        assert created.status_code in (200, 201, 409), created.text
        headers = s3_request_headers(config, "PUT", path, body=data)
        response = await client.put(s3_url(config, path), headers=headers, content=data)
        assert response.status_code in (200, 201), response.text


# ---------------------------------------------------------------------------
# staging, dedup, verification
# ---------------------------------------------------------------------------


class TestStaging:
    async def test_stage_verify_materialize_roundtrip(self, store, tmp_path):
        staged = await _stage(store, tmp_path, PDF)
        assert staged.blob_key == payload_byte_hash(PDF)
        assert staged.byte_length == len(PDF)
        assert staged.already_present is False
        assert staged.artifact_path is None
        assert staged.pre_stat == staged.post_stat

        assert await store.verify_artifact(staged.blob_key, ".pdf") is True
        assert await store.artifact_exists(staged.blob_key, ".pdf") is True
        assert await store.available_length(staged.blob_key, ".pdf") == len(PDF)

        destination = tmp_path / "work" / "copy.pdf"
        path = await store.materialize_to(staged.blob_key, ".pdf", destination)
        assert path == destination
        assert destination.read_bytes() == PDF

    async def test_multichunk_source_streams_and_verifies(self, store, tmp_path):
        staged = await _stage(store, tmp_path, LARGE, ".bin")
        assert staged.byte_length == len(LARGE)
        assert staged.blob_key == payload_byte_hash(LARGE)
        assert await store.verify_artifact(staged.blob_key, ".bin") is True
        destination = tmp_path / "large.bin"
        await store.materialize_to(staged.blob_key, ".bin", destination)
        assert destination.read_bytes() == LARGE
        assert store.bytes_read == len(LARGE)
        assert store.bytes_written == len(LARGE)
        assert store.bytes_read_back == len(LARGE)
        assert store.bytes_materialized == len(LARGE)

    async def test_duplicate_stage_converges_with_full_byte_proof(self, store, tmp_path):
        first = await _stage(store, tmp_path, PDF)
        second = await _stage(store, tmp_path, PDF)
        assert first.already_present is False
        assert second.already_present is True
        assert second.blob_key == first.blob_key
        assert store.stage_calls == 2
        assert store.dedup_hits == 1

    async def test_distinct_bytes_get_distinct_identities(self, store, tmp_path):
        a = await _stage(store, tmp_path, PDF, ".pdf")
        b = await _stage(store, tmp_path, PDF + b"more", ".pdf")
        assert a.blob_key != b.blob_key
        assert await store.verify_artifact(a.blob_key, ".pdf")
        assert await store.verify_artifact(b.blob_key, ".pdf")

    async def test_same_bytes_different_suffix_are_distinct_objects(self, store, tmp_path):
        a = await _stage(store, tmp_path, PDF, ".pdf")
        b = await _stage(store, tmp_path, PDF, ".txt")
        assert a.blob_key == b.blob_key
        assert await store.artifact_exists(a.blob_key, ".pdf")
        assert await store.artifact_exists(a.blob_key, ".txt")
        # Suffix is converter-routing truth: both durable keys resolve.
        assert await store.available_length(a.blob_key, ".txt") == len(PDF)

    async def test_locators_are_credential_free(self, store, tmp_path):
        staged = await _stage(store, tmp_path, PDF)
        locator = store.locator_for(staged.blob_key, ".pdf")
        assert _LOCATOR_PATTERN.match(locator)
        endpoint, _, secret_key = require_s3_env()
        assert "marker-marker" not in locator or secret_key != "marker-marker"
        assert secret_key not in locator
        assert endpoint not in locator
        assert "@" not in locator and ":" not in locator.removeprefix("s3://").split("/", 1)[0]

    async def test_hostile_keys_and_suffixes_are_rejected(self, store):
        with pytest.raises(SourceStoreError):
            store.validate_blob_key("../../etc/passwd")
        with pytest.raises(SourceStoreError):
            store.validate_suffix(".pdf/../../escape")
        with pytest.raises(SourceStoreError):
            store.validate_suffix("pdf")


# ---------------------------------------------------------------------------
# TOCTOU discipline across the network boundary
# ---------------------------------------------------------------------------


class TestToctouRejection:
    async def test_truncation_during_hashing_pass_is_incoherent(self, store, tmp_path):
        src = tmp_path / "doc.pdf"
        src.write_bytes(PDF)

        def _truncate():
            os.truncate(src, 4)

        with pytest.raises(IncoherentSourceError):
            await store.stage_from_path(
                src, suffix=".pdf", hooks={PHASE_DURING_READ: _truncate}
            )
        # Nothing was staged for the rejected acquisition.
        assert await store.list_blob_keys() == []

    async def test_append_during_hashing_pass_is_incoherent(self, store, tmp_path):
        src = tmp_path / "doc.pdf"
        src.write_bytes(PDF)

        def _append():
            with open(src, "ab") as handle:
                handle.write(b"appended")

        with pytest.raises(IncoherentSourceError):
            await store.stage_from_path(
                src, suffix=".pdf", hooks={PHASE_DURING_READ: _append}
            )
        assert await store.list_blob_keys() == []

    async def test_mutation_between_hashing_and_upload_is_incoherent(self, store, tmp_path):
        src = tmp_path / "doc.pdf"
        src.write_bytes(PDF)

        def _rewrite():
            with open(src, "r+b") as handle:
                handle.seek(0)
                handle.write(b"X")  # same size, different content

        with pytest.raises(IncoherentSourceError):
            await store.stage_from_path(
                src, suffix=".pdf", hooks={PHASE_AFTER_READ: _rewrite}
            )
        assert await store.list_blob_keys() == []

    async def test_replaced_path_acquires_current_bytes(self, store, tmp_path):
        src = tmp_path / "doc.pdf"
        src.write_bytes(PDF)
        staged = await _stage(store, tmp_path, PDF)

        # Replacement after acquisition cannot affect the committed
        # object; a new acquisition of the replaced path is a NEW
        # identity, never a splice.
        src.write_bytes(PDF + b" replaced")
        staged2 = await store.stage_from_path(src, suffix=".pdf")
        assert staged2.blob_key != staged.blob_key
        assert await store.verify_artifact(staged.blob_key, ".pdf") is True


# ---------------------------------------------------------------------------
# conditional create, ambiguous outcomes, corruption
# ---------------------------------------------------------------------------


class TestConditionalCreateAndCorruption:
    async def test_independent_writer_identical_bytes_converge(self, store, tmp_path):
        blob_key = payload_byte_hash(PDF)
        await _raw_put(store._config.bucket, blob_key, ".pdf", PDF)
        staged = await store.stage_from_path(
            _write(tmp_path, PDF), suffix=".pdf"
        )
        assert staged.already_present is True
        assert staged.blob_key == blob_key
        assert store.dedup_hits == 1

    async def test_wrong_bytes_at_claimed_identity_fail_closed(self, store, tmp_path):
        blob_key = payload_byte_hash(PDF)
        occupant = b"not the claimed content at all"
        await _raw_put(store._config.bucket, blob_key, ".pdf", occupant)
        src = _write(tmp_path, PDF)
        with pytest.raises(SourceStoreError, match="occupied by different content"):
            await store.stage_from_path(src, suffix=".pdf")
        # Nothing was overwritten: the corrupt occupant remains exactly
        # what the independent writer put there (refusal, not repair).
        endpoint, access_key, secret_key = require_s3_env()
        async with httpx.AsyncClient(timeout=30.0) as client:
            hex_digest = blob_key.removeprefix("sha256:")
            path = (
                f"/{store._config.bucket}/kernel-sources/"
                f"{hex_digest[:2]}/{hex_digest}.pdf"
            )
            probe = S3StoreConfig(
                endpoint_url=endpoint,
                bucket=store._config.bucket,
                access_key_id=access_key,
                secret_access_key=secret_key,
                prefix="kernel-sources",
            )
            response = await client.get(
                s3_url(probe, path),
                headers=s3_request_headers(probe, "GET", path),
            )
        assert response.content == occupant

    async def test_ambiguous_put_outcome_converges_on_retry(self, store, tmp_path):
        faulty = S3SourceStore(
            _source_store_config(store._config.bucket, cleanup=False),
            fault_phases={PHASE_AFTER_PUT},
        )
        try:
            src = _write(tmp_path, PDF)
            with pytest.raises(InjectedFaultError):
                await faulty.stage_from_path(src, suffix=".pdf")
        finally:
            await faulty.close()

        # The PUT may or may not have landed; a later observer must
        # converge by reading/verifying durable truth, not by assuming
        # failure or re-uploading blindly.
        staged = await store.stage_from_path(_write(tmp_path, PDF), suffix=".pdf")
        assert staged.blob_key == payload_byte_hash(PDF)
        assert staged.already_present is True
        assert await store.verify_artifact(staged.blob_key, ".pdf") is True

    async def test_missing_object_is_honestly_unavailable(self, store, tmp_path):
        absent = payload_byte_hash(b"never staged")
        assert await store.artifact_exists(absent, ".pdf") is False
        assert await store.available_length(absent, ".pdf") is None
        assert await store.verify_artifact(absent, ".pdf") is False
        with pytest.raises(SourceStoreError, match="missing"):
            await store.materialize_to(absent, ".pdf", tmp_path / "x.pdf")

    async def test_truncated_object_fails_content_verification(self, store, tmp_path):
        staged = await _stage(store, tmp_path, PDF)
        await _raw_put(
            store._config.bucket, staged.blob_key, ".pdf", PDF[:-5]
        )
        assert await store.available_length(staged.blob_key, ".pdf") == len(PDF) - 5
        assert await store.verify_artifact(staged.blob_key, ".pdf") is False
        with pytest.raises(SourceStoreError, match="content verification"):
            await store.materialize_to(
                staged.blob_key, ".pdf", tmp_path / "trunc.pdf"
            )
        assert not (tmp_path / "trunc.pdf").exists()

    async def test_tampered_same_size_object_fails_hash_verification(self, store, tmp_path):
        staged = await _stage(store, tmp_path, PDF)
        tampered = bytearray(PDF)
        tampered[10] ^= 0xFF
        await _raw_put(store._config.bucket, staged.blob_key, ".pdf", bytes(tampered))
        # Same length: a size-only check would pass. Content hash must not.
        assert await store.available_length(staged.blob_key, ".pdf") == len(PDF)
        assert await store.verify_artifact(staged.blob_key, ".pdf") is False
        with pytest.raises(SourceStoreError, match="content verification"):
            await store.materialize_to(
                staged.blob_key, ".pdf", tmp_path / "t.pdf"
            )
        assert not (tmp_path / "t.pdf").exists()


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


class TestConcurrentStaging:
    async def test_many_concurrent_identical_stages_converge(self, store, tmp_path):
        src = _write(tmp_path, PDF)
        barrier = asyncio.Barrier(4)

        async def _worker() -> object:
            await barrier.wait()
            return await store.stage_from_path(src, suffix=".pdf")

        results = await asyncio.gather(*(_worker() for _ in range(4)))
        keys = {r.blob_key for r in results}
        assert keys == {payload_byte_hash(PDF)}
        assert all(r.byte_length == len(PDF) for r in results)
        assert await store.verify_artifact(keys.pop(), ".pdf") is True
        # 4 racing conditional creates: at most one fresh write, the
        # rest converge; never a corruption or an overwrite.
        assert store.stage_calls == 4
        assert store.bytes_written == len(PDF)


# ---------------------------------------------------------------------------
# unreachable endpoints / fail-closed construction
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_unreachable_endpoint_raises_without_fallback(self, tmp_path):
        s3 = S3SourceStore(
            S3StoreConfig(
                endpoint_url="http://127.0.0.1:1",
                bucket="marker-unreachable",
                access_key_id="x",
                secret_access_key="y",
                prefix="kernel-sources",
                timeout=2.0,
            )
        )
        try:
            src = _write(tmp_path, PDF)
            with pytest.raises(SourceStoreError):
                await s3.stage_from_path(src, suffix=".pdf")
        finally:
            await s3.close()

    async def test_factory_requires_full_s3_configuration(self, monkeypatch):
        # app.core.config snapshots the environment at import; the
        # factory reads the snapshot at call time, so patch attributes.
        monkeypatch.setattr("app.core.config.SOURCE_STORE_PROFILE", "s3")
        monkeypatch.setattr("app.core.config.SOURCE_S3_ENDPOINT", "")
        monkeypatch.setattr("app.core.config.SOURCE_S3_BUCKET", "")
        monkeypatch.setattr("app.core.config.SOURCE_S3_ACCESS_KEY", "")
        monkeypatch.setattr("app.core.config.SOURCE_S3_SECRET_KEY", "")
        with pytest.raises(SourceStoreError, match="refusing to fall back"):
            build_source_store()

    async def test_factory_rejects_unknown_profile(self, monkeypatch):
        monkeypatch.setattr("app.core.config.SOURCE_STORE_PROFILE", "tape-drive")
        with pytest.raises(SourceStoreError, match="unknown MARKER_SOURCE_STORE_PROFILE"):
            build_source_store()

    async def test_factory_builds_configured_s3_store(self, monkeypatch):
        endpoint, access_key, secret_key = require_s3_env()
        bucket = unique_bucket()
        monkeypatch.setattr("app.core.config.SOURCE_STORE_PROFILE", "s3")
        monkeypatch.setattr("app.core.config.SOURCE_S3_ENDPOINT", endpoint)
        monkeypatch.setattr("app.core.config.SOURCE_S3_BUCKET", bucket)
        monkeypatch.setattr("app.core.config.SOURCE_S3_ACCESS_KEY", access_key)
        monkeypatch.setattr("app.core.config.SOURCE_S3_SECRET_KEY", secret_key)
        s3 = build_source_store()
        assert isinstance(s3, S3SourceStore)
        assert s3.profile == "marker.kernel.source.s3.v1"
        assert s3._config.bucket == bucket
        assert s3._config.prefix == "kernel-sources"
        await s3.close()


# ---------------------------------------------------------------------------
# ownership separation from the kernel payload namespace
# ---------------------------------------------------------------------------


class TestPayloadNamespaceIsolation:
    async def test_payload_store_cannot_see_or_delete_source_artifacts(self, tmp_path):
        endpoint, access_key, secret_key = require_s3_env()
        bucket = unique_bucket()
        source = S3SourceStore(
            S3StoreConfig(
                endpoint_url=endpoint,
                bucket=bucket,
                access_key_id=access_key,
                secret_access_key=secret_key,
                prefix="kernel-sources",
            )
        )
        payload = S3PayloadStore(
            S3StoreConfig(
                endpoint_url=endpoint,
                bucket=bucket,
                access_key_id=access_key,
                secret_access_key=secret_key,
                delete_namespace_on_close=True,  # teardown owns the bucket
            )
        )
        try:
            staged = await _stage(source, tmp_path, PDF)
            await payload.stage(PDF)  # identical bytes, payload namespace

            payload_keys = await payload.list_objects()
            source_keys = await source.list_blob_keys()
            assert payload_byte_hash(PDF) in payload_keys
            assert staged.blob_key in source_keys
            # Identical bytes share the blob-key STRING in both
            # namespaces (content identity is profile-neutral); the
            # physical namespaces stay disjoint by locator path.
            assert payload.locator_for(staged.blob_key) != source.locator_for(
                staged.blob_key, ".pdf"
            )
            assert "kernel-sources" not in payload.locator_for(staged.blob_key)
            assert "kernel-payloads" not in source.locator_for(staged.blob_key, ".pdf")
            # Same bytes, both namespaces, independent objects:
            assert await source.verify_artifact(staged.blob_key, ".pdf") is True
            payload_check = await payload.check_object(payload_byte_hash(PDF))
            assert payload_check.available is True

            # Payload deletion (GC tombstone scope) removes only the
            # payload object; the source artifact survives untouched.
            await payload.delete_object(payload_byte_hash(PDF))
            assert await source.verify_artifact(staged.blob_key, ".pdf") is True
            assert await source.artifact_exists(staged.blob_key, ".pdf") is True
        finally:
            await payload.close()
            await source.close()


# ---------------------------------------------------------------------------
# fresh-process reopen (store-level, environment-only reconstruction)
# ---------------------------------------------------------------------------

_REOPEN_PROBE = """
import asyncio
import os
import sys

sys.path.insert(0, os.environ["MARKER_TEST_BACKEND_ROOT"])

from app.kernel.source_object_store import S3SourceStore
from app.kernel.object_store import S3StoreConfig


async def main() -> None:
    store = S3SourceStore(
        S3StoreConfig(
            endpoint_url=os.environ["MARKER_TEST_S3_ENDPOINT"],
            bucket=os.environ["MARKER_TEST_S3_BUCKET"],
            access_key_id=os.environ["MARKER_TEST_S3_ACCESS_KEY"],
            secret_access_key=os.environ["MARKER_TEST_S3_SECRET_KEY"],
            prefix="kernel-sources",
        )
    )
    try:
        blob_key = os.environ["MARKER_TEST_S3_KEY"]
        suffix = os.environ["MARKER_TEST_S3_SUFFIX"]
        verified = await store.verify_artifact(blob_key, suffix)
        length = await store.available_length(blob_key, suffix)
        destination = os.environ["MARKER_TEST_S3_DEST"]
        path = await store.materialize_to(blob_key, suffix, __import__("pathlib").Path(destination))
        digest = __import__("hashlib").sha256(__import__("pathlib").Path(destination).read_bytes()).hexdigest()
    finally:
        await store.close()
    print("VERIFY:" + ("ok" if verified else "failed"))
    print("LENGTH:" + str(length))
    print("MATERIALIZE_DIGEST:" + digest)


asyncio.run(main())
"""


class TestFreshProcessReopen:
    async def test_second_process_resolves_committed_object_from_environment(
        self, store, tmp_path
    ):
        staged = await _stage(store, tmp_path, LARGE, ".bin")
        destination = tmp_path / "proc-b" / "materialized.bin"
        destination.parent.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env.update(
            {
                "MARKER_TEST_S3_BUCKET": store._config.bucket,
                "MARKER_TEST_S3_KEY": staged.blob_key,
                "MARKER_TEST_S3_SUFFIX": ".bin",
                "MARKER_TEST_S3_DEST": str(destination),
                "MARKER_TEST_BACKEND_ROOT": str(Path(__file__).resolve().parent.parent),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        completed = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _REOPEN_PROBE,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await completed.communicate()
        assert completed.returncode == 0, err.decode("utf-8", "replace")
        text = out.decode("utf-8", "replace")
        assert "VERIFY:ok" in text
        assert f"LENGTH:{len(LARGE)}" in text
        assert f"MATERIALIZE_DIGEST:{staged.blob_key.removeprefix('sha256:')}" in text
        assert destination.read_bytes() == LARGE


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, data: bytes, name: str = "doc.pdf") -> Path:
    src = tmp_path / name
    src.write_bytes(data)
    return src
