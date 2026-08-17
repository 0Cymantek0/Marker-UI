"""High-assurance security-domain partition publications (PR78).

A partitioned lexical generation restricts its corpus to view documents
whose committed lineage (view → content revision → source → latest
in-generation domain assignment) resolves into the declared security
domains. Unresolvable or unattributed lineage is excluded, never
guessed in, and a malformed policy payload fails the build closed. The
partition shares the materialized generation with the shared build but
is a physically separate FTS5 corpus under a derived, caller-unnamable
``ha.`` profile.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import KernelError, LexicalIntegrityError
from app.kernel.generations import GenerationService
from app.kernel.models import KernelLexicalRow

from app.kernel.publications import (
    HIGH_ASSURANCE_PROFILE_PREFIX,
    PublicationService,
    compute_lexical_identity,
    high_assurance_partition_key,
    high_assurance_profile,
    open_published_reader,
    resolve_published_set,
)
from app.kernel.records import (
    SOURCE_CONSISTENCY_NATIVE_ATOMIC,
    ContentRevisionRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
from app.kernel.snapshots import resolve_snapshot
from app.kernel.patches import ViewDocumentRecord

pytestmark = pytest.mark.asyncio


def _blob(value: int) -> str:
    return f"sha256:{value:064x}"


def _view(
    record_id: str,
    texts: dict[str, str],
    revision: str,
    *,
    view_id: str = "document",
) -> ViewDocumentRecord:
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    return ViewDocumentRecord(
        record_id=record_id,
        content_revision_ref=revision,
        graph=graph,
        texts=dict(texts),
        view_id=view_id,
    )


async def _seed_source(
    service: KernelCommitService,
    workspace: str,
    *,
    tag: str,
    domain: str,
    node_text: str,
    node_id: str = "n1",
) -> str:
    """Commit full lineage for one source and return its view record id."""
    source = SourceIdentityRecord(
        record_id=f"src.{tag}",
        source_kind="local_path",
        source_key=f"C:/docs/{tag}.md",
    )
    revision = ContentRevisionRecord(
        record_id=f"rev.{tag}",
        source_ref=source.record_id,
        blob_key=_blob(abs(int.from_bytes(tag.encode(), "big")) % (1 << 256)),
        byte_length=len(node_text),
        media_type="text/markdown",
        consistency_class=SOURCE_CONSISTENCY_NATIVE_ATOMIC,
        suffix=".md",
    )
    assignment = SecurityDomainRecord(
        record_id=f"assign.{tag}",
        source_ref=source.record_id,
        domain_key=domain,
    )
    view = _view(
        f"view.{tag}", {node_id: node_text}, revision.record_id, view_id=f"doc-{tag}"
    )
    # No view-head advancement: the lexical corpus is derived from the
    # generation records alone (latest revision per view at the cut), so
    # policy/lineage commits need not move the view head.
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(source, revision, assignment, view),
        )
    )
    return view.record_id


async def _commit_bare_view(
    service: KernelCommitService, workspace: str, record_id: str, text: str
) -> None:
    """A view with no source lineage at all (PR77-style fixture)."""
    view = _view(record_id, {"n1": text}, "rev-standalone", view_id=record_id)
    await service.commit(
        KernelCommitBatch(workspace_id=workspace, records=(view,))
    )


async def _materialized(factory: async_sessionmaker, workspace: str):
    return await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )


async def _locator_record_ids(
    factory: async_sessionmaker, lexical_generation_id: str
) -> set[str]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KernelLexicalRow.record_id).where(
                        KernelLexicalRow.lexical_generation_id == lexical_generation_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return set(rows)


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


async def test_partition_corpus_contains_only_declared_domains(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    alpha = await _seed_source(
        service, "ws-a", tag="alpha", domain="dom-alpha", node_text="needle alpha one"
    )
    beta = await _seed_source(
        service, "ws-a", tag="beta", domain="dom-beta", node_text="needle beta two"
    )
    bare = "view-bare"
    await _commit_bare_view(service, "ws-a", bare, "needle no lineage")
    gen = await _materialized(factory, "ws-a")

    pubs = PublicationService(factory)
    shared = await pubs.publish(materialized_generation_id=gen.generation_id)
    partition = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )

    assert partition.profile.startswith(HIGH_ASSURANCE_PROFILE_PREFIX)
    assert partition.profile != shared.profile
    assert partition.materialized_generation_id == shared.materialized_generation_id
    assert partition.lexical_generation_id != shared.lexical_generation_id

    shared_ids = await _locator_record_ids(factory, shared.lexical_generation_id)
    partition_ids = await _locator_record_ids(factory, partition.lexical_generation_id)
    # Shared corpus: both domains plus the unattributed view.
    assert shared_ids == {alpha, beta, bare}
    # Partition corpus: only the declared domain; unattributed excluded.
    assert partition_ids == {alpha}


async def test_partition_reader_serves_only_authorized_hits(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _seed_source(
        service, "ws-a", tag="alpha", domain="dom-alpha", node_text="shared needle"
    )
    await _seed_source(
        service,
        "ws-a",
        tag="beta",
        domain="dom-beta",
        node_text="shared needle shared needle shared needle",
    )
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    await pubs.publish(materialized_generation_id=gen.generation_id)
    partition = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )

    reader = await open_published_reader(
        factory, "ws-a", profile=partition.profile, pin_lease_seconds=None
    )
    assert reader is not None
    try:
        hits = await reader.search('"needle"', limit=10)
        assert {hit.record_id for hit in hits} == {"view.alpha"}
    finally:
        await reader.close()


async def test_partition_reassignment_follows_latest_assignment(
    payload_env: tuple,
) -> None:
    """A source reassigned before the cut belongs to its newest domain:
    the latest in-generation assignment wins, by causal commit order."""
    factory, store, service = payload_env
    source = SourceIdentityRecord(
        record_id="src.move", source_kind="local_path", source_key="C:/docs/move.md"
    )
    revision = ContentRevisionRecord(
        record_id="rev.move",
        source_ref="src.move",
        blob_key=_blob(1),
        byte_length=1,
        media_type="text/markdown",
        consistency_class=SOURCE_CONSISTENCY_NATIVE_ATOMIC,
        suffix=".md",
    )
    first = SecurityDomainRecord(
        record_id="assign.move.1", source_ref="src.move", domain_key="dom-alpha"
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(source, revision, first))
    )
    # The later assignment (dom-beta) and the view arrive in one later
    # commit, so the cut sees the source in its newest domain.
    view = _view("view.move", {"n1": "moving needle"}, "rev.move", view_id="doc-move")
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                SecurityDomainRecord(
                    record_id="assign.move.2",
                    source_ref="src.move",
                    domain_key="dom-beta",
                ),
                view,
            ),
        )
    )
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    alpha_set = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )
    beta_set = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-beta"}),
    )
    alpha_ids = await _locator_record_ids(factory, alpha_set.lexical_generation_id)
    beta_ids = await _locator_record_ids(factory, beta_set.lexical_generation_id)
    assert alpha_ids == set()
    assert beta_ids == {"view.move"}


async def test_partition_build_is_idempotent_and_separate_from_shared(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _seed_source(
        service, "ws-a", tag="alpha", domain="dom-alpha", node_text="stable needle"
    )
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    shared = await pubs.publish(materialized_generation_id=gen.generation_id)
    partition = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )
    again = await pubs.build_lexical(
        gen.generation_id, partition_domains=frozenset({"dom-alpha"})
    )
    shared_again = await pubs.build_lexical(gen.generation_id)
    assert again.lexical_generation_id == partition.lexical_generation_id
    assert shared_again.lexical_generation_id == shared.lexical_generation_id


async def test_unpartitioned_identity_unchanged_by_partition_parameter(
    payload_env: tuple,
) -> None:
    """The partition key only enters identity when set: legacy builds
    keep their exact pre-PR78 identity inputs."""
    base = dict(
        workspace_id="ws-a",
        kernel_commit_id=7,
        snapshot_id="snap-1",
        source_generation_id="sha256:" + "1" * 64,
    )
    legacy = compute_lexical_identity(**base)
    explicit_default = compute_lexical_identity(**base, partition_key="")
    partitioned = compute_lexical_identity(**base, partition_key="sha256:" + "9" * 64)
    assert legacy == explicit_default
    assert partitioned != legacy


async def test_high_assurance_profile_is_deterministic_and_order_independent():
    a = high_assurance_profile(["dom-alpha", "dom-beta"])
    b = high_assurance_profile(["dom-beta", "dom-alpha"])
    c = high_assurance_profile(frozenset({"dom-alpha", "dom-beta"}))
    assert a == b == c
    assert a.startswith("ha.")
    assert high_assurance_profile(["dom-alpha"]) != a
    assert high_assurance_partition_key(["a", "b"]).startswith("sha256:")
    # Grammar: partition profiles must be valid publication profiles.
    from app.kernel.publications import validate_publication_profile

    validate_publication_profile(a)


async def test_partition_requires_at_least_one_domain(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _seed_source(
        service, "ws-a", tag="alpha", domain="dom-alpha", node_text="needle"
    )
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    with pytest.raises(KernelError, match="at least one security domain"):
        await pubs.publish_high_assurance(
            materialized_generation_id=gen.generation_id, partition_domains=frozenset()
        )


async def test_malformed_domain_assignment_fails_partition_build_closed(
    payload_env: tuple,
) -> None:
    """A tampered security_domain payload in the generation refuses the
    whole partition build — membership is never guessed."""
    factory, store, service = payload_env
    await _seed_source(
        service, "ws-a", tag="alpha", domain="dom-alpha", node_text="needle"
    )
    gen = await _materialized(factory, "ws-a")
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_generation_records SET payload_json = '{not json' "
            "WHERE generation_id = ? AND record_class = 'security_domain'",
            (gen.generation_id,),
        )
        conn.commit()
    pubs = PublicationService(factory)
    with pytest.raises(LexicalIntegrityError, match="unreadable|malformed"):
        await pubs.publish_high_assurance(
            materialized_generation_id=gen.generation_id,
            partition_domains=frozenset({"dom-alpha"}),
        )


async def test_reader_search_offset_pages_deterministically(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _seed_source(
        service, "ws-a", tag="a1", domain="dom-alpha", node_text="needle one"
    )
    await _seed_source(
        service, "ws-a", tag="a2", domain="dom-alpha", node_text="needle two"
    )
    await _seed_source(
        service, "ws-a", tag="a3", domain="dom-alpha", node_text="needle three"
    )
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    reader = await open_published_reader(
        factory, "ws-a", profile=ref.profile, pin_lease_seconds=None
    )
    assert reader is not None
    try:
        page_one = await reader.search('"needle"', limit=2, offset=0)
        page_two = await reader.search('"needle"', limit=2, offset=2)
        full = await reader.search('"needle"', limit=10, offset=0)
        assert len(page_one) == 2
        assert [h.row_index for h in page_one + page_two] == [
            h.row_index for h in full
        ]
        # An offset past the end is an honest empty page, never an error.
        assert await reader.search('"needle"', limit=2, offset=50) == ()
        with pytest.raises(KernelError, match="offset"):
            await reader.search('"needle"', limit=2, offset=-1)
    finally:
        await reader.close()


async def test_partition_publication_resolves_by_derived_profile(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _seed_source(
        service, "ws-a", tag="alpha", domain="dom-alpha", node_text="needle"
    )
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    ref = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )
    resolved = await resolve_published_set(
        factory, "ws-a", profile=high_assurance_profile(["dom-alpha"])
    )
    assert resolved is not None
    assert resolved.publication_set_id == ref.publication_set_id
