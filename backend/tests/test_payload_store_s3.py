"""S3-specific payload-store semantics (V3.2 PR83B1, Workstream 5).

The shared suite (``test_payload_store_conformance.py``) proves the
store-neutral contract. These tests falsify the S3 profile's own
claims against a real server (MinIO by default):

* the conditional-create linearization really is enforced by the
  server (412 on competing bytes), not simulated client-side;
* concurrent identical publishers converge with exactly one creator;
* an ambiguous writer (request sent, outcome never observed — the
  crashed-writer window) is re-observed and converged with;
* a fresh process reopening the namespace sees the same truth (no
  in-process caching of availability);
* locators never leak credentials;
* unreachable endpoints fail closed (no silent fallback);
* test namespaces are fully removed on close (no cross-run leakage).

Everything runs against the server pointed at by the ``MARKER_TEST_S3_*``
environment; strict mode refuses to skip.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio

from app.kernel.errors import PayloadStageError
from app.kernel.object_store import (
    S3PayloadStore,
    S3StoreConfig,
    s3_request_headers,
    s3_url,
)
from app.utils.canonical import payload_byte_hash
from tests.s3_provisioning import require_s3_env, unique_bucket

pytestmark = pytest.mark.asyncio

_LOCATOR_PATTERN = re.compile(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[0-9a-z-]+/[0-9a-f]{64}$")

#: Fresh-process reopen probe: constructs a store from the same
#: environment, verifies the named object, prints machine-checkable
#: verdicts. Path-independent (all inputs via environment).
_REOPEN_PROBE = """
import asyncio
import os

from app.kernel.object_store import S3PayloadStore, S3StoreConfig


async def main() -> None:
    store = S3PayloadStore(
        S3StoreConfig(
            endpoint_url=os.environ["MARKER_TEST_S3_ENDPOINT"],
            bucket=os.environ["MARKER_TEST_S3_BUCKET"],
            access_key_id=os.environ["MARKER_TEST_S3_ACCESS_KEY"],
            secret_access_key=os.environ["MARKER_TEST_S3_SECRET_KEY"],
        )
    )
    try:
        blob_key = os.environ["MARKER_TEST_S3_KEY"]
        check = await store.check_object(blob_key)
        data = await store.read(blob_key)
    finally:
        await store.close()
    expected = os.environ["MARKER_TEST_S3_PAYLOAD"].encode("utf-8")
    print("CHECK:" + ("available" if check.available else "unavailable"))
    print("READ:" + ("match" if data == expected else "mismatch"))


asyncio.run(main())
"""


def _config(delete_on_close: bool = True) -> S3StoreConfig:
    endpoint, access_key, secret_key = require_s3_env()
    return S3StoreConfig(
        endpoint_url=endpoint,
        bucket=unique_bucket(),
        access_key_id=access_key,
        secret_access_key=secret_key,
        delete_namespace_on_close=delete_on_close,
    )


@pytest_asyncio.fixture
async def store():
    payload_store = S3PayloadStore(_config())
    yield payload_store
    await payload_store.close()


async def _raw_put(
    config: S3StoreConfig, path: str, body: bytes, *, conditional: bool
) -> int:
    """Signed PUT as an independent external writer (not the store)."""
    extra = {"If-None-Match": "*"} if conditional else None
    headers = s3_request_headers(
        config, "PUT", path, body=body, extra_headers=extra
    )
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.put(
            s3_url(config, path), headers=headers, content=body
        )
        return response.status_code


# ---------------------------------------------------------------------------
# Namespace isolation and lifecycle
# ---------------------------------------------------------------------------


async def test_namespaces_are_isolated() -> None:
    config_a, config_b = _config(), _config()
    store_a = S3PayloadStore(config_a)
    store_b = S3PayloadStore(config_b)
    try:
        blob = await store_a.stage(b"namespace-isolation")
        assert await store_a.object_exists(blob.blob_key)
        assert not await store_b.object_exists(blob.blob_key)
        check_b = await store_b.check_object(blob.blob_key)
        assert not check_b.exists
    finally:
        await store_a.close()
        await store_b.close()


async def test_close_empties_and_removes_test_namespace() -> None:
    config = _config()
    first = S3PayloadStore(config)
    blob = await first.stage(b"removed-on-close")
    await first.stage(b"second-object")
    await first.close()

    reopened = S3PayloadStore(
        # same namespace, but do not delete it this time: it must
        # already be gone, and lazy ensure recreates it empty
        S3StoreConfig(
            endpoint_url=config.endpoint_url,
            bucket=config.bucket,
            access_key_id=config.access_key_id,
            secret_access_key=config.secret_access_key,
            delete_namespace_on_close=True,
        )
    )
    try:
        assert await reopened.list_objects() == []
        assert not await reopened.object_exists(blob.blob_key)
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Conditional create is enforced by the server
# ---------------------------------------------------------------------------


async def test_server_rejects_competing_bytes_with_412(store) -> None:
    blob = await store.stage(b"original-bytes")
    status = await _raw_put(
        store._config, store._object_path(blob.blob_key), b"competing-bytes",
        conditional=True,
    )
    assert status == 412
    # the refusal left the original untouched and still verified
    check = await store.check_object(blob.blob_key)
    assert check.available and check.length == len(b"original-bytes")


async def test_tampered_occupant_is_unavailable_then_healed(store) -> None:
    blob = await store.stage(b"true-bytes")
    status = await _raw_put(
        store._config, store._object_path(blob.blob_key), b"tampered",
        conditional=False,
    )
    assert status == 200  # an external unconditional writer can corrupt

    corrupted = await store.check_object(blob.blob_key)
    assert corrupted.exists and not corrupted.available  # fail-closed view

    healed = await store.stage(b"true-bytes")
    assert healed.blob_key == blob.blob_key
    assert not healed.already_present  # verified replacement, not dedup
    assert (await store.check_object(blob.blob_key)).available


# ---------------------------------------------------------------------------
# Concurrency (barrier-controlled, not sleep-based)
# ---------------------------------------------------------------------------


async def test_concurrent_identical_stages_converge_with_one_creator(store) -> None:
    payload = b"converge-on-me" * 64
    start = asyncio.Event()

    async def publisher():
        await start.wait()
        return await store.stage(payload)

    tasks = [asyncio.create_task(publisher()) for _ in range(8)]
    start.set()  # release every writer onto the namespace together
    results = await asyncio.gather(*tasks)

    assert len({blob.blob_key for blob in results}) == 1
    creators = [blob for blob in results if not blob.already_present]
    assert len(creators) == 1  # exactly one conditional-create winner
    assert (await store.check_object(results[0].blob_key)).available


async def test_concurrent_distinct_stages_all_verified(store) -> None:
    payloads = [bytes([i]) * (i + 1) * 97 for i in range(1, 9)]
    start = asyncio.Event()

    async def publisher(data: bytes):
        await start.wait()
        return await store.stage(data)

    tasks = [asyncio.create_task(publisher(data)) for data in payloads]
    start.set()
    blobs = await asyncio.gather(*tasks)

    assert len({blob.blob_key for blob in blobs}) == len(payloads)
    listed = await store.list_objects()
    assert set(listed) == {blob.blob_key for blob in blobs}
    for blob in blobs:
        assert (await store.check_object(blob.blob_key)).available


# ---------------------------------------------------------------------------
# Ambiguity: a writer that never observed its own outcome
# ---------------------------------------------------------------------------


async def test_abandoned_writer_outcome_is_reobserved_and_converged(store) -> None:
    """Send a complete PUT, then discard the connection before reading
    the response — the crashed-writer window. The server may or may
    not have committed: that is exactly the ambiguity. The store's
    next stage of the same bytes must converge on verified truth
    either way instead of assuming the write failed."""
    payload = b"abandoned-writer-payload"
    blob_key = payload_byte_hash(payload)
    path = store._object_path(blob_key)
    headers = s3_request_headers(
        store._config, "PUT", path, body=payload,
        extra_headers={"If-None-Match": "*"},
    )
    async with httpx.AsyncClient(timeout=15) as client:
        request = client.build_request(
            "PUT", s3_url(store._config, path), headers=headers, content=payload
        )
        response = await client.send(request, stream=True)
        await response.aclose()  # outcome deliberately never observed

    # Whether the abandoned write committed is unknowable from the
    # client side. Probe existence without sleeping past it: if the
    # server did commit, the store's convergence MUST take the dedup
    # path (already_present=True); if it did not, stage creates it.
    visible = await _eventually_exists(store, blob_key)
    converged = await store.stage(payload)
    assert converged.blob_key == blob_key
    if visible:
        assert converged.already_present, (
            "server-side object was visible before stage, but stage "
            "re-created it instead of converging by verification"
        )
    assert (await store.check_object(blob_key)).available
    assert await store.read(blob_key) == payload


async def _eventually_exists(store, blob_key: str, attempts: int = 10) -> bool:
    """Poll briefly for an in-flight server commit to land."""
    for _ in range(attempts):
        if await store.object_exists(blob_key):
            return True
        await asyncio.sleep(0.05)
    return await store.object_exists(blob_key)


# ---------------------------------------------------------------------------
# Fresh-process reopen (no in-process truth)
# ---------------------------------------------------------------------------


async def test_fresh_process_reopens_the_namespace(store) -> None:
    payload = b"cross-process-truth"
    blob = await store.stage(payload)
    bucket = store._config.bucket

    env = {
        **os.environ,
        "MARKER_TEST_S3_BUCKET": bucket,
        "MARKER_TEST_S3_KEY": blob.blob_key,
        "MARKER_TEST_S3_PAYLOAD": payload.decode("utf-8"),
        "PYTHONPATH": str(
            __import__("pathlib").Path(__file__).resolve().parents[1]
        ),
    }
    result = subprocess.run(
        [sys.executable, "-c", _REOPEN_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "CHECK:available" in result.stdout
    assert "READ:match" in result.stdout


# ---------------------------------------------------------------------------
# Hygiene and failure closure
# ---------------------------------------------------------------------------


async def test_locators_are_credential_free(store) -> None:
    blob = await store.stage(b"locator-hygiene")
    config = store._config
    hex_digest = blob.blob_key.removeprefix("sha256:")
    # structural equality: the locator is exactly bucket/prefix/hash —
    # no credential field has anywhere to hide in it
    assert blob.locator == f"s3://{config.bucket}/{config.prefix}/{hex_digest}"
    assert _LOCATOR_PATTERN.match(blob.locator)
    assert config.secret_access_key not in blob.locator
    check = await store.check_object(blob.blob_key)
    assert check.locator == blob.locator


async def test_unreachable_endpoint_fails_closed() -> None:
    require_s3_env()  # keep the skip/strict contract even for negatives
    broken = S3PayloadStore(
        S3StoreConfig(
            endpoint_url="http://127.0.0.1:1",  # nothing listens here
            bucket=unique_bucket(),
            access_key_id="unsigned",
            secret_access_key="unsigned",
        )
    )
    try:
        with pytest.raises(PayloadStageError):
            await broken.stage(b"must-not-silently-succeed")
    finally:
        await broken.close()


async def test_empty_payload_roundtrip(store) -> None:
    blob = await store.stage(b"")
    assert blob.payload_length == 0
    check = await store.check_object(blob.blob_key)
    assert check.available and check.length == 0
    assert await store.read(blob.blob_key) == b""


async def test_delete_removes_from_namespace_view_and_restage_republishes(store) -> None:
    keep = await store.stage(b"keep-me")
    drop = await store.stage(b"drop-me")
    result = await store.delete_object(drop.blob_key)
    assert result.existed and result.deleted

    listed = await store.list_objects()
    assert keep.blob_key in listed and drop.blob_key not in listed

    republished = await store.stage(b"drop-me")
    assert republished.already_present is False
    assert (await store.check_object(drop.blob_key)).available
