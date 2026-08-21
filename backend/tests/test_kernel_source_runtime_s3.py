"""Industrial-profile kernel runtime tests (V3.2 PR83B3).

Coordinator-level conformance of the kernel runtime over the S3 source
profile: authorization binds committed shared-topology revisions,
dispatch consumes verified materializations of those bytes, and every
unavailability fails closed —

* acquire -> authorize -> dispatch -> convert runs end to end on the
  shared object store, and the converter parses exactly the committed
  revision's bytes (a verified working copy, not the external path);
* mutating or deleting the original external source after submission
  cannot change what executes;
* a missing or tampered shared object terminal-fails honestly at
  launch — no fallback to any path authority;
* the industrial profile is fail-closed at submission and
  authorization: no legacy path-trust submissions, no adoption of
  unowned-path rows;
* restart with an empty node-local cache reconstructs a consumable
  input purely from durable shared truth;
* a corrupt local cache working copy is rebuilt from durable truth,
  never trusted (end-to-end through dispatch).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.task_manager as tm_module
from app.kernel.commit import KernelCommitService
from app.kernel.errors import KernelError
from app.kernel.models import KernelRecord
from app.kernel.object_store import S3StoreConfig
from app.kernel.source_object_store import S3SourceStore
from app.models.job import ConversionJob
from app.services.task_manager import TaskManager
from tests.s3_provisioning import require_s3_env, unique_bucket

pytestmark = pytest.mark.asyncio

PDF_A = b"%PDF-1.4 runtime revision A content for industrial dispatch"
PDF_B = b"%PDF-1.4 mutated external content that must never execute"


class RecordingConversionService:
    def __init__(self) -> None:
        self.parsed_paths: list[str] = []
        self.calls = 0

    def plan(self, filepath: str, config: dict[str, Any]) -> Any:
        import types

        return types.SimpleNamespace(execution_backend="cpu_thread")

    def supports_multiple_formats(self, filepath: str, config: dict[str, Any]) -> bool:
        return False

    def convert_file(self, filepath: str, config: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.parsed_paths.append(filepath)
        content = Path(filepath).read_bytes()
        return {
            "text": f"converted:{len(content)}",
            "extension": "md",
            "images": [],
            "metadata": {"pages": 1},
        }


def _build_store() -> S3SourceStore:
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


@pytest_asyncio.fixture
async def industrial_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.db_migration import upgrade_database

    roots = tmp_path / "roots"
    docs = roots / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots))
    monkeypatch.delenv("MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS", raising=False)

    url = f"sqlite+aiosqlite:///{(tmp_path / 'runtime.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(tm_module, "async_session_factory", factory)

    service = RecordingConversionService()
    store = _build_store()
    manager = TaskManager()
    coordinator = manager.start_kernel_runtime(
        service,
        session_factory=factory,
        commit_service=KernelCommitService(factory),
        source_store=store,
        source_cache_root=tmp_path / "cache",
        workspace_id="t",
        owner_id="test-industrial",
        lease_seconds=60.0,
        renew_interval_seconds=0.05,
        dispatch_poll_seconds=0.05,
        watchdog_interval_seconds=0.1,
        max_in_flight=4,
    )
    try:
        yield SimpleNamespace(
            manager=manager,
            coordinator=coordinator,
            factory=factory,
            service=service,
            store=store,
            docs=docs,
            tmp_path=tmp_path,
        )
    finally:
        coordinator.stop()
        manager.shutdown()
        await engine.dispose()
        await store.close()


async def _make_row(env, job_id: str, src: Path, config_extra: dict | None = None):
    config: dict[str, Any] = {
        "local_filepath": str(src),
        "output_format": "markdown",
        "original_name": src.name,
    }
    config.update(config_extra or {})
    async with env.factory() as session:
        session.add(
            ConversionJob(
                id=job_id,
                filename=src.name,
                original_name=src.name,
                status="pending",
                input_format="pdf",
                output_format="markdown",
                config_json=json.dumps(config),
                queue_backend="kernel",
            )
        )
        await session.commit()
    return config


async def _row_config(env, job_id: str) -> dict[str, Any]:
    async with env.factory() as session:
        row = await session.get(ConversionJob, job_id)
        return json.loads(row.config_json) if row and row.config_json else {}


async def _wait_status(env, job_id: str, *statuses: str, timeout: float = 25.0):
    import asyncio

    async def _poll():
        while True:
            async with env.factory() as session:
                row = await session.get(ConversionJob, job_id)
            if row and row.status in statuses:
                return row
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _raw_delete_object(store: S3SourceStore, blob_key: str, suffix: str) -> None:
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


async def _raw_put_object(
    store: S3SourceStore, blob_key: str, suffix: str, data: bytes
) -> None:
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


class TestIndustrialAuthorization:
    async def test_submit_acquires_binds_and_executes_shared_revision(
        self, industrial_env
    ):
        env = industrial_env
        src = env.docs / "doc.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-bind", src)

        work_id = await env.manager.submit_conversion(
            "job-bind", str(src), config, env.service
        )
        assert work_id is not None

        row_config = await _row_config(env, "job-bind")
        block = row_config["source_revision"]
        assert block["store_profile"] == "marker.kernel.source.s3.v1"
        # Shared profile: no node-local path is recorded as durable
        # executable state.
        assert "durable_filepath" not in row_config

        async with env.factory() as session:
            record = await session.get(KernelRecord, "conversion-request.job-bind")
        props = json.loads(record.payload_json)["properties"]
        assert props["source_revision"]["content_revision_id"] == block["content_revision_id"]

        env.coordinator.start()
        row = await _wait_status(env, "job-bind", "completed", "failed")
        assert row.status == "completed", row.error_message
        assert env.service.parsed_paths, "converter was never invoked"
        parsed = Path(env.service.parsed_paths[0])
        assert parsed.read_bytes() == PDF_A
        # The executed file is a verified materialization under the
        # node-local cache root, not the external source path.
        cache_root = (env.tmp_path / "cache").resolve()
        assert cache_root in parsed.resolve().parents

    async def test_authorize_refuses_path_trust_without_revision(self, industrial_env):
        env = industrial_env
        src = env.docs / "legacy.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-legacy", src)
        config.pop("source_revision", None)
        with pytest.raises(KernelError, match="requires a committed source revision"):
            await env.coordinator.authorize("job-legacy", config)

    async def test_submission_with_missing_source_fails_closed(self, industrial_env):
        env = industrial_env
        src = env.docs / "ghost.pdf"  # never written: the file is gone
        config = await _make_row(env, "job-ghost", src)
        with pytest.raises(KernelError, match="requires acquisition"):
            await env.manager.submit_conversion(
                "job-ghost", str(src), config, env.service
            )

    async def test_restart_does_not_adopt_unowned_path_rows(self, industrial_env):
        env = industrial_env
        src = env.docs / "old.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(
            env,
            "job-old",
            src,
            {"durable_filepath": str(src)},  # legacy shape, no revision block
        )
        report = await env.coordinator.recover()
        assert "job-old" not in report["adopted"]
        async with env.factory() as session:
            row = await session.get(ConversionJob, "job-old")
        assert row.status == "pending"  # honestly left, never path-trusted


class TestExecutionConsumesSharedRevision:
    async def test_worker_parses_materialized_revision_not_external_path(
        self, industrial_env
    ):
        env = industrial_env
        src = env.docs / "swap.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-swap", src)
        await env.manager.submit_conversion("job-swap", str(src), config, env.service)

        src.write_bytes(PDF_B)  # mutate the external source after submission

        env.coordinator.start()
        row = await _wait_status(env, "job-swap", "completed", "failed")
        assert row.status == "completed", row.error_message
        assert Path(env.service.parsed_paths[0]).read_bytes() == PDF_A

    async def test_external_source_deletion_after_submission_is_survivable(
        self, industrial_env
    ):
        env = industrial_env
        src = env.docs / "gone.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-gone", src)
        await env.manager.submit_conversion("job-gone", str(src), config, env.service)
        src.unlink()

        env.coordinator.start()
        row = await _wait_status(env, "job-gone", "completed", "failed")
        assert row.status == "completed", row.error_message
        assert Path(env.service.parsed_paths[0]).read_bytes() == PDF_A

    async def test_missing_shared_object_terminal_fails_at_launch(self, industrial_env):
        env = industrial_env
        src = env.docs / "lost.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-lost", src)
        await env.manager.submit_conversion("job-lost", str(src), config, env.service)

        block = (await _row_config(env, "job-lost"))["source_revision"]
        await _raw_delete_object(env.store, block["blob_key"], block["suffix"])

        env.coordinator.start()
        row = await _wait_status(env, "job-lost", "completed", "failed", "cancelled")
        assert row.status == "failed"
        assert "acquired source revision unavailable" in (row.error_message or "")

    async def test_tampered_shared_object_terminal_fails_at_launch(
        self, industrial_env
    ):
        env = industrial_env
        src = env.docs / "tainted.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-tainted", src)
        await env.manager.submit_conversion("job-tainted", str(src), config, env.service)

        block = (await _row_config(env, "job-tainted"))["source_revision"]
        tampered = bytearray(PDF_A)
        tampered[4] ^= 0xFF  # same size: only content verification catches it
        await _raw_put_object(
            env.store, block["blob_key"], block["suffix"], bytes(tampered)
        )

        env.coordinator.start()
        row = await _wait_status(env, "job-tainted", "completed", "failed", "cancelled")
        assert row.status == "failed"
        assert "acquired source revision unavailable" in (row.error_message or "")

    async def test_corrupt_local_cache_is_rebuilt_not_executed(self, industrial_env):
        env = industrial_env
        src = env.docs / "cached.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-cache-1", src)
        await env.manager.submit_conversion("job-cache-1", str(src), config, env.service)
        env.coordinator.start()
        row = await _wait_status(env, "job-cache-1", "completed", "failed")
        assert row.status == "completed", row.error_message
        cached = Path(env.service.parsed_paths[0])

        data = bytearray(cached.read_bytes())
        data[5] ^= 0xFF
        os.chmod(cached, 0o644)
        cached.write_bytes(bytes(data))

        config2 = await _make_row(env, "job-cache-2", src)
        await env.manager.submit_conversion("job-cache-2", str(src), config2, env.service)
        row2 = await _wait_status(env, "job-cache-2", "completed", "failed")
        assert row2.status == "completed", row2.error_message
        assert cached.read_bytes() == PDF_A  # rebuilt from durable truth
        assert Path(env.service.parsed_paths[1]).read_bytes() == PDF_A

    async def test_restart_with_empty_cache_resolves_from_shared_truth(
        self, industrial_env
    ):
        env = industrial_env
        src = env.docs / "restart.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-restart", src)
        await env.manager.submit_conversion("job-restart", str(src), config, env.service)

        # Simulate a replacement node: fresh coordinator, same durable
        # database + object store, EMPTY node-local cache, original
        # external source gone.
        env.coordinator.stop()
        env.manager.shutdown()
        src.unlink()

        fresh_cache = env.tmp_path / "cache-fresh-node"
        service2 = RecordingConversionService()
        manager2 = TaskManager()
        coordinator2 = manager2.start_kernel_runtime(
            service2,
            session_factory=env.factory,
            commit_service=KernelCommitService(env.factory),
            source_store=env.store,
            source_cache_root=fresh_cache,
            workspace_id="t",
            owner_id="replacement-node",
            lease_seconds=60.0,
            renew_interval_seconds=0.05,
            dispatch_poll_seconds=0.05,
            watchdog_interval_seconds=0.1,
            max_in_flight=4,
        )
        try:
            await coordinator2.recover()
            coordinator2.start()
            row = await _wait_status(env, "job-restart", "completed", "failed")
            assert row.status == "completed", row.error_message
            assert service2.parsed_paths, "converter was never invoked"
            parsed = Path(service2.parsed_paths[0])
            assert parsed.read_bytes() == PDF_A
            assert fresh_cache.resolve() in parsed.resolve().parents
        finally:
            coordinator2.stop()
            manager2.shutdown()
