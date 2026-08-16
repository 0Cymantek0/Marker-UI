"""Source acquisition service tests (PR70/71 local slice).

End-to-end identity matrix over the real kernel commit spine, migrated
file-backed SQLite, and the content-addressed source store — the
commit-durable counterpart of the pure record-identity tests.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.models import KernelRecord
from app.kernel.source_store import IncoherentSourceError, LocalSourceStore
from app.services.source_acquisition import SourceAcquisitionService

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-src"

PDF_A = b"%PDF-1.4 revision A content"
PDF_B = b"%PDF-1.4 revision B content (different page count)"


@pytest_asyncio.fixture
async def source_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    store = LocalSourceStore(tmp_path / "source_store")
    commit_service = KernelCommitService(factory)
    service = SourceAcquisitionService(
        factory, commit_service, store, workspace_id=WORKSPACE
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


async def _record_classes(env) -> Counter:
    async with env.factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord.record_class).where(
                    KernelRecord.workspace_id == WORKSPACE
                )
            )
        ).scalars().all()
    return Counter(rows)


async def _acquire(env, path: Path, data: bytes, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    kwargs.setdefault("source_kind", "local_path")
    kwargs.setdefault("suffix", ".pdf")
    return await env.service.acquire(path, **kwargs)


class TestAcquisitionHappyPath:
    async def test_acquire_commits_full_identity_chain(self, source_env):
        env = source_env
        acquired = await _acquire(env, env.docs / "report.pdf", PDF_A, job_id="j1")

        assert acquired.blob_key.startswith("sha256:")
        assert acquired.consistency_class == "stable_handle"
        assert acquired.byte_length == len(PDF_A)
        assert acquired.authorization_epoch == 1

        counts = await _record_classes(env)
        assert counts["source_identity"] == 1
        assert counts["content_revision"] == 1
        assert counts["access_policy_revision"] == 1
        assert counts["authorization_epoch"] == 1
        assert counts["source_observation"] == 1

        artifact = env.store.artifact_path(acquired.blob_key, ".pdf")
        assert artifact.is_file()
        assert artifact.read_bytes() == PDF_A

        assert await env.service.resolve(acquired.to_config()) == acquired

    async def test_resolve_rejects_malformed_blocks(self, source_env):
        env = source_env
        assert await env.service.resolve({}) is None
        assert await env.service.resolve({"blob_key": "sha256:" + "0" * 64}) is None

    async def test_resolve_rejects_block_disagreeing_with_committed_record(
        self, source_env
    ):
        env = source_env
        acquired = await _acquire(env, env.docs / "r.pdf", PDF_A)
        forged = dict(acquired.to_config())
        forged["byte_length"] = 3
        assert await env.service.resolve(forged) is None


class TestIdentityMatrix:
    async def test_reacquire_same_source_same_bytes_converges(self, source_env):
        env = source_env
        first = await _acquire(env, env.docs / "same.pdf", PDF_A, job_id="j1")
        second = await _acquire(env, env.docs / "same.pdf", PDF_A, job_id="j2")

        assert second.source_id == first.source_id
        assert second.content_revision_id == first.content_revision_id
        assert second.access_policy_id == first.access_policy_id
        counts = await _record_classes(env)
        assert counts["content_revision"] == 1
        # two acquisition events remain inspectable history
        assert counts["source_observation"] == 2

    async def test_changed_bytes_mints_new_revision_same_source(self, source_env):
        env = source_env
        first = await _acquire(env, env.docs / "evolve.pdf", PDF_A)
        second = await _acquire(env, env.docs / "evolve.pdf", PDF_B)

        assert second.source_id == first.source_id
        assert second.content_revision_id != first.content_revision_id
        assert second.blob_key != first.blob_key
        counts = await _record_classes(env)
        assert counts["source_identity"] == 1
        assert counts["content_revision"] == 2

    async def test_policy_only_change_keeps_content_identity(self, source_env, monkeypatch):
        env = source_env
        first = await _acquire(env, env.docs / "pol.pdf", PDF_A)
        # Widen the permitted root: same file, same bytes, new policy fact
        # (permitted_root differs) — access identity must change, content
        # identity must not, and the epoch must advance.
        monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(env.tmp_path))

        second = await _acquire(env, env.docs / "pol.pdf", PDF_A)

        assert second.content_revision_id == first.content_revision_id
        assert second.blob_key == first.blob_key
        assert second.access_policy_id != first.access_policy_id
        assert second.authorization_epoch == first.authorization_epoch + 1
        counts = await _record_classes(env)
        assert counts["content_revision"] == 1
        assert counts["access_policy_revision"] == 2

    async def test_identical_bytes_two_sources_stay_distinct(self, source_env):
        env = source_env
        a = await _acquire(env, env.docs / "a.pdf", PDF_A, job_id="j1")
        b = await _acquire(env, env.docs / "b.pdf", PDF_A, job_id="j2")

        assert a.source_id != b.source_id
        assert a.content_revision_id != b.content_revision_id
        # physical bytes dedup: one artifact serves both revisions
        assert a.blob_key == b.blob_key
        assert env.store.dedup_hits == 1
        assert len(await env.store.list_artifacts()) == 1

    async def test_upload_occurrences_are_distinct_logical_sources(self, source_env):
        env = source_env
        upload_a = env.tmp_path / "uploads" / "a.pdf"
        upload_b = env.tmp_path / "uploads" / "b.pdf"
        a = await _acquire(env, upload_a, PDF_A, source_kind="upload", job_id="job-a")
        b = await _acquire(env, upload_b, PDF_A, source_kind="upload", job_id="job-b")

        assert a.source_id != b.source_id
        assert a.blob_key == b.blob_key

    async def test_url_source_requires_explicit_key(self, source_env):
        env = source_env
        src = env.tmp_path / "downloads" / "doc.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(PDF_A)
        from app.kernel.source_store import SourceStoreError

        with pytest.raises(SourceStoreError):
            await env.service.acquire(src, source_kind="url", suffix=".pdf")
        url_acquired = await env.service.acquire(
            src,
            source_kind="url",
            suffix=".pdf",
            source_key_override="url:https://example.com/doc.pdf",
        )
        assert url_acquired.consistency_class == "best_effort_consistent"


class TestRejectionAndPolicy:
    async def test_mutation_during_acquisition_records_rejection_only(self, source_env):
        env = source_env
        src = env.docs / "attack.pdf"
        src.write_bytes(b"M" * (2 * 1024 * 1024))

        def mutate() -> None:
            with open(src, "r+b") as handle:
                handle.seek(0)
                handle.write(b"X" * 4096)

        with pytest.raises(IncoherentSourceError):
            await env.service.acquire(
                src,
                source_kind="local_path",
                suffix=".pdf",
                job_id="j-attack",
                hooks={"during-read": mutate},
            )

        counts = await _record_classes(env)
        assert counts["source_observation"] == 1
        assert "content_revision" not in counts
        # the rejection is inspectable durable history
        async with env.factory() as session:
            rows = (
                await session.execute(
                    select(KernelRecord.payload_json).where(
                        KernelRecord.workspace_id == WORKSPACE,
                        KernelRecord.record_class == "source_observation",
                    )
                )
            ).scalars().all()
        obs = [json.loads(r) for r in rows]
        assert obs[0]["outcome"] == "rejected_incoherent"
        assert obs[0]["content_revision_ref"] is None
        assert len(await env.store.list_artifacts()) == 0

    async def test_local_path_outside_permitted_roots_rejected(self, source_env):
        env = source_env
        outside = env.tmp_path / "outside" / "secret.pdf"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(PDF_A)
        from app.errors import InputNotAllowedError

        with pytest.raises(InputNotAllowedError):
            await env.service.acquire(outside, source_kind="local_path", suffix=".pdf")
        assert (await _record_classes(env)).get("source_observation") is None


class TestResolveAndRestart:
    async def test_resolve_survives_external_file_change_and_disappearance(
        self, source_env
    ):
        env = source_env
        src = env.docs / "gone.pdf"
        acquired = await _acquire(env, src, PDF_A)

        src.write_bytes(b"mutated after acquisition")
        assert await env.service.resolve(acquired.to_config()) is not None

        src.unlink()
        resolved = await env.service.resolve(acquired.to_config())
        assert resolved == acquired
        # owned bytes are still executable truth
        artifact = await env.service.artifact_path_for(resolved)
        assert artifact.read_bytes() == PDF_A

    async def test_resolve_fails_honestly_when_artifact_missing(self, source_env):
        env = source_env
        acquired = await _acquire(env, env.docs / "lost.pdf", PDF_A)
        artifact = env.store.artifact_path(acquired.blob_key, ".pdf")
        artifact.chmod(0o644)
        artifact.unlink()
        assert await env.service.resolve(acquired.to_config()) is None

    async def test_resolve_fails_when_artifact_truncated(self, source_env):
        env = source_env
        acquired = await _acquire(env, env.docs / "trunc.pdf", PDF_A)
        artifact = env.store.artifact_path(acquired.blob_key, ".pdf")
        artifact.chmod(0o644)
        artifact.write_bytes(PDF_A[:5])
        assert await env.service.resolve(acquired.to_config()) is None


class TestConcurrencyAndCrash:
    async def test_concurrent_duplicate_acquisition_converges(self, source_env):
        env = source_env
        src = env.docs / "race.pdf"
        src.write_bytes(PDF_A)

        results = await asyncio.gather(
            *[
                env.service.acquire(
                    src, source_kind="local_path", suffix=".pdf", job_id=f"j-{i}"
                )
                for i in range(4)
            ]
        )

        revision_ids = {r.content_revision_id for r in results}
        assert len(revision_ids) == 1
        counts = await _record_classes(env)
        assert counts["content_revision"] == 1
        assert counts["source_identity"] == 1
        assert counts["source_observation"] == 4

    async def test_crash_between_staging_and_commit_leaves_no_source_truth(
        self, source_env, tmp_path: Path
    ):
        """Commit fault after staging: bytes exist, no revision committed."""
        env = source_env
        src = env.docs / "crash.pdf"
        src.write_bytes(PDF_A)

        faulted = KernelCommitService(env.factory)
        original = faulted.commit

        async def commit_with_fault(batch: KernelCommitBatch, **kwargs):
            kwargs["_inject_fault_at"] = "pre-commit"
            return await original(batch, **kwargs)

        faulted.commit = commit_with_fault  # type: ignore[method-assign]
        faulted_service = SourceAcquisitionService(
            env.factory, faulted, env.store, workspace_id=WORKSPACE
        )
        with pytest.raises(Exception):
            await faulted_service.acquire(src, source_kind="local_path", suffix=".pdf")

        assert "content_revision" not in (await _record_classes(env))
        # staged bytes are unreferenced residue, not truth
        assert len(await env.store.list_artifacts()) == 1

        # retry after the crash converges onto a real commit
        recovered = await env.service.acquire(
            src, source_kind="local_path", suffix=".pdf"
        )
        assert (await _record_classes(env))["content_revision"] == 1
        assert recovered.blob_key.startswith("sha256:")
