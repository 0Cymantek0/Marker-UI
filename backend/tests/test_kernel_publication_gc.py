"""Publication GC integration tests (V3.2 PR76, plan matrices A24/A25).

Published members are never collected while live; a pinned superseded
set and all of its members survive collection until the pin releases or
lapses; retirement drops lexical rows, manifests, and the runtime FTS5
tables transactionally; stale staging residue obeys the same grace
threshold as generations.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import GenerationService
from app.kernel.gc import collect, plan_collection
from app.kernel.payloads import LocalPayloadStore
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord
from app.kernel.publications import (
    PublicationService,
    open_pinned_publication,
    resolve_published_set,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
from app.kernel.snapshots import resolve_snapshot

pytestmark = pytest.mark.asyncio


def _view(record_id: str, texts: dict[str, str], revision: str) -> ViewDocumentRecord:
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts),
        (),
    )
    return ViewDocumentRecord(
        record_id=record_id,
        content_revision_ref=revision,
        graph=graph,
        texts=dict(texts),
    )


async def _commit_view(
    service: KernelCommitService,
    workspace: str,
    view: ViewDocumentRecord,
    *,
    advance: bool = True,
) -> None:
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(view,),
            view_advancement=ViewAdvancement(new_revision_id=view.view_revision_id())
            if advance
            else None,
        )
    )


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


def _fts_tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'kernel_fts_%'"
            )
        }


async def _publish(
    factory: async_sessionmaker,
    service: KernelCommitService,
    workspace: str,
    marker: str,
    *,
    advance: bool = True,
):
    pubs = PublicationService(factory)
    await _commit_view(
        service,
        workspace,
        _view(f"view-{marker}", {"n1": f"{marker} text"}, marker),
        advance=advance,
    )
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


async def test_published_members_never_collected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish(factory, service, "ws-a", "one")

    report = await collect(factory, store)
    assert report.publication_sets_retired == 0
    assert report.lexical_generations_retired == 0
    assert report.generations_retired == 0
    assert (await pubs.get_publication_set(p1.publication_set_id)).state == "published"
    assert (
        await pubs.get_lexical_generation(p1.lexical_generation_id)
    ).state == "validated"
    assert _fts_tables(_db_path(factory)) != set()


async def test_pinned_superseded_set_survives_then_retires(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", "one")
    pubs2, gen2, p2 = await _publish(factory, service, "ws-a", "two", advance=False)
    assert (await pubs.get_publication_set(p1.publication_set_id)).state == "superseded"

    reader = await open_pinned_publication(
        factory, p1.publication_set_id, lease_seconds=300
    )
    try:
        # while the pin is live nothing of P1 may retire
        report = await collect(factory, store)
        assert report.publication_sets_retired == 0
        assert report.lexical_generations_retired == 0
        hits = await reader.search("one")
        assert [hit.text for hit in hits] == ["one text"]
    finally:
        await reader.close()

    report = await collect(factory, store)
    assert report.publication_sets_retired == 1
    assert report.lexical_generations_retired == 1
    assert report.generations_retired == 1  # gen1 unprotected once P1 retired

    remaining_fts = _fts_tables(_db_path(factory))
    assert p1.lexical_generation_id.removeprefix("sha256:") not in {
        name.removeprefix("kernel_fts_") for name in remaining_fts
    }
    assert p2.lexical_generation_id.removeprefix("sha256:") in {
        name.removeprefix("kernel_fts_") for name in remaining_fts
    }

    # the published state is untouched P2 throughout
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p2.publication_set_id


async def test_expired_publication_pin_lapses_and_members_retire(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", "one")
    await _publish(factory, service, "ws-a", "two", advance=False)

    reader = await open_pinned_publication(
        factory, p1.publication_set_id, lease_seconds=300
    )
    pin_id = reader.pin_id
    assert pin_id is not None
    # simulate lease lapse: backdate expiry to the past (dialect storage
    # format: naive UTC isoformat with a space separator)
    lapsed = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(sep=" ")
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_publication_pins SET expires_at = ? WHERE pin_id = ?",
            (lapsed, pin_id),
        )
        conn.commit()

    report = await collect(factory, store)
    assert report.expired_publication_pins_purged == 1
    assert report.publication_sets_retired == 1
    assert report.lexical_generations_retired == 1
    await reader.close()  # released row is already gone; close is idempotent


async def test_pin_acquired_after_mark_rescued_at_retirement(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", "one")
    await _publish(factory, service, "ws-a", "two", advance=False)

    plan = await plan_collection(factory, store)
    assert p1.publication_set_id in plan.eligible_publication_sets

    reader = await open_pinned_publication(
        factory, p1.publication_set_id, lease_seconds=300
    )
    try:
        from app.kernel.gc import execute_collection

        report = await execute_collection(factory, store, plan)
        assert p1.publication_set_id in report.publication_sets_rescued
        assert report.publication_sets_retired == 0
        assert report.lexical_generations_retired == 0
    finally:
        await reader.close()


async def test_stale_staged_lexical_residue_collectible(payload_env: tuple) -> None:
    factory, store, service = payload_env
    from app.kernel.publications import PHASE_PUB_LEXICAL_STAGED

    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    pubs = PublicationService(factory)
    with pytest.raises(Exception, match="injected fault"):
        await pubs.build_lexical(
            gen.generation_id, _inject_fault_at=PHASE_PUB_LEXICAL_STAGED
        )

    # fresh staging residue is retained by the default grace window
    default_report = await collect(factory, store)
    assert default_report.lexical_generations_retired == 0

    stale_report = await collect(
        factory, store, stale_staging_seconds=0.0
    )
    assert stale_report.lexical_generations_retired == 1
    assert _fts_tables(_db_path(factory)) == set()


async def test_stale_staged_publication_candidate_collectible(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    from app.kernel.publications import PHASE_PUB_SET_STAGED

    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    pubs = PublicationService(factory)
    with pytest.raises(Exception, match="injected fault"):
        await pubs.stage_publication_set(
            materialized_generation_id=gen.generation_id,
            _inject_fault_at=PHASE_PUB_SET_STAGED,
        )

    default_report = await collect(factory, store)
    assert default_report.publication_sets_retired == 0

    stale_report = await collect(factory, store, stale_staging_seconds=0.0)
    assert stale_report.publication_sets_retired == 1
    assert await resolve_published_set(factory, "ws-a") is None
