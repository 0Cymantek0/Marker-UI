"""Shared PR83C1 recovery-drill infrastructure (real services only).

Builds the deterministic industrial fixture every recovery drill needs —
real PostgreSQL, real S3-compatible payload and source namespaces, a
multi-commit kernel history with payload-backed records, acquired
source revisions, a published lexical PublicationSet with a captured
deterministic query expectation, and a fenced in-flight work item — and
provides the destructive restore path (fresh database + fresh object
namespaces, originals abandoned) the disaster drills prove against.

The recovery drills refuse to run against anything less: no SQLite, no
in-memory fakes, strict-mode-aware skipping when the real services are
absent (the strict industrial runner turns that skip into a failure).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.fencing import acquire as acquire_lease
from app.kernel.generations import GenerationService
from app.kernel.object_store import S3PayloadStore, S3StoreConfig
from app.kernel.outbox import OutboxIntent
from app.kernel.publications import PublicationService, open_published_reader
from app.kernel.records import KernelEdge, NativeObjectRecord, ObservationRecord
from app.kernel.recovery import (
    LoadedRecoveryPoint,
    PgSidecarTools,
    restore_object_namespaces,
)
from app.kernel.scheduler import register_work
from app.kernel.snapshots import (
    COMPLETENESS_COMPLETE,
    PAYLOAD_REQUIREMENT_REPLAYABLE,
    resolve_snapshot,
)
from app.kernel.source_object_store import S3SourceStore
from app.services.source_acquisition import SourceAcquisitionService
from tests.pg_provisioning import (
    create_postgres_database,
    drop_postgres_database,
    engine_kwargs_for,
    provisioned_database,
    require_postgres_admin_url,
)
from tests.s3_provisioning import require_s3_env

#: Token pair the fixture's published corpus is queried with. Chosen to
#: be tokenizer-stable (plain lowercase ASCII) across the SQLite and
#: PostgreSQL lexical backends.
QUERY_TEXT = "zephyrharbor quillanted"
QUERY_MODE = "any_term"

WORK_KIND = "conversion.execute"
RESOURCE_CLASS = "conversion"


def require_recovery_services() -> tuple[str, str, str, str]:
    """Admin URL + S3 endpoint credentials, skip/fail-aware."""
    admin_url = require_postgres_admin_url()
    endpoint, access_key, secret_key = require_s3_env()
    return admin_url, endpoint, access_key, secret_key


def parse_admin_url(url: str) -> tuple[str, int, str, str]:
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    return (
        parsed.host or "127.0.0.1",
        parsed.port or 5432,
        parsed.username or "postgres",
        parsed.password or "",
    )


def _payload_store(endpoint: str, access: str, secret: str, bucket: str) -> S3PayloadStore:
    return S3PayloadStore(
        S3StoreConfig(
            endpoint_url=endpoint,
            bucket=bucket,
            access_key_id=access,
            secret_access_key=secret,
            prefix="kernel-payloads",
            delete_namespace_on_close=True,
        )
    )


def _source_store(endpoint: str, access: str, secret: str, bucket: str) -> S3SourceStore:
    return S3SourceStore.build_default(
        endpoint_url=endpoint,
        bucket=bucket,
        access_key_id=access,
        secret_access_key=secret,
        region="us-east-1",
        prefix="kernel-sources",
    )


def _doc_view(view_id: str, text: str):
    from app.kernel.patches import ViewDocumentRecord
    from app.kernel.reading_order import OrderNode, ReadingOrderGraph

    graph = ReadingOrderGraph.build((OrderNode(node_id="n1"),), ())
    return ViewDocumentRecord(
        record_id=f"view.{view_id}",
        content_revision_ref=f"rev.{view_id}",
        graph=graph,
        texts={"n1": text},
        view_id=view_id,
    )


@dataclass
class LeasedWork:
    """One fenced in-flight work item created through the real seams."""

    work_id: int
    fencing_token: int
    owner_id: str
    result: dict[str, Any]


@dataclass
class RecoveryWorkspace:
    """A fully populated industrial workspace bound to real services."""

    workspace_id: str
    database_url: str
    admin_url: str
    engine: Any
    session_factory: async_sessionmaker
    payload_store: S3PayloadStore
    source_store: S3SourceStore
    commit_service: KernelCommitService
    generation_service: GenerationService
    publication_service: PublicationService
    acquisition: SourceAcquisitionService
    pg_tools: PgSidecarTools
    endpoint: str
    access_key: str
    secret_key: str
    database_name: str
    #: deterministic query expectation captured from the live reader
    query_expectation: dict[str, Any] = field(default_factory=dict)
    #: acquired source revision configs (store profile = s3)
    source_blocks: list[dict[str, Any]] = field(default_factory=list)
    leased: list[LeasedWork] = field(default_factory=list)
    _backup_payload: S3PayloadStore | None = None
    _backup_source: S3SourceStore | None = None
    _closed: bool = False

    @property
    def live_stores(self) -> tuple[S3PayloadStore, S3SourceStore]:
        return self.payload_store, self.source_store

    def ensure_backup_stores(self) -> tuple[S3PayloadStore, S3SourceStore]:
        """One dedicated backup namespace pair per workspace lifetime."""
        if self._backup_payload is None:
            self._backup_payload = self.new_backup_payload_store()
        if self._backup_source is None:
            self._backup_source = self.new_backup_source_store()
        return self._backup_payload, self._backup_source

    def new_backup_payload_store(self) -> S3PayloadStore:
        return _payload_store(
            self.endpoint,
            self.access_key,
            self.secret_key,
            f"marker-rec-backup-p-{uuid.uuid4().hex[:12]}",
        )

    def new_backup_source_store(self) -> S3SourceStore:
        return _source_store(
            self.endpoint,
            self.access_key,
            self.secret_key,
            f"marker-rec-backup-s-{uuid.uuid4().hex[:12]}",
        )

    async def commit(self, batch: KernelCommitBatch):
        return await self.commit_service.commit(batch)

    async def head(self) -> int:
        from app.kernel.recovery import current_head_commit

        return await current_head_commit(self.session_factory, self.workspace_id)

    async def acquire_source(self, path: Path, *, job_id: str):
        acquired = await self.acquisition.acquire(
            path, source_kind="local_path", suffix=".pdf", job_id=job_id
        )
        self.source_blocks.append(acquired.to_config())
        return acquired

    async def destroy_original_namespaces(self) -> None:
        """Delete the live payload + source object namespaces.

        After this the original object bytes are gone; only backup or
        restored copies can satisfy any closure check.
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self.payload_store.close()
        except Exception:
            pass
        try:
            await self.source_store.close()
        except Exception:
            pass

    async def close(self) -> None:
        if not self._closed:
            await self.destroy_original_namespaces()
        for store in (self._backup_payload, self._backup_source):
            close = getattr(store, "close", None)
            if store is not None and close is not None:
                try:
                    await close()
                except Exception:
                    pass
        self._backup_payload = None
        self._backup_source = None
        await self.engine.dispose()


@asynccontextmanager
async def recovery_workspace(tmp_path: Path, *, workspace_id: str = "recovery-drill"):
    """Provision + populate one industrial workspace; tear it down after.

    Everything real: PostgreSQL database, S3 payload namespace, S3 source
    namespace, kernel commits with payload bytes, acquired source
    revisions, an activated materialized generation, a published lexical
    set with the deterministic query captured, and one fenced in-flight
    work item under a short lease.
    """
    admin_url, endpoint, access_key, secret_key = require_recovery_services()
    async with provisioned_database(
        "postgresql", (tmp_path / "kernel.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        engine = create_async_engine(prov.url, **engine_kwargs_for("postgresql"))
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        payload_store = _payload_store(
            endpoint, access_key, secret_key, f"marker-rec-p-{uuid.uuid4().hex[:12]}"
        )
        source_store = _source_store(
            endpoint, access_key, secret_key, f"marker-rec-s-{uuid.uuid4().hex[:12]}"
        )
        host, port, user, password = parse_admin_url(admin_url)
        ws = RecoveryWorkspace(
            workspace_id=workspace_id,
            database_url=prov.url,
            admin_url=admin_url,
            engine=engine,
            session_factory=session_factory,
            payload_store=payload_store,
            source_store=source_store,
            commit_service=KernelCommitService(session_factory, payload_store=payload_store),
            generation_service=GenerationService(session_factory),
            publication_service=PublicationService(session_factory),
            acquisition=SourceAcquisitionService(
                session_factory,
                KernelCommitService(session_factory),
                source_store,
                workspace_id=workspace_id,
                cache_root=tmp_path / "source-cache",
            ),
            pg_tools=PgSidecarTools(
                host=host, port=port, user=user, password=password
            ),
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            database_name=prov.url.rsplit("/", 1)[-1],
        )
        try:
            await _populate(ws, tmp_path)
            yield ws
        finally:
            await ws.close()


async def _populate(ws: RecoveryWorkspace, tmp_path: Path) -> None:
    state = await populate_workspace(
        ws.session_factory,
        ws.payload_store,
        ws.source_store,
        ws.workspace_id,
        tmp_path,
    )
    ws.query_expectation = state.query_expectation
    ws.source_blocks = state.source_blocks
    # the in-process drills want the work in-flight under a short lease
    owner_id = "drill-worker-a"
    lease = await acquire_lease(
        ws.session_factory,
        work_id=state.work_id,
        owner_id=owner_id,
        lease_seconds=2.0,
    )
    assert lease is not None
    ws.leased.append(
        LeasedWork(
            work_id=state.work_id,
            fencing_token=lease.fencing_token,
            owner_id=owner_id,
            result={
                "job_id": state.job_id,
                "status": "completed",
                "marker": f"result-{state.job_id}",
            },
        )
    )


@dataclass
class FixtureState:
    """What ``populate_workspace`` built, for drills and probes alike."""

    query_expectation: dict[str, Any]
    source_blocks: list[dict[str, Any]]
    work_id: int
    job_id: str = "drill-job-a"


async def populate_workspace(
    session_factory: async_sessionmaker,
    payload_store: S3PayloadStore,
    source_store: S3SourceStore,
    workspace_id: str,
    tmp_path: Path,
) -> FixtureState:
    """Deterministic industrial fixture over the given real services.

    Kernel commits with distinct payload bytes, two acquired source
    revisions, a published lexical PublicationSet whose deterministic
    query expectation is captured live, and a fenced in-flight work
    item under a short lease. Used by the in-process drills and by the
    OS-process failover probes — one authority for the fixture shape.
    """
    # local-path acquisition policy reads MARKER_WORKSPACE_ROOTS at call
    # time: permit sources under tmp_path for this call's lifetime only
    import os as _os

    previous_roots = _os.environ.get("MARKER_WORKSPACE_ROOTS")
    _os.environ["MARKER_WORKSPACE_ROOTS"] = str(tmp_path)
    try:
        return await _populate_workspace_inner(
            session_factory, payload_store, source_store, workspace_id, tmp_path
        )
    finally:
        if previous_roots is None:
            _os.environ.pop("MARKER_WORKSPACE_ROOTS", None)
        else:
            _os.environ["MARKER_WORKSPACE_ROOTS"] = previous_roots


async def _populate_workspace_inner(
    session_factory: async_sessionmaker,
    payload_store: S3PayloadStore,
    source_store: S3SourceStore,
    workspace_id: str,
    tmp_path: Path,
) -> FixtureState:
    from app.kernel.commit import KernelCommitService as _KCS

    commit_service = _KCS(session_factory, payload_store=payload_store)
    generations = GenerationService(session_factory)
    pubs = PublicationService(session_factory)
    acquisition = SourceAcquisitionService(
        session_factory, _KCS(session_factory), source_store,
        workspace_id=workspace_id, cache_root=tmp_path / "source-cache",
    )

    async def commit(batch: KernelCommitBatch):
        return await commit_service.commit(batch)

    # -- kernel commits with distinct payload bytes (2 payload objects) --
    await commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ObservationRecord(
                    observer="drill.alpha",
                    derivation={"step": "observe-alpha"},
                    payload_bytes=b"RECOVERY-PAYLOAD-ALPHA-" + b"a" * 64,
                ),
            ),
            producer={"drill": "pr83c1"},
        )
    )
    await commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ObservationRecord(
                    observer="drill.beta",
                    derivation={"step": "observe-beta"},
                    payload_bytes=b"RECOVERY-PAYLOAD-BETA-" + b"b" * 96,
                ),
            ),
            producer={"drill": "pr83c1"},
        )
    )

    # -- two acquired source revisions (S3 source namespace) --
    source_blocks: list[dict[str, Any]] = []
    for index, name in enumerate(("alpha", "beta")):
        source_path = tmp_path / f"source-{name}.pdf"
        source_path.write_bytes(
            b"%PDF-1.4 RECOVERY SOURCE " + name.encode() + b" " + bytes([48 + index]) * 32
        )
        acquired = await acquisition.acquire(
            source_path, source_kind="local_path", suffix=".pdf",
            job_id=f"drill-src-{name}",
        )
        source_blocks.append(acquired.to_config())

    # -- published lexical corpus over distinct view documents --
    await commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                _doc_view("alpha", "zephyrharbor drifts over the alpha yard"),
                _doc_view("beta", "quillanted ink dries on the beta page"),
            ),
            producer={"drill": "pr83c1"},
        )
    )
    snapshot = await resolve_snapshot(session_factory, workspace_id)
    generation = await generations.build_and_activate(snapshot)
    await pubs.publish(materialized_generation_id=generation.generation_id)
    reader = await open_published_reader(session_factory, workspace_id)
    assert reader is not None
    try:
        hits = await reader.search(QUERY_TEXT, QUERY_MODE)
        expected_ids = [hit.record_id for hit in hits]
    finally:
        await reader.close()
    assert expected_ids, "fixture corpus must produce deterministic hits"
    query_expectation = {
        "profile": "default",
        "text": QUERY_TEXT,
        "mode": QUERY_MODE,
        "expected_record_ids": expected_ids,
    }

    # -- replayable snapshot sanity before any drill relies on it --
    verified = await resolve_snapshot(
        session_factory,
        workspace_id,
        required_payload_state=PAYLOAD_REQUIREMENT_REPLAYABLE,
        payload_store=payload_store,
    )
    assert verified.completeness == COMPLETENESS_COMPLETE

    # -- one fenced in-flight work item (short lease, owner may die) --
    revision = await acquisition.resolve(source_blocks[0])
    assert revision is not None
    record = NativeObjectRecord(
        record_id="conversion-request.drill-job-a",
        source_uri=revision.source_id,
        locator=revision.blob_key,
        media_type=revision.media_type,
        extractor_name="recovery-drill",
        extractor_version="1",
    )
    receipt = await commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(record,),
            edges=(
                KernelEdge(
                    edge_kind="depends_on",
                    source_ref=record.record_id,
                    target_ref=revision.content_revision_id,
                ),
            ),
            outbox=(OutboxIntent(work_kind=WORK_KIND, payload={"job_id": "drill-job-a"}),),
        )
    )
    work_id = receipt.outbox_ids[0]
    await register_work(session_factory, work_id=work_id, resource_class=RESOURCE_CLASS)
    return FixtureState(
        query_expectation=query_expectation,
        source_blocks=source_blocks,
        work_id=work_id,
    )


@dataclass
class RestoredTarget:
    """A disaster-restored topology: fresh database + fresh namespaces."""

    database_url: str
    database_name: str
    engine: Any
    session_factory: async_sessionmaker
    payload_store: S3PayloadStore
    source_store: S3SourceStore
    scratch_dir: Path

    async def close(self) -> None:
        await self.payload_store.close()
        await self.source_store.close()
        await self.engine.dispose()
        import shutil

        shutil.rmtree(self.scratch_dir, ignore_errors=True)


async def restore_to_fresh_services(
    ws: RecoveryWorkspace,
    loaded: LoadedRecoveryPoint,
    tmp_path: Path,
    *,
    destroy_originals: bool = True,
) -> RestoredTarget:
    """The destructive restore: fresh PostgreSQL target, fresh object
    namespaces seeded from the verified backup, originals abandoned.

    Ordering is the proof: the original database is dropped and the
    original object namespaces deleted *before* the oracle runs, so a
    passing oracle can only be reading restored copies.
    """
    from sqlalchemy.engine import make_url

    dump = loaded.dump_path.read_bytes()

    if destroy_originals:
        await ws.destroy_original_namespaces()
        await ws.engine.dispose()
        await drop_postgres_database(ws.admin_url, ws.database_url)

    fresh_url = await create_postgres_database(ws.admin_url)
    fresh_name = make_url(fresh_url).database
    await ws.pg_tools.restore_database(fresh_name, dump)

    engine = create_async_engine(fresh_url, **engine_kwargs_for("postgresql"))
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    target_payload = _payload_store(
        ws.endpoint, ws.access_key, ws.secret_key, f"marker-rec-restore-p-{uuid.uuid4().hex[:12]}"
    )
    target_source = _source_store(
        ws.endpoint, ws.access_key, ws.secret_key, f"marker-rec-restore-s-{uuid.uuid4().hex[:12]}"
    )
    scratch = Path(tmp_path) / "restore-scratch"
    backup_payload, backup_source = ws.ensure_backup_stores()
    await restore_object_namespaces(
        loaded,
        backup_payload_store=backup_payload,
        backup_source_store=backup_source,
        target_payload_store=target_payload,
        target_source_store=target_source,
        scratch_dir=scratch,
    )
    return RestoredTarget(
        database_url=fresh_url,
        database_name=str(fresh_name),
        engine=engine,
        session_factory=session_factory,
        payload_store=target_payload,
        source_store=target_source,
        scratch_dir=scratch,
    )
