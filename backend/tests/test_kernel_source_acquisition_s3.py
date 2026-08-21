"""Industrial-profile source acquisition tests (V3.2 PR83B3).

Service-level conformance of :class:`SourceAcquisitionService` over the
real S3 source store: committed kernel truth keeps its PR70/71 identity
semantics, while resolution, availability, and consumption become
topology-honest —

* the config block self-describes its store profile, and ``resolve``
  never crosses profiles (a local block is not reinterpret as
  shared-topology-available, and vice versa);
* availability follows the shared object (missing/truncated = honestly
  unresolvable), never a node-local path;
* ``consumable_path_for`` materializes verified working copies, reuses
  them only after full content verification, and rebuilds — never
  trusts — a corrupt local cache;
* identity semantics survive the industrial path unchanged (same
  source + changed bytes = new revision; identical bytes converge).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitService
from app.kernel.object_store import S3StoreConfig
from app.kernel.source_object_store import S3SourceStore
from app.kernel.source_store import (
    LOCAL_SOURCE_STORE_PROFILE,
    LocalSourceStore,
    SourceStoreError,
)
from app.services.source_acquisition import SourceAcquisitionService
from tests.s3_provisioning import require_s3_env, unique_bucket

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-src-s3"

PDF_A = b"%PDF-1.4 industrial revision A content"
PDF_B = b"%PDF-1.4 industrial revision B content (different bytes)"


@pytest_asyncio.fixture
async def s3_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.db_migration import upgrade_database

    roots_dir = tmp_path / "roots"
    docs = roots_dir / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots_dir))
    monkeypatch.delenv("MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS", raising=False)

    url = f"sqlite+aiosqlite:///{(tmp_path / 'source.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    endpoint, access_key, secret_key = require_s3_env()
    store = S3SourceStore(
        S3StoreConfig(
            endpoint_url=endpoint,
            bucket=unique_bucket(),
            access_key_id=access_key,
            secret_access_key=secret_key,
            prefix="kernel-sources",
            delete_namespace_on_close=True,
        )
    )
    commit_service = KernelCommitService(factory)
    service = SourceAcquisitionService(
        factory,
        commit_service,
        store,
        workspace_id=WORKSPACE,
        cache_root=tmp_path / "cache",
    )
    try:
        yield SimpleNamespace(
            factory=factory,
            store=store,
            commit_service=commit_service,
            service=service,
            docs=docs,
            tmp_path=tmp_path,
        )
    finally:
        await engine.dispose()
        await store.close()


async def _acquire(env, data: bytes, name: str = "doc.pdf"):
    src = env.docs / name
    src.write_bytes(data)
    return await env.service.acquire(
        src, source_kind="local_path", suffix=".pdf", job_id=""
    )


class TestIndustrialAcquisition:
    async def test_acquire_commits_profiled_revision(self, s3_env):
        acquired = await _acquire(s3_env, PDF_A)
        assert acquired.store_profile == "marker.kernel.source.s3.v1"
        assert acquired.blob_key.startswith("sha256:")
        block = acquired.to_config()
        assert block["store_profile"] == "marker.kernel.source.s3.v1"

        resolved = await s3_env.service.resolve(block)
        assert resolved is not None
        assert resolved.content_revision_id == acquired.content_revision_id
        assert resolved.store_profile == acquired.store_profile

    async def test_resolve_is_profile_gated(self, s3_env, tmp_path):
        acquired = await _acquire(s3_env, PDF_A)
        block = acquired.to_config()

        # A local-profile runtime cannot vouch for shared-topology bytes.
        local_service = SourceAcquisitionService(
            s3_env.factory,
            s3_env.commit_service,
            LocalSourceStore(tmp_path / "local_store"),
            workspace_id=WORKSPACE,
        )
        assert await local_service.resolve(block) is None
        assert local_service.legacy_submit_fallback is True
        assert s3_env.service.legacy_submit_fallback is False

        # A legacy block (no store_profile) committed by a pre-PR83B3
        # local runtime is local-by-construction: the industrial
        # runtime must refuse it rather than read a node-local path.
        legacy_block = {k: v for k, v in block.items() if k != "store_profile"}
        assert await s3_env.service.resolve(legacy_block) is None

        # Garbage profiles never validate into an envelope at all.
        forged = dict(block, store_profile="marker.kernel.source.tape.v1")
        assert await s3_env.service.resolve(forged) is None

    async def test_missing_object_is_unresolvable(self, s3_env):
        acquired = await _acquire(s3_env, PDF_A)
        # Destroy the durable object (simulated operational loss).
        await _raw_delete(s3_env.store, acquired.blob_key, ".pdf")
        assert await s3_env.service.resolve(acquired.to_config()) is None

    async def test_truncated_object_is_unresolvable(self, s3_env):
        acquired = await _acquire(s3_env, PDF_A)
        await _raw_put_bytes(s3_env.store, acquired.blob_key, ".pdf", PDF_A[:-4])
        assert await s3_env.service.resolve(acquired.to_config()) is None

    async def test_identity_semantics_survive_industrial_path(self, s3_env):
        first = await _acquire(s3_env, PDF_A)
        (s3_env.docs / "doc.pdf").write_bytes(PDF_B)
        second = await _acquire(s3_env, PDF_B)
        assert second.source_id == first.source_id
        assert second.content_revision_id != first.content_revision_id
        assert second.blob_key != first.blob_key

        # Identical bytes re-acquired converge onto committed truth.
        (s3_env.docs / "doc.pdf").write_bytes(PDF_A)
        again = await _acquire(s3_env, PDF_A)
        assert again.content_revision_id == first.content_revision_id
        assert s3_env.store.dedup_hits == 1


class TestConsumableMaterialization:
    async def test_consumable_path_is_verified_working_copy(self, s3_env):
        acquired = await _acquire(s3_env, PDF_A)
        path = await s3_env.service.consumable_path_for(acquired)
        assert path.is_file()
        assert path.read_bytes() == PDF_A
        # The working copy lives under the cache root, not a source store.
        assert str(s3_env.tmp_path / "cache") in str(path)

    async def test_second_consumption_reuses_verified_cache(self, s3_env):
        acquired = await _acquire(s3_env, PDF_A)
        first = await s3_env.service.consumable_path_for(acquired)
        second = await s3_env.service.consumable_path_for(acquired)
        assert first == second
        materializer = s3_env.service._materializer
        assert materializer.materializations == 1
        assert materializer.cache_hits == 1
        assert materializer.cache_rebuilds == 0

    async def test_corrupt_cache_is_rebuilt_not_trusted(self, s3_env):
        acquired = await _acquire(s3_env, PDF_A)
        path = await s3_env.service.consumable_path_for(acquired)

        # Corrupt the local working copy (same size, different bytes):
        # it must never be handed to a converter as-is.
        data = bytearray(path.read_bytes())
        data[5] ^= 0xFF
        import os

        os.chmod(path, 0o644)
        path.write_bytes(bytes(data))

        rebuilt = await s3_env.service.consumable_path_for(acquired)
        assert rebuilt == path
        assert path.read_bytes() == PDF_A
        materializer = s3_env.service._materializer
        assert materializer.cache_rebuilds == 1

    async def test_missing_shared_object_fails_closed_at_consumption(
        self, s3_env
    ):
        acquired = await _acquire(s3_env, PDF_A)
        await _raw_delete(s3_env.store, acquired.blob_key, ".pdf")
        with pytest.raises(SourceStoreError):
            await s3_env.service.consumable_path_for(acquired)

    async def test_tampered_shared_object_fails_closed_at_consumption(
        self, s3_env
    ):
        acquired = await _acquire(s3_env, PDF_A)
        tampered = bytearray(PDF_A)
        tampered[3] ^= 0xFF
        await _raw_put_bytes(s3_env.store, acquired.blob_key, ".pdf", bytes(tampered))
        with pytest.raises(SourceStoreError):
            await s3_env.service.consumable_path_for(acquired)


class TestExecutionLocator:
    async def test_local_locator_is_path_and_s3_locator_is_object_uri(
        self, s3_env, tmp_path
    ):
        acquired = await _acquire(s3_env, PDF_A)
        s3_locator = s3_env.service.execution_locator_for(acquired)
        assert s3_locator.startswith("s3://")
        assert "kernel-sources" in s3_locator

        local_service = SourceAcquisitionService(
            s3_env.factory,
            s3_env.commit_service,
            LocalSourceStore(tmp_path / "local_store"),
            workspace_id=WORKSPACE,
        )
        assert local_service.store_profile == LOCAL_SOURCE_STORE_PROFILE
        local_acquired = await local_service.acquire(
            s3_env.docs / "doc.pdf",
            source_kind="local_path",
            suffix=".pdf",
        )
        local_locator = local_service.execution_locator_for(local_acquired)
        assert not local_locator.startswith("s3://")
        assert Path(local_locator).is_file()


# ---------------------------------------------------------------------------
# raw object-store writers (independent of the store under test)
# ---------------------------------------------------------------------------


async def _raw_put_bytes(store: S3SourceStore, blob_key: str, suffix: str, data: bytes) -> None:
    import httpx

    from app.kernel.object_store import s3_request_headers, s3_url

    config = store._config
    hex_digest = blob_key.removeprefix("sha256:")
    path = f"/{config.bucket}/{config.prefix}/{hex_digest[:2]}/{hex_digest}{suffix}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(
            s3_url(config, path),
            headers=s3_request_headers(config, "PUT", path, body=data),
            content=data,
        )
        assert response.status_code in (200, 201), response.text


async def _raw_delete(store: S3SourceStore, blob_key: str, suffix: str) -> None:
    import httpx

    from app.kernel.object_store import s3_request_headers, s3_url

    config = store._config
    hex_digest = blob_key.removeprefix("sha256:")
    path = f"/{config.bucket}/{config.prefix}/{hex_digest[:2]}/{hex_digest}{suffix}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            s3_url(config, path),
            headers=s3_request_headers(config, "DELETE", path),
        )
        assert response.status_code in (200, 204, 404), response.text
