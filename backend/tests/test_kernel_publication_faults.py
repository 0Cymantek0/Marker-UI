"""Publication fault-injection tests (V3.2 PR76, plan matrices 10 + 11).

Deterministic faults at every lifecycle phase: nothing before the
activation linearization point may displace the accepted published set;
a fault after the commit exposes the complete new set; staged residue
is identifiable, resumable, and collectable; retrying after an
uncertain acknowledgment converges on the same accepted set.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError, KernelError
from app.kernel.generations import GenerationService
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord
from app.kernel.publications import (
    PUBLICATION_FAULT_PHASES,
    PHASE_PUB_LEXICAL_STAGED,
    PHASE_PUB_POST_ACTIVATE,
    PHASE_PUB_PRE_ACTIVATE,
    PHASE_PUB_SET_STAGED,
    PHASE_PUB_VALIDATE_BEGIN,
    PHASE_PUB_VALIDATED,
    PublicationService,
    resolve_published_set,
    verify_publication_set,
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


async def _seed_two_cuts(
    factory: async_sessionmaker, service: KernelCommitService
) -> tuple:
    """ws-a: publish P1 from cut 1, then advance to cut 2 (unpublished)."""
    pubs = PublicationService(factory)
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen1 = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    p1 = await pubs.publish(materialized_generation_id=gen1.generation_id)

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    return pubs, gen1, p1, gen2


async def test_fault_phase_set_is_exact() -> None:
    assert PUBLICATION_FAULT_PHASES == frozenset(
        {
            "pub-lexical-begin",
            "pub-lexical-source-read",
            "pub-lexical-rows-materialized",
            "pub-lexical-staged",
            "pub-lexical-validate-begin",
            "pub-lexical-validated",
            "pub-set-staged",
            "pub-validate-begin",
            "pub-validated",
            "pub-pre-activate",
            "pub-post-activate",
        }
    )


@pytest.mark.parametrize(
    "phase",
    sorted(
        PUBLICATION_FAULT_PHASES
        - {PHASE_PUB_POST_ACTIVATE}
    ),
)
async def test_fault_before_linearization_keeps_prior_set(
    payload_env: tuple, phase: str
) -> None:
    """Every fault before the activation commit leaves P1 the one
    published set; the failed candidate is never visible."""
    factory, store, service = payload_env
    pubs, gen1, p1, gen2 = await _seed_two_cuts(factory, service)

    with pytest.raises((InjectedFaultError, KernelError)):
        await pubs.publish(
            materialized_generation_id=gen2.generation_id, _inject_fault_at=phase
        )

    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id
    assert (await verify_publication_set(factory, p1.publication_set_id)).ok


async def test_pre_activate_rolls_back_then_retry_converges(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1, gen2 = await _seed_two_cuts(factory, service)

    with pytest.raises(InjectedFaultError):
        await pubs.publish(
            materialized_generation_id=gen2.generation_id,
            _inject_fault_at=PHASE_PUB_PRE_ACTIVATE,
        )
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id

    p2 = await pubs.publish(materialized_generation_id=gen2.generation_id)
    assert p2.state == "published"
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p2.publication_set_id
    assert (
        await pubs.get_publication_set(p1.publication_set_id)
    ).state == "superseded"


async def test_post_activate_fault_leaves_complete_new_set(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1, gen2 = await _seed_two_cuts(factory, service)

    with pytest.raises(InjectedFaultError):
        await pubs.publish(
            materialized_generation_id=gen2.generation_id,
            _inject_fault_at=PHASE_PUB_POST_ACTIVATE,
        )

    # the caller saw an error, but the commit happened: P2 is complete
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id != p1.publication_set_id
    assert resolved.state == "published"
    assert (await verify_publication_set(factory, resolved.publication_set_id)).ok

    # retrying the uncertain acknowledgment converges on the same set
    retried = await pubs.publish(materialized_generation_id=gen2.generation_id)
    assert retried.publication_set_id == resolved.publication_set_id
    assert retried.state == "published"
    sets = await pubs.list_publication_sets(workspace_id="ws-a")
    assert len([s for s in sets if s.state == "published"]) == 1


async def test_staged_candidate_survives_restart_and_stays_invisible(
    payload_env: tuple,
) -> None:
    """A staged-but-never-published candidate (crash between staging and
    validation) is repairable from durable state alone and never
    displaces the last accepted set."""
    factory, store, service = payload_env
    pubs, gen1, p1, gen2 = await _seed_two_cuts(factory, service)

    with pytest.raises(InjectedFaultError):
        await pubs.publish(
            materialized_generation_id=gen2.generation_id,
            _inject_fault_at=PHASE_PUB_SET_STAGED,
        )
    staged = [
        s for s in await pubs.list_publication_sets(workspace_id="ws-a")
        if s.state == "staged"
    ]
    assert len(staged) == 1

    # fresh process (restart view): the candidate validates + activates
    from tests.test_kernel_publication import _fresh_factory

    fresh = PublicationService(_fresh_factory(_db_path(factory)))
    validated = await fresh.validate_publication_set(staged[0].publication_set_id)
    activated = await fresh.activate_publication_set(validated.publication_set_id)
    assert activated.state == "published"

    resolved = await resolve_published_set(
        _fresh_factory(_db_path(factory)), "ws-a"
    )
    assert resolved is not None
    assert resolved.publication_set_id == staged[0].publication_set_id


async def test_unknown_and_misapplied_fault_phases_rejected(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1, gen2 = await _seed_two_cuts(factory, service)

    with pytest.raises(KernelError, match="unknown fault phase"):
        await pubs.publish(
            materialized_generation_id=gen2.generation_id, _inject_fault_at="nope"
        )
    with pytest.raises(KernelError, match="does not apply"):
        await pubs.validate_publication_set(
            p1.publication_set_id, _inject_fault_at=PHASE_PUB_PRE_ACTIVATE
        )
    with pytest.raises(KernelError, match="does not apply"):
        await pubs.activate_publication_set(
            p1.publication_set_id, _inject_fault_at=PHASE_PUB_VALIDATE_BEGIN
        )


async def test_stale_staged_lexical_residue_never_confused_with_published(
    payload_env: tuple,
) -> None:
    """Crash-orphaned lexical staging residue is identifiable (A24/
    negative list): it cannot activate, and the published set stays the
    last accepted one."""
    factory, store, service = payload_env
    pubs, gen1, p1, gen2 = await _seed_two_cuts(factory, service)

    with pytest.raises(InjectedFaultError):
        await pubs.build_lexical(
            gen2.generation_id, _inject_fault_at=PHASE_PUB_LEXICAL_STAGED
        )
    residue = [
        ref
        for ref in await pubs.list_lexical_generations(workspace_id="ws-a")
        if ref.state == "staged"
    ]
    assert len(residue) == 1

    from app.kernel.errors import LexicalStateError

    with pytest.raises(LexicalStateError):
        await pubs.stage_publication_set(
            materialized_generation_id=gen2.generation_id,
            lexical_generation_id=residue[0].lexical_generation_id,
        )
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id
