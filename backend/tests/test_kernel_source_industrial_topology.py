"""Industrial source-topology acceptance proofs (V3.2 PR83B3).

The process-boundary claim this phase exists to make true, attacked
with real OS processes rather than in-process fakes:

* **process A** acquires a source into the industrial profile against
  real backing services (real S3-compatible object store; real
  PostgreSQL when provisioned, SQLite otherwise) and commits the
  source revision, then exits;
* **process A's node-local state is destroyed** — original external
  source file deleted, source-store root and materialization cache
  removed;
* **process B** starts with different empty node-local directories and
  ONLY the durable configuration (database + object store), resolves
  the committed revision, verifies it, and reaches the converter-facing
  consumption boundary with the exact committed bytes.

The test fails if the implementation secretly depends on process A's
``LocalSourceStore`` directory, its materialization cache, or the
original external source path.

Additionally proven here:

* crash windows: an object staged but never kernel-committed is not
  semantic truth, and retry converges onto it safely (dedup), on both
  the S3 profile and the local profile;
* payload/source ownership separation at kernel-truth level: a source
  acquisition commits only source-record classes — no payload
  registry rows exist that could ever authorize payload GC against
  source artifacts, and the payload namespace listing cannot see them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitService
from app.kernel.errors import InjectedFaultError
from app.kernel.models import KernelRecord
from app.kernel.object_store import S3PayloadStore, S3StoreConfig
from app.kernel.source_object_store import (
    PHASE_AFTER_VERIFY,
    S3SourceStore,
)
from app.kernel.source_store import LocalSourceStore
from app.services.source_acquisition import SourceAcquisitionService
from tests.pg_provisioning import BACKENDS, provisioned_database
from tests.s3_provisioning import require_s3_env, unique_bucket

pytestmark = pytest.mark.asyncio

PDF_A = b"%PDF-1.4 cross-process industrial revision A payload\n\x25\xe2\xe3\xcf\xd3\n"

_BACKEND_ROOT = str(Path(__file__).resolve().parent.parent)

_PROCESS_PROBE = r"""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["MARKER_TEST_BACKEND_ROOT"])


async def main() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.kernel.commit import KernelCommitService
    from app.kernel.source_store import build_source_store
    from app.services.source_acquisition import SourceAcquisitionService

    mode = os.environ["PROBE_MODE"]
    url = os.environ["PROBE_DB_URL"]
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = build_source_store()
    service = SourceAcquisitionService(
        factory, KernelCommitService(factory), store,
        workspace_id=os.environ["PROBE_WORKSPACE"],
        cache_root=Path(os.environ["MARKER_SOURCE_CACHE_ROOT"]),
    )
    try:
        if mode == "acquire":
            from app.db_migration import upgrade_database
            await upgrade_database(url=url)
            acquired = await service.acquire(
                Path(os.environ["PROBE_SOURCE"]),
                source_kind="local_path",
                suffix=".pdf",
                job_id="probe-a",
            )
            print("BLOCK:" + json.dumps(acquired.to_config()))
        elif mode == "consume":
            block = json.loads(os.environ["PROBE_BLOCK"])
            resolved = await service.resolve(block)
            if resolved is None:
                print("RESOLVE:none")
                return
            path = await service.consumable_path_for(resolved)
            data = Path(path).read_bytes()
            print("RESOLVE:" + json.dumps(resolved.to_config()))
            print("BYTES_SHA:" + hashlib.sha256(data).hexdigest())
            print("PATH:" + str(path))
        else:
            raise SystemExit("unknown PROBE_MODE " + mode)
    finally:
        await engine.dispose()
        close = getattr(store, "close", None)
        if close is not None:
            await close()


asyncio.run(main())
"""


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest_asyncio.fixture
async def probe_db(backend: str, tmp_path: Path):
    """A real durable database (PostgreSQL when provisioned, else SQLite)."""
    async with provisioned_database(
        backend, (tmp_path / "probe.db").as_posix()
    ) as prov:
        yield prov.url


async def _run_probe(env: dict[str, str]) -> tuple[int, list[str], str]:
    completed = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PROCESS_PROBE,
        env=env,
        cwd=_BACKEND_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await completed.communicate()
    return (
        completed.returncode,
        out.decode("utf-8", "replace").splitlines(),
        err.decode("utf-8", "replace"),
    )


class TestProcessBoundary:
    async def test_fresh_process_consumes_committed_shared_revision(
        self, backend: str, probe_db: str, tmp_path: Path
    ):
        endpoint, access_key, secret_key = require_s3_env()
        bucket = unique_bucket()

        roots = tmp_path / "roots"
        docs = roots / "docs"
        docs.mkdir(parents=True)
        source = docs / "doc.pdf"
        source.write_bytes(PDF_A)

        node_a = tmp_path / "node-a"
        node_b = tmp_path / "node-b"
        node_a_cache = node_a / "cache"
        node_b_cache = node_b / "cache"
        node_a_cache.mkdir(parents=True)
        node_b_cache.mkdir(parents=True)
        (node_a / "source_store").mkdir()
        (node_b / "source_store").mkdir()

        common = dict(os.environ)
        common.update(
            {
                "MARKER_TEST_BACKEND_ROOT": _BACKEND_ROOT,
                "PROBE_DB_URL": probe_db,
                "PROBE_WORKSPACE": "probe-topology",
                "MARKER_SOURCE_STORE_PROFILE": "s3",
                "MARKER_SOURCE_S3_ENDPOINT": endpoint,
                "MARKER_SOURCE_S3_BUCKET": bucket,
                "MARKER_SOURCE_S3_ACCESS_KEY": access_key,
                "MARKER_SOURCE_S3_SECRET_KEY": secret_key,
                "PYTHONIOENCODING": "utf-8",
            }
        )

        # --- process A: acquire + commit, then exit ------------------
        env_a = dict(
            common,
            PROBE_MODE="acquire",
            PROBE_SOURCE=str(source),
            MARKER_WORKSPACE_ROOTS=str(roots),
            MARKER_SOURCE_STORE_ROOT=str(node_a / "source_store"),
            MARKER_SOURCE_CACHE_ROOT=str(node_a_cache),
        )
        code, lines, err = await _run_probe(env_a)
        assert code == 0, err
        block_line = next(line for line in lines if line.startswith("BLOCK:"))
        block = json.loads(block_line.removeprefix("BLOCK:"))
        assert block["store_profile"] == "marker.kernel.source.s3.v1"

        # --- process A's node-local world is destroyed ---------------
        source.unlink()  # original external input gone
        shutil.rmtree(node_a)  # A's cache + source-store root gone

        # --- process B: different empty node dirs, durable truth only -
        env_b = dict(
            common,
            PROBE_MODE="consume",
            PROBE_BLOCK=json.dumps(block),
            MARKER_SOURCE_STORE_ROOT=str(node_b / "source_store"),
            MARKER_SOURCE_CACHE_ROOT=str(node_b_cache),
        )
        code_b, lines_b, err_b = await _run_probe(env_b)
        assert code_b == 0, err_b
        verdicts = {line.split(":", 1)[0]: line.split(":", 1)[1] for line in lines_b if ":" in line}
        assert verdicts.get("RESOLVE", "none") != "none"
        resolved = json.loads(verdicts["RESOLVE"])
        assert resolved["content_revision_id"] == block["content_revision_id"]
        assert resolved["blob_key"] == block["blob_key"]
        assert verdicts["BYTES_SHA"] == hashlib.sha256(PDF_A).hexdigest()
        consumed = Path(verdicts["PATH"])
        assert node_b_cache.resolve() in consumed.resolve().parents
        assert consumed.read_bytes() == PDF_A


class TestCrashWindows:
    async def test_s3_stage_without_commit_is_residue_retry_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        roots = tmp_path / "roots"
        docs = roots / "docs"
        docs.mkdir(parents=True)
        monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots))
        monkeypatch.delenv("MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS", raising=False)

        url = f"sqlite+aiosqlite:///{(tmp_path / 'crash.db').as_posix()}"
        await upgrade_database(url=url)
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        endpoint, access_key, secret_key = require_s3_env()
        faulty = S3SourceStore(
            S3StoreConfig(
                endpoint_url=endpoint,
                bucket=unique_bucket(),
                access_key_id=access_key,
                secret_access_key=secret_key,
                prefix="kernel-sources",
            ),
            fault_phases={PHASE_AFTER_VERIFY},
        )
        service = SourceAcquisitionService(
            factory, KernelCommitService(factory), faulty, workspace_id="crash"
        )
        src = docs / "doc.pdf"
        src.write_bytes(PDF_A)
        try:
            # Crash window: the object is fully staged + verified in the
            # shared store, but the client dies before returning — the
            # kernel commit never happens.
            with pytest.raises(InjectedFaultError):
                await service.acquire(src, source_kind="local_path", suffix=".pdf")
            async with factory() as session:
                revisions = (
                    await session.execute(
                        select(KernelRecord.id).where(
                            KernelRecord.workspace_id == "crash",
                            KernelRecord.record_class == "content_revision",
                        )
                    )
                ).scalars().all()
            assert revisions == []  # object residue is not semantic truth
        finally:
            crash_bucket = faulty._config.bucket
            await faulty.close()

        # Retry with a healthy process converges: the staged object is
        # verified + reused (dedup), one revision committed.
        healthy = S3SourceStore(
            S3StoreConfig(
                endpoint_url=endpoint,
                bucket=crash_bucket,
                access_key_id=access_key,
                secret_access_key=secret_key,
                prefix="kernel-sources",
            )
        )
        try:
            service2 = SourceAcquisitionService(
                factory, KernelCommitService(factory), healthy, workspace_id="crash"
            )
            acquired = await service2.acquire(
                src, source_kind="local_path", suffix=".pdf"
            )
            assert healthy.dedup_hits == 1  # reused the crash's staged bytes
            async with factory() as session:
                revisions = (
                    await session.execute(
                        select(KernelRecord.id).where(
                            KernelRecord.workspace_id == "crash",
                            KernelRecord.record_class == "content_revision",
                        )
                    )
                ).scalars().all()
            assert revisions == [acquired.content_revision_id]
        finally:
            await healthy.close()
        await engine.dispose()

    async def test_local_stage_without_commit_converges_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The crash window is profile-symmetric: PR70/71 behavior."""
        from app.kernel.source_store import PHASE_AFTER_VERIFY as LOCAL_AFTER_VERIFY

        roots = tmp_path / "roots"
        docs = roots / "docs"
        docs.mkdir(parents=True)
        monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots))

        url = f"sqlite+aiosqlite:///{(tmp_path / 'crash-local.db').as_posix()}"
        await upgrade_database(url=url)
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        faulty = LocalSourceStore(
            tmp_path / "store", fault_phases={LOCAL_AFTER_VERIFY}
        )
        service = SourceAcquisitionService(
            factory, KernelCommitService(factory), faulty, workspace_id="crash-l"
        )
        src = docs / "doc.pdf"
        src.write_bytes(PDF_A)
        with pytest.raises(InjectedFaultError):
            await service.acquire(src, source_kind="local_path", suffix=".pdf")

        healthy = LocalSourceStore(tmp_path / "store")
        service2 = SourceAcquisitionService(
            factory, KernelCommitService(factory), healthy, workspace_id="crash-l"
        )
        acquired = await service2.acquire(src, source_kind="local_path", suffix=".pdf")
        assert healthy.dedup_hits == 1
        assert acquired.blob_key.startswith("sha256:")
        await engine.dispose()


class TestPayloadOwnershipSeparation:
    async def test_source_acquisition_creates_no_payload_gc_authority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Kernel truth keeps source artifacts outside payload GC scope.

        A committed source revision must never appear as an ordinary
        kernel payload: no payload record classes reference it, and the
        payload namespace's own listing cannot see the source object.
        """
        roots = tmp_path / "roots"
        docs = roots / "docs"
        docs.mkdir(parents=True)
        monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots))

        url = f"sqlite+aiosqlite:///{(tmp_path / 'gc.db').as_posix()}"
        await upgrade_database(url=url)
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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
            service = SourceAcquisitionService(
                factory, KernelCommitService(factory), source, workspace_id="gc"
            )
            src = docs / "doc.pdf"
            src.write_bytes(PDF_A)
            acquired = await service.acquire(src, source_kind="local_path", suffix=".pdf")

            async with factory() as session:
                classes = Counter(
                    (
                        await session.execute(
                            select(KernelRecord.record_class).where(
                                KernelRecord.workspace_id == "gc"
                            )
                        )
                    ).scalars().all()
                )
            assert set(classes) == {
                "source_identity",
                "content_revision",
                "access_policy_revision",
                "authorization_epoch",
                "source_observation",
            }
            assert not any(
                cls.startswith("payload") for cls in classes
            )  # no payload rows: GC tombstones cannot target source bytes

            # And the payload maintenance scope cannot even see them.
            assert await payload.list_objects() == []
            assert await source.artifact_exists(acquired.blob_key, ".pdf") is True
        finally:
            await payload.close()
            await source.close()
        await engine.dispose()
