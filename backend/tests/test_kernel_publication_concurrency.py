"""Publication concurrency tests (V3.2 PR76, plan matrices A6-A8/A18).

Cooperative single-loop concurrency (asyncio.gather over the shared
file-backed factory, SQLite writer lock + service retry loops provide
the real serialization): competing publishers never split the head,
identical builds converge idempotently, and readers churning searches
across an activation stay attributable to exactly one generation.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import GenerationService
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord
from app.kernel.publications import (
    PublicationReader,
    PublicationService,
    open_published_reader,
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


async def _generation_at(factory: async_sessionmaker, workspace: str):
    return await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )


async def test_competing_publishers_produce_one_head(payload_env: tuple) -> None:
    """Two different candidates race to publish; exactly one wins the
    head, the loser ends superseded or still validated — never a split
    published state."""
    factory, store, service = payload_env
    pubs = PublicationService(factory)
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen1 = await _generation_at(factory, "ws-a")
    p1 = await pubs.publish(materialized_generation_id=gen1.generation_id)

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    await _commit_view(
        service, "ws-a", _view("view-3", {"n1": "gamma"}, "rev-s3"), advance=False
    )
    gen2 = await GenerationService(factory).build(
        await resolve_snapshot(factory, "ws-a", at_commit=2)
    )
    gen3 = await _generation_at(factory, "ws-a")  # cut 3, supersedes gen2

    results = await asyncio.gather(
        pubs.publish(materialized_generation_id=gen2.generation_id),
        pubs.publish(materialized_generation_id=gen3.generation_id),
    )
    # each publisher's ref is a snapshot of its own completion moment;
    # the durable final state is what must have exactly one winner
    winner_ids = {ref.publication_set_id for ref in results}

    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id in winner_ids
    sets = await pubs.list_publication_sets(workspace_id="ws-a")
    current_states = {
        s.publication_set_id: s.state
        for s in sets
        if s.publication_set_id in winner_ids
    }
    assert (
        list(current_states.values()).count("published") == 1
    ), current_states
    assert current_states[resolved.publication_set_id] == "published"
    # P1 was displaced by whoever won
    assert (
        await pubs.get_publication_set(p1.publication_set_id)
    ).state == "superseded"


async def test_triple_activation_of_same_set_converges(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs = PublicationService(factory)
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen1 = await _generation_at(factory, "ws-a")

    staged = await pubs.stage_publication_set(
        materialized_generation_id=gen1.generation_id
    )
    validated = await pubs.validate_publication_set(staged.publication_set_id)

    results = await asyncio.gather(
        *[pubs.activate_publication_set(validated.publication_set_id) for _ in range(3)]
    )
    assert all(ref.state == "published" for ref in results)
    assert len({ref.publication_set_id for ref in results}) == 1
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == validated.publication_set_id


async def test_concurrent_identical_builds_converge_idempotently(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen1 = await _generation_at(factory, "ws-a")
    pubs = PublicationService(factory)

    refs = await asyncio.gather(
        *[pubs.build_lexical(gen1.generation_id) for _ in range(3)]
    )
    assert len({ref.lexical_generation_id for ref in refs}) == 1
    assert all(ref.state == "validated" for ref in refs)


async def test_readers_churning_across_activation_stay_pinned(
    payload_env: tuple,
) -> None:
    """A reader keeps searching its resolved set while a new set
    activates underneath; every observed hit stays attributable to one
    generation (A7/A18)."""
    factory, store, service = payload_env
    pubs = PublicationService(factory)
    await _commit_view(
        service,
        "ws-a",
        _view("view-1", {"n1": "alpha one", "n2": "alpha two"}, "rev-s1"),
    )
    gen1 = await _generation_at(factory, "ws-a")
    p1 = await pubs.publish(materialized_generation_id=gen1.generation_id)

    async def churn(reader: PublicationReader, stop: asyncio.Event) -> list[str]:
        seen: list[str] = []
        while not stop.is_set():
            hits = await reader.search("alpha")
            seen.extend(
                f"{hit.lexical_generation_id}:{hit.node_id}" for hit in hits
            )
            await asyncio.sleep(0)
        return seen

    reader = await open_published_reader(factory, "ws-a")
    assert reader is not None
    stop = asyncio.Event()
    task = asyncio.create_task(churn(reader, stop))
    try:
        await _commit_view(
            service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
        )
        gen2 = await _generation_at(factory, "ws-a")
        await pubs.publish(materialized_generation_id=gen2.generation_id)
        await asyncio.sleep(0)
    finally:
        stop.set()
        seen = await task
        await reader.close()

    assert seen  # the reader observed hits throughout the switch
    assert set(seen) == {
        f"{p1.lexical_generation_id}:n1",
        f"{p1.lexical_generation_id}:n2",
    }  # only P1's generation ever answered


async def test_reindex_during_active_queries_never_blends(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs = PublicationService(factory)
    await _commit_view(
        service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1")
    )
    gen1 = await _generation_at(factory, "ws-a")
    p1 = await pubs.publish(materialized_generation_id=gen1.generation_id)

    async def build_and_publish_next() -> None:
        await _commit_view(
            service,
            "ws-a",
            _view("view-2", {"n1": "alpha beta", "n2": "beta"}, "rev-s2"),
            advance=False,
        )
        gen2 = await _generation_at(factory, "ws-a")
        await pubs.publish(materialized_generation_id=gen2.generation_id)

    async def query_old_set() -> set[str]:
        reader = await open_published_reader(factory, "ws-a")
        assert reader is not None
        try:
            origins: set[str] = set()
            for _ in range(5):
                for term in ("alpha", "beta"):
                    try:
                        hits = await reader.search(term)
                    except Exception:
                        continue
                    origins.update(
                        hit.lexical_generation_id for hit in hits
                    )
                await asyncio.sleep(0)
            return origins
        finally:
            await reader.close()

    old_origins, _ = await asyncio.gather(query_old_set(), build_and_publish_next())
    # the old-set reader only ever saw its own generation
    assert old_origins <= {p1.lexical_generation_id}
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.lexical_generation_id != p1.lexical_generation_id
