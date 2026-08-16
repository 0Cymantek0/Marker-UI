"""Runtime source-truth binding tests (PR70/71 local slice, plan §6.6/§13).

Submission → acquisition → authorization → fenced execution over the
real coordinator: authorized work references committed source truth,
the worker consumes the revision's immutable artifact (never the
external path), and restart/retry converge without re-trusting a
changed external source.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitService
from app.kernel.models import KernelRecord, KernelRecordEdge
from app.kernel.source_store import LocalSourceStore
from app.models.job import ConversionJob
from app.services import task_manager as tm_module
from app.services.task_manager import TaskManager
from app.services.source_acquisition import SOURCE_CONFIG_KEY

pytestmark = pytest.mark.asyncio

PDF_A = b"%PDF-1.4 runtime revision A body"
PDF_B = b"%PDF-1.4 runtime revision B body (different!)"


class RecordingConversionService:
    """Fake converter that records which file path it actually parsed."""

    def __init__(self) -> None:
        self.parsed_paths: list[str] = []
        self.calls = 0

    def plan(self, filepath: str, config: dict[str, Any]) -> Any:
        return SimpleNamespace(execution_backend="cpu_thread")

    def supports_multiple_formats(self, filepath: str, config: dict[str, Any]) -> bool:
        return False

    def convert_file(self, filepath: str, config: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.parsed_paths.append(filepath)
        content = Path(filepath).read_bytes()
        return {
            "text": f"converted:{len(content)}:{content[:20].decode('latin-1')}",
            "extension": "md",
            "images": [],
            "metadata": {"pages": 1},
        }


@pytest_asyncio.fixture
async def source_runtime_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    store = LocalSourceStore(tmp_path / "source_store")
    manager = TaskManager()
    coordinator = manager.start_kernel_runtime(
        service,
        session_factory=factory,
        commit_service=KernelCommitService(factory),
        source_store=store,
        workspace_id="t",
        owner_id="test-runtime",
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


class TestAuthorizedWorkReferencesSourceTruth:
    async def test_submit_acquires_and_binds_revision(self, source_runtime_env):
        env = source_runtime_env
        src = env.docs / "doc.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-bind", src)

        work_id = await env.manager.submit_conversion(
            "job-bind", str(src), config, env.service
        )

        assert work_id is not None
        row_config = await _row_config(env, "job-bind")
        block = row_config[SOURCE_CONFIG_KEY]
        assert block["blob_key"].startswith("sha256:")
        assert block["consistency_class"] == "stable_handle"

        # the authorized request record carries the revision refs + edge
        async with env.factory() as session:
            record = await session.get(KernelRecord, "conversion-request.job-bind")
            edge = (
                await session.execute(
                    select(KernelRecordEdge).where(
                        KernelRecordEdge.source_record_id == "conversion-request.job-bind"
                    )
                )
            ).scalars().first()
        assert record is not None
        props = json.loads(record.payload_json)["properties"]
        assert props["source_revision"]["content_revision_id"] == block["content_revision_id"]
        assert edge is not None
        assert edge.edge_kind == "depends_on"
        assert edge.target_record_id == block["content_revision_id"]

    async def test_duplicate_submission_converges(self, source_runtime_env):
        env = source_runtime_env
        src = env.docs / "dup.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-dup", src)

        first = await env.manager.submit_conversion("job-dup", str(src), config, env.service)
        second = await env.manager.submit_conversion("job-dup", str(src), config, env.service)

        assert second == first
        async with env.factory() as session:
            count = len(
                (
                    await session.execute(
                        select(KernelRecord.id).where(
                            KernelRecord.workspace_id == "t",
                            KernelRecord.record_class == "content_revision",
                        )
                    )
                ).scalars().all()
            )
        assert count == 1


class TestExecutionConsumesAcquiredRevision:
    async def test_worker_parses_artifact_not_external_path(self, source_runtime_env):
        env = source_runtime_env
        src = env.docs / "swap.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-swap", src)
        await env.manager.submit_conversion("job-swap", str(src), config, env.service)

        # Mutate the external source AFTER authorization, BEFORE dispatch.
        src.write_bytes(PDF_B)

        env.coordinator.start()
        row = await _wait_status(env, "job-swap", "completed", "failed")

        assert row.status == "completed", row.error_message
        assert env.service.parsed_paths, "converter was never invoked"
        parsed = Path(env.service.parsed_paths[0])
        assert parsed.read_bytes() == PDF_A  # revision A, not mutated B
        store_root = (env.tmp_path / "source_store").resolve()
        assert store_root in parsed.resolve().parents  # executed the artifact

    async def test_external_source_disappearing_after_authorization_is_survivable(
        self, source_runtime_env
    ):
        env = source_runtime_env
        src = env.docs / "gone.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-gone", src)
        await env.manager.submit_conversion("job-gone", str(src), config, env.service)

        src.unlink()

        env.coordinator.start()
        row = await _wait_status(env, "job-gone", "completed", "failed")
        assert row.status == "completed", row.error_message

    async def test_missing_artifact_terminal_fails_honestly(self, source_runtime_env):
        env = source_runtime_env
        src = env.docs / "lost-artifact.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-lost", src)
        await env.manager.submit_conversion("job-lost", str(src), config, env.service)

        row_config = await _row_config(env, "job-lost")
        block = row_config[SOURCE_CONFIG_KEY]
        artifact = env.store.artifact_path(block["blob_key"], block["suffix"])
        artifact.chmod(0o644)
        artifact.unlink()
        src.unlink()  # no external fallback exists

        env.coordinator.start()
        row = await _wait_status(env, "job-lost", "completed", "failed", "cancelled")
        assert row.status == "failed"
        assert "acquired source revision unavailable" in (row.error_message or "")

    async def test_truncated_artifact_terminal_fails_honestly(self, source_runtime_env):
        env = source_runtime_env
        src = env.docs / "trunc-artifact.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-trunc", src)
        await env.manager.submit_conversion("job-trunc", str(src), config, env.service)

        row_config = await _row_config(env, "job-trunc")
        block = row_config[SOURCE_CONFIG_KEY]
        artifact = env.store.artifact_path(block["blob_key"], block["suffix"])
        artifact.chmod(0o644)
        artifact.write_bytes(PDF_A[:3])

        env.coordinator.start()
        row = await _wait_status(env, "job-trunc", "completed", "failed", "cancelled")
        assert row.status == "failed"
        assert "acquired source revision unavailable" in (row.error_message or "")


class TestAuthorizeValidation:
    async def test_forged_block_reacquires_rather_than_authorizing_fiction(
        self, source_runtime_env
    ):
        env = source_runtime_env
        src = env.docs / "forged.pdf"
        src.write_bytes(PDF_A)
        forged_block = {
            "source_id": "source.doesnotexist",
            "content_revision_id": "content.doesnotexist",
            "access_policy_id": "access.doesnotexist",
            "authorization_epoch": 99,
            "blob_key": "sha256:" + "0" * 64,
            "byte_length": 1,
            "consistency_class": "stable_handle",
            "media_type": "application/pdf",
            "suffix": ".pdf",
        }
        config = await _make_row(
            env, "job-forged", src, {SOURCE_CONFIG_KEY: forged_block}
        )

        work_id = await env.manager.submit_conversion(
            "job-forged", str(src), config, env.service
        )
        assert work_id is not None

        row_config = await _row_config(env, "job-forged")
        block = row_config[SOURCE_CONFIG_KEY]
        assert block["content_revision_id"] != "content.doesnotexist"
        assert block["blob_key"].startswith("sha256:")

    async def test_authorize_rejects_unresolvable_block_without_file(self, source_runtime_env):
        from app.kernel.errors import KernelError

        env = source_runtime_env
        forged_block = {
            "source_id": "source.missing",
            "content_revision_id": "content.missing",
            "access_policy_id": "access.missing",
            "authorization_epoch": 1,
            "blob_key": "sha256:" + "1" * 64,
            "byte_length": 1,
            "consistency_class": "stable_handle",
            "media_type": "application/pdf",
            "suffix": ".pdf",
        }
        with pytest.raises(KernelError, match="does not resolve"):
            await env.coordinator.authorize(
                "job-ghost", {SOURCE_CONFIG_KEY: forged_block}
            )


class TestRestartRecovery:
    async def test_restart_executes_acquired_revision_with_external_source_changed(
        self, source_runtime_env
    ):
        env = source_runtime_env
        src = env.docs / "restart.pdf"
        src.write_bytes(PDF_A)
        config = await _make_row(env, "job-restart", src)
        await env.manager.submit_conversion("job-restart", str(src), config, env.service)

        # "Crash": stop dispatch, mutate the external world, then restart.
        env.coordinator.stop()
        src.write_bytes(PDF_B)

        recovered = await env.coordinator.recover()
        assert "job-restart" not in recovered["swept"]
        env.coordinator.start()
        row = await _wait_status(env, "job-restart", "completed", "failed")
        assert row.status == "completed", row.error_message
        parsed = Path(env.service.parsed_paths[0])
        assert parsed.read_bytes() == PDF_A
