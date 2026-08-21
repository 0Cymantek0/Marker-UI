"""Reusable payload-store behavioral conformance (PR83A WS2).

``KernelPayloadStore`` is the backend-neutral boundary the kernel
commit path depends on: content identity, idempotent publication,
verified availability, and cheap existence probes. This suite asserts
exactly that behavior and is parametrized over store implementations —
the local filesystem store today, an industrial object-store
implementation in PR83B — so any store the kernel accepts proves the
same semantics without kernel-side forks.

Adding an implementation: register a factory in ``STORE_FACTORIES``
(returning a fresh store over an isolated root/endpoint).
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
import pytest_asyncio

from app.kernel.payloads import (
    BLOB_KEY_PATTERN,
    KernelPayloadStore,
    LocalPayloadStore,
    PayloadMaintenanceStore,
)
from app.utils.canonical import payload_byte_hash
from tests.s3_provisioning import maybe_s3_store_factory

pytestmark = pytest.mark.asyncio


def _local_factory(root: pathlib.Path) -> LocalPayloadStore:
    return LocalPayloadStore(root)


#: Implementations that claim the kernel payload-store contract. An
#: industrial object-store implementation registers itself here and
#: inherits this whole suite (PR83B).
STORE_FACTORIES = {
    "local_file": _local_factory,
}

_s3_factory = maybe_s3_store_factory()
if _s3_factory is not None:
    STORE_FACTORIES["s3_minio"] = _s3_factory


@pytest.fixture(params=sorted(STORE_FACTORIES), ids=sorted(STORE_FACTORIES))
def store_name(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def payload_store(store_name: str, tmp_path: pathlib.Path):
    store = STORE_FACTORIES[store_name](tmp_path / f"store-{store_name}")
    assert isinstance(store, KernelPayloadStore)  # structural contract
    assert isinstance(store, PayloadMaintenanceStore)  # lifecycle contract
    yield store
    closer = getattr(store, "close", None)
    if closer is not None:
        result = closer()
        if asyncio.iscoroutine(result):
            await result


# ---------------------------------------------------------------------------
# Content identity
# ---------------------------------------------------------------------------


async def test_stage_yields_content_addressed_key(payload_store) -> None:
    blob = await payload_store.stage(b"identity-bytes")
    assert BLOB_KEY_PATTERN.match(blob.blob_key)
    assert blob.blob_key == payload_byte_hash(b"identity-bytes")
    assert blob.payload_length == len(b"identity-bytes")
    assert blob.locator


async def test_distinct_bytes_get_distinct_keys(payload_store) -> None:
    a = await payload_store.stage(b"aaa")
    b = await payload_store.stage(b"bbb")
    assert a.blob_key != b.blob_key


# ---------------------------------------------------------------------------
# Repeat writes of the same content
# ---------------------------------------------------------------------------


async def test_restage_same_bytes_is_idempotent(payload_store) -> None:
    first = await payload_store.stage(b"repeat-me")
    second = await payload_store.stage(b"repeat-me")
    assert second.blob_key == first.blob_key
    assert second.already_present
    assert second.payload_length == first.payload_length


# ---------------------------------------------------------------------------
# Existence / verification checks
# ---------------------------------------------------------------------------


async def test_check_object_verifies_staged_bytes(payload_store) -> None:
    blob = await payload_store.stage(b"verify-me")
    check = await payload_store.check_object(blob.blob_key)
    assert check.available
    assert check.exists and check.hash_ok and check.length_ok
    assert check.length == len(b"verify-me")
    assert check.locator == blob.locator


async def test_check_object_rejects_wrong_expected_length(payload_store) -> None:
    blob = await payload_store.stage(b"length-sensitive")
    check = await payload_store.check_object(
        blob.blob_key, expected_length=len(b"length-sensitive") + 1
    )
    assert not check.available
    assert check.exists and not check.length_ok


async def test_check_object_of_missing_key_is_not_available(payload_store) -> None:
    key = payload_byte_hash(b"never-staged")
    check = await payload_store.check_object(key)
    assert not check.available
    assert not check.exists


async def test_object_exists_distinguishes_presence(payload_store) -> None:
    blob = await payload_store.stage(b"present")
    missing = payload_byte_hash(b"absent")
    assert await payload_store.object_exists(blob.blob_key)
    assert not await payload_store.object_exists(missing)


# ---------------------------------------------------------------------------
# Maintenance metadata (GC accounting; PR83B1 WS6)
# ---------------------------------------------------------------------------


async def test_stat_object_reports_length_and_age_of_staged_bytes(
    payload_store,
) -> None:
    import time

    before = time.time()
    blob = await payload_store.stage(b"stat-me")
    stat = await payload_store.stat_object(blob.blob_key)
    assert stat is not None
    assert stat.blob_key == blob.blob_key
    assert stat.length == len(b"stat-me")
    # Age metadata comes from the store's own clock and may lag the
    # caller's; it must not predate the staging call.
    assert stat.last_modified_epoch >= before - 5.0


async def test_stat_object_of_missing_key_is_none(payload_store) -> None:
    missing = payload_byte_hash(b"never-existed")
    assert await payload_store.stat_object(missing) is None


async def test_stat_object_none_after_authorized_delete(payload_store) -> None:
    blob = await payload_store.stage(b"retire-me")
    result = await payload_store.delete_object(blob.blob_key)
    assert result.deleted
    assert await payload_store.stat_object(blob.blob_key) is None


# ---------------------------------------------------------------------------
# Failure behavior around the database visibility boundary
# ---------------------------------------------------------------------------


async def test_verified_object_survives_without_db_reference(payload_store) -> None:
    """Staging is one-sided: bytes are safe garbage until a commit
    references them, and remain independently verifiable afterwards."""
    blob = await payload_store.stage(b"orphan-safe")
    check = await payload_store.check_object(blob.blob_key)
    assert check.available


async def test_staging_after_physical_loss_republishes(payload_store) -> None:
    """PR65B rescue precondition: when bytes vanish between staging and
    the commit transaction, re-staging the exact bytes is always a
    valid republication."""
    blob = await payload_store.stage(b"rescue-me")
    destroyer = getattr(payload_store, "delete_object", None)
    if destroyer is None:  # implementation without retirement support
        pytest.skip("store has no physical deletion; rescue N/A")
    await destroyer(blob.blob_key)
    assert not await payload_store.object_exists(blob.blob_key)
    republished = await payload_store.stage(b"rescue-me")
    assert republished.blob_key == blob.blob_key
    assert (await payload_store.check_object(blob.blob_key)).available
