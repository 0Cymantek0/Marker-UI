"""Snapshot source-binding tests (PR70/71 local slice, plan §6.7/§13.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitService
from app.kernel.snapshots import resolve_snapshot
from app.kernel.source_store import LocalSourceStore
from app.services.source_acquisition import SourceAcquisitionService

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-snap"
PDF_A = b"%PDF-1.4 snapshot revision A"
PDF_B = b"%PDF-1.4 snapshot revision B"


@pytest_asyncio.fixture
async def snap_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = tmp_path / "roots"
    docs = roots / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots))
    monkeypatch.delenv("MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS", raising=False)

    url = f"sqlite+aiosqlite:///{(tmp_path / 'snap.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalSourceStore(tmp_path / "source_store")
    service = SourceAcquisitionService(
        factory, KernelCommitService(factory), store, workspace_id=WORKSPACE
    )
    try:
        yield factory, store, service, docs, tmp_path
    finally:
        await engine.dispose()


async def test_bindings_empty_before_any_source_truth(snap_env):
    factory, *_ = snap_env
    snapshot = await resolve_snapshot(factory, WORKSPACE)
    assert snapshot.content_revision_ids == ()
    assert snapshot.access_policy_set_id != ""
    # deterministic for the same committed state
    rebuilt = await resolve_snapshot(factory, WORKSPACE)
    assert rebuilt.snapshot_id == snapshot.snapshot_id


async def test_content_revision_membership_changes_identity(snap_env):
    factory, store, service, docs, tmp_path = snap_env
    src = docs / "evolve.pdf"
    src.write_bytes(PDF_A)
    await service.acquire(src, source_kind="local_path", suffix=".pdf")

    after_a = await resolve_snapshot(factory, WORKSPACE)
    assert len(after_a.content_revision_ids) == 1

    src.write_bytes(PDF_B)
    await service.acquire(src, source_kind="local_path", suffix=".pdf")
    after_b = await resolve_snapshot(factory, WORKSPACE)

    assert len(after_b.content_revision_ids) == 2
    assert after_b.snapshot_id != after_a.snapshot_id
    # historical cut pinned before the second revision keeps its bindings
    pinned = await resolve_snapshot(
        factory, WORKSPACE, at_commit=after_a.kernel_commit_id
    )
    assert pinned.snapshot_id == after_a.snapshot_id
    assert pinned.content_revision_ids == after_a.content_revision_ids


async def test_policy_only_change_binds_access_without_reminting_content(snap_env):
    factory, store, service, docs, tmp_path = snap_env
    monkeypatch = pytest.MonkeyPatch()
    src = docs / "pol.pdf"
    src.write_bytes(PDF_A)
    await service.acquire(src, source_kind="local_path", suffix=".pdf")
    before = await resolve_snapshot(factory, WORKSPACE)

    # widen the permitted root: access-only revision, same content bytes
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(tmp_path))
    try:
        await service.acquire(src, source_kind="local_path", suffix=".pdf")
    finally:
        monkeypatch.undo()
    after = await resolve_snapshot(factory, WORKSPACE)

    assert after.content_revision_ids == before.content_revision_ids
    assert after.access_policy_set_id != before.access_policy_set_id
    assert after.snapshot_id != before.snapshot_id


async def test_bindings_only_include_records_visible_in_cut(snap_env):
    factory, store, service, docs, tmp_path = snap_env
    first = docs / "one.pdf"
    first.write_bytes(PDF_A)
    await service.acquire(first, source_kind="local_path", suffix=".pdf")
    cut_after_first = (await resolve_snapshot(factory, WORKSPACE)).kernel_commit_id

    second = docs / "two.pdf"
    second.write_bytes(PDF_B)
    await service.acquire(second, source_kind="local_path", suffix=".pdf")

    pinned = await resolve_snapshot(factory, WORKSPACE, at_commit=cut_after_first)
    latest = await resolve_snapshot(factory, WORKSPACE)
    assert set(pinned.content_revision_ids) < set(latest.content_revision_ids)
