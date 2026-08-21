"""Dual-backend lexical publication conformance (PR83B2).

Everything in this file runs against a REAL backend per parametrized
fixture: SQLite (FTS5 virtual table) and PostgreSQL 16 (tsvector + GIN
per-generation table). No mocks, no skip escape in strict industrial
mode. It proves the full industrial story end to end — build, validate,
activate, serve, continue, isolate, race, fail, and retire — with the
physical artifact inspected in each backend's own catalog.

Documented, deliberate tokenizer divergences (see the PR83B2 evidence
bundle) are asserted *per backend*, never papered over: PostgreSQL's
``simple`` configuration does not fold Latin diacritics while FTS5
``unicode61`` does; everything else in the logical query contract
(terms AND/OR, phrase adjacency, case folding, numeric tokens) must
hold identically on both.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError, PublicationIntegrityError
from app.kernel.gc import execute_collection, plan_collection
from app.kernel.lexical import (
    POSTGRES_LEXICAL_INDEX_ID,
    POSTGRES_LEXICAL_TOKENIZER,
    POSTGRES_TEXT_SEARCH_CONFIG,
    SQLITE_LEXICAL_TOKENIZER,
    lexical_query_hash,
)
from app.kernel.models import KernelLexicalGeneration
from app.kernel.publications import (
    PHASE_PUB_LEXICAL_ROWS_MATERIALIZED,
    PHASE_PUB_LEXICAL_VALIDATE_BEGIN,
    PHASE_PUB_PRE_ACTIVATE,
    PublicationService,
    acquire_publication_pin,
    compute_lexical_identity,
    fts_table_name,
    high_assurance_profile,
    open_pinned_publication,
    open_published_reader,
    release_publication_pin,
    resolve_published_set,
    verify_lexical_generation,
)
from app.kernel.records import (
    SOURCE_CONSISTENCY_NATIVE_ATOMIC,
    ContentRevisionRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
from app.kernel.patches import ViewDocumentRecord
from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)
from tests.test_kernel_publication import _materialized

pytestmark = pytest.mark.asyncio


@dataclasses.dataclass
class LexicalEnv:
    backend: str
    engine: object
    session_factory: async_sessionmaker
    store: object
    service: KernelCommitService
    pubs: PublicationService
    server_version: str


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def lex_env(backend: str, tmp_path: pathlib.Path):
    """Fresh migrated database + commit/publication services on a real
    backend (real PostgreSQL when the profile says so; strict mode
    refuses to skip)."""
    async with provisioned_database(
        backend, (tmp_path / "kernel.db").as_posix()
    ) as prov:
        result = await upgrade_database(url=prov.url)
        assert result.to_revision, "bootstrap must reach a migration head"
        engine = create_async_engine(prov.url, **engine_kwargs_for(backend))
        assert engine.dialect.name == backend
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        from app.kernel.payloads import LocalPayloadStore

        store = LocalPayloadStore(tmp_path / "payloads")
        service = KernelCommitService(session_factory, payload_store=store)
        server_version = ""
        if backend == "postgresql":
            async with engine.connect() as conn:
                server_version = await conn.scalar(text("SELECT version()"))
        try:
            yield LexicalEnv(
                backend=backend,
                engine=engine,
                session_factory=session_factory,
                store=store,
                service=service,
                pubs=PublicationService(session_factory),
                server_version=server_version,
            )
        finally:
            await engine.dispose()


def _doc_view(view_id: str, text: str) -> ViewDocumentRecord:
    """One single-node view document under its own distinct view id."""
    graph = ReadingOrderGraph.build((OrderNode(node_id="n1"),), ())
    return ViewDocumentRecord(
        record_id=f"view.{view_id}",
        content_revision_ref=f"rev.{view_id}",
        graph=graph,
        texts={"n1": text},
        view_id=view_id,
    )


async def _commit_doc(env: LexicalEnv, workspace: str, view: ViewDocumentRecord) -> None:
    await env.service.commit(
        KernelCommitBatch(workspace_id=workspace, records=(view,))
    )


async def _publish(env: LexicalEnv, workspace: str, views: list[ViewDocumentRecord]):
    """Commit documents and publish their materialized generation.

    Documents carry distinct view ids and are committed without view-head
    advancement: the lexical corpus is derived from generation records
    (latest revision per view at the cut), so this is the honest multi-
    document shape on both backends.
    """
    for view in views:
        await _commit_doc(env, workspace, view)
    gen = await _materialized(env.session_factory, workspace)
    ref = await env.pubs.publish(materialized_generation_id=gen.generation_id)
    return gen, ref


async def _physical_table_exists(env: LexicalEnv, table: str) -> bool:
    async with env.session_factory() as session:
        if env.backend == "postgresql":
            return bool(
                await session.scalar(
                    text("SELECT to_regclass(:name) IS NOT NULL"),
                    {"name": f'public."{table}"'},
                )
            )
        row = await session.scalar(
            text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": table},
        )
        return bool(row)


async def _gin_index_valid(env: LexicalEnv, table: str) -> bool | None:
    if env.backend != "postgresql":
        return None
    async with env.session_factory() as session:
        return await session.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :ixname AND i.indrelid = CAST(:relname AS regclass)"
            ),
            {"ixname": f"{table}_tsv_ix", "relname": table},
        )


# ---------------------------------------------------------------------------
# Build / validate / activate / open on a real backend
# ---------------------------------------------------------------------------


async def test_publish_and_serve_lexical_on_real_backend(lex_env) -> None:
    env = lex_env
    assert env.backend != "postgresql" or "PostgreSQL 16" in env.server_version
    gen, ref = await _publish(env, "ws-conf", [_doc_view("one", "alpha beta")])
    assert ref.state == "published"
    reader = await open_published_reader(env.session_factory, "ws-conf")
    assert reader is not None
    try:
        hits = await reader.search("alpha")
        assert [hit.record_id for hit in hits] == ["view.one"]
        assert hits[0].text == "alpha beta"
    finally:
        await reader.close()


async def test_lexical_generation_identity_is_backend_scoped(lex_env) -> None:
    """The manifest must describe the physical projection honestly."""
    env = lex_env
    _, ref = await _publish(env, "ws-id", [_doc_view("one", "needle")])
    async with env.session_factory() as session:
        manifest = await session.get(
            KernelLexicalGeneration, ref.lexical_generation_id
        )
    assert manifest is not None
    if env.backend == "postgresql":
        assert manifest.tokenizer == POSTGRES_LEXICAL_TOKENIZER
        assert POSTGRES_TEXT_SEARCH_CONFIG in manifest.tokenizer_config_json
        # Identity embeds the pg index id: byte-different from the FTS5
        # identity over identical declared inputs.
        fts5_id = compute_lexical_identity(
            workspace_id=manifest.workspace_id,
            kernel_commit_id=manifest.kernel_commit_id,
            snapshot_id=manifest.snapshot_id,
            source_generation_id=manifest.source_generation_id,
            tokenizer="unicode61",
        )
        assert manifest.lexical_generation_id != fts5_id
        assert manifest.lexical_generation_id == compute_lexical_identity(
            workspace_id=manifest.workspace_id,
            kernel_commit_id=manifest.kernel_commit_id,
            snapshot_id=manifest.snapshot_id,
            source_generation_id=manifest.source_generation_id,
            tokenizer=POSTGRES_LEXICAL_TOKENIZER,
            tokenizer_config_json=manifest.tokenizer_config_json,
            index_id=POSTGRES_LEXICAL_INDEX_ID,
        )
        assert len(manifest.fts_table) <= 63
        assert manifest.fts_table == fts_table_name(
            manifest.lexical_generation_id, backend="postgresql"
        )
    else:
        assert manifest.tokenizer == SQLITE_LEXICAL_TOKENIZER
        # Byte-compat with the pre-PR83B2 SQLite identity.
        assert manifest.lexical_generation_id == compute_lexical_identity(
            workspace_id=manifest.workspace_id,
            kernel_commit_id=manifest.kernel_commit_id,
            snapshot_id=manifest.snapshot_id,
            source_generation_id=manifest.source_generation_id,
            tokenizer=SQLITE_LEXICAL_TOKENIZER,
        )
    assert await _physical_table_exists(env, manifest.fts_table)
    verification = await verify_lexical_generation(
        env.session_factory, ref.lexical_generation_id
    )
    assert verification.ok, verification.problems


async def test_postgres_artifact_is_a_valid_gin_indexed_tsvector_table(
    lex_env,
) -> None:
    env = lex_env
    _, ref = await _publish(env, "ws-phys", [_doc_view("one", "haystack needle")])
    async with env.session_factory() as session:
        manifest = await session.get(KernelLexicalGeneration, ref.lexical_generation_id)
    assert manifest is not None
    if env.backend == "postgresql":
        # The GIN index must exist and be valid, and the stored tsvector
        # must be the regeneration under the pinned config.
        assert await _gin_index_valid(env, manifest.fts_table) is True
        async with env.session_factory() as session:
            async with session.begin():
                diverged = await session.scalar(
                    text(
                        f'SELECT count(*) FROM "{manifest.fts_table}" '
                        "WHERE tsv IS DISTINCT FROM "
                        "to_tsvector('simple'::regconfig, \"text\")"
                    )
                )
                lexemes = await session.scalar(
                    text(
                        f'SELECT tsv FROM "{manifest.fts_table}" '
                        "WHERE row_index = 0"
                    )
                )
        assert diverged == 0
        assert "needle" in str(lexemes)
    else:
        # SQLite keeps its FTS5 virtual table.
        async with env.session_factory() as session:
            ddl = await session.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE name = :n"
                ),
                {"n": manifest.fts_table},
            )
        assert "fts5" in str(ddl)


async def test_physical_tamper_fails_closed_on_real_backend(lex_env) -> None:
    env = lex_env
    _, ref = await _publish(env, "ws-tamper", [_doc_view("one", "original needle")])
    async with env.session_factory() as session:
        manifest = await session.get(KernelLexicalGeneration, ref.lexical_generation_id)
    assert manifest is not None
    async with env.session_factory() as session:
        async with session.begin():
            if env.backend == "postgresql":
                await session.execute(
                    text(
                        f'UPDATE "{manifest.fts_table}" SET "text" = :t '
                        "WHERE row_index = 0"
                    ),
                    {"t": "tampered needle payload"},
                )
            else:
                await session.execute(
                    text(
                        f'UPDATE "{manifest.fts_table}" SET text = :t '
                        "WHERE rowid = 0"
                    ),
                    {"t": "tampered needle payload"},
                )
    verification = await verify_lexical_generation(
        env.session_factory, ref.lexical_generation_id
    )
    assert not verification.ok
    assert verification.problems
    # Serving must also fail closed against the tampered artifact.
    reader = await open_pinned_publication(
        env.session_factory, ref.publication_set_id
    )
    try:
        with pytest.raises(PublicationIntegrityError):
            await reader.search("needle")
    finally:
        await reader.close()


async def test_empty_corpus_generation_valid_on_real_backend(lex_env) -> None:
    env = lex_env
    await _commit_doc(env, "ws-empty", _doc_view("empty", "content"))
    gen = await _materialized(env.session_factory, "ws-empty")
    ref = await env.pubs.publish(materialized_generation_id=gen.generation_id)
    reader = await open_published_reader(env.session_factory, "ws-empty")
    assert reader is not None
    try:
        assert await reader.search("anything") == ()
    finally:
        await reader.close()
    assert ref.state == "published"


# ---------------------------------------------------------------------------
# Query semantics matrix (tokenizer differences asserted per backend)
# ---------------------------------------------------------------------------


async def test_query_semantics_matrix(lex_env) -> None:
    env = lex_env
    docs = {
        "case": "The Quick Brown Fox jumps",
        "diacritic": "café régime naïve",
        "hyphen": "alpha-beta gamma delta",
        "numeric": "needle in 1234 haystack",
        "repeat": "needle needle needle repetition",
        "punct": "termination. semi;colon, end",
        "unicode": "résumé και δύο 日本語",
    }
    views = [_doc_view(view_id, body) for view_id, body in docs.items()]
    _, _ = await _publish(env, "ws-matrix", views)
    reader = await open_published_reader(env.session_factory, "ws-matrix")
    assert reader is not None

    async def hit_ids(text: str, mode: str = "all_terms") -> list[str]:
        hits = await reader.search(text, mode, limit=50)
        return sorted(hit.record_id for hit in hits)

    try:
        # Case folding: identical on both profiles.
        assert await hit_ids("quick") == ["view.case"]
        assert await hit_ids("QUICK") == ["view.case"]
        # Numeric tokens: identical.
        assert await hit_ids("1234") == ["view.numeric"]
        # Punctuation-adjacent terms: identical.
        assert await hit_ids("termination") == ["view.punct"]
        assert await hit_ids("semi") == ["view.punct"]
        # Hyphenated token members match as terms on both; the phrase
        # keeps adjacency on both (FTS5 quoted phrase / phraseto <->).
        assert await hit_ids("alpha") == ["view.hyphen"]
        assert await hit_ids("alpha beta", "phrase") == ["view.hyphen"]
        assert await hit_ids("alpha gamma", "phrase") == []
        # Repeated terms: plain membership identical.
        assert await hit_ids("needle") == ["view.numeric", "view.repeat"]
        # all_terms intersects across docs; any_term unions.
        assert await hit_ids("needle haystack") == ["view.numeric"]
        assert await hit_ids("quick needle", "any_term") == [
            "view.case",
            "view.numeric",
            "view.repeat",
        ]
        # Unicode letters outside ASCII match as literal terms on both.
        assert await hit_ids("日本語") == ["view.unicode"]
        # No-match and near-miss stay empty on both.
        assert await hit_ids("nonexistent") == []
        assert await hit_ids("quick foxes zebra", "all_terms") == []

        # Deliberate, documented diacritics divergence: unicode61 folds
        # Latin diacritics (café→cafe), PostgreSQL 'simple' does not.
        assert await hit_ids("café") == ["view.diacritic"]
        if env.backend == "postgresql":
            assert await hit_ids("cafe") == []
        else:
            assert await hit_ids("cafe") == ["view.diacritic"]
    finally:
        await reader.close()


async def test_operator_bearing_input_is_literal_on_real_backend(lex_env) -> None:
    env = lex_env
    await _publish(env, "ws-adversarial", [_doc_view("one", "plain text body")])
    reader = await open_published_reader(env.session_factory, "ws-adversarial")
    assert reader is not None
    try:
        for adversarial in (
            'OR NEAR(a b) AND "quoted" column:filter',
            "'; DROP TABLE x; --",
            "a & b | !c",
            "<script>* ? ( )",
        ):
            # Never raises a backend grammar error and never matches the
            # plain corpus: input is literal content, compiled through
            # bound parameters only.
            assert await reader.search(adversarial, "all_terms") == ()
        assert await reader.search("plain body") != ()
    finally:
        await reader.close()


# ---------------------------------------------------------------------------
# Ranking determinism and direction
# ---------------------------------------------------------------------------


async def test_ranking_best_first_and_direction_per_backend(lex_env) -> None:
    env = lex_env
    views = [
        _doc_view("rare", "zebra context"),
        _doc_view("frequent", "zebra zebra zebra zebra"),
        _doc_view("mid", "zebra zebra"),
    ]
    await _publish(env, "ws-rank", views)
    reader = await open_published_reader(env.session_factory, "ws-rank")
    assert reader is not None
    try:
        first = await reader.search("zebra", limit=10)
        second = await reader.search("zebra", limit=10)
        # Deterministic: identical order for the immutable generation.
        assert [h.record_id for h in first] == [h.record_id for h in second]
        # Best-first by the backend's documented contract: more zebra →
        # better on both (higher ts_rank on PG, lower bm25 on SQLite).
        assert first[0].record_id == "view.frequent"
        assert first[-1].record_id == "view.rare"
        ranks = [h.rank for h in first]
        if env.backend == "postgresql":
            # ts_rank: better is numerically larger — the FTS5 direction
            # must NOT be inherited.
            assert ranks == sorted(ranks, reverse=True)
            assert ranks[0] > ranks[-1]
        else:
            assert ranks == sorted(ranks)
            assert ranks[0] < ranks[-1]
    finally:
        await reader.close()


# ---------------------------------------------------------------------------
# Keyset traversal invariance (ties, page sizes, activation pinning)
# ---------------------------------------------------------------------------


def _tie_corpus() -> list[ViewDocumentRecord]:
    # Exact-tie documents (identical text) + graded documents: the
    # traversal must be page-boundary-invariant under both.
    views = []
    for index in range(4):
        views.append(_doc_view(f"tie{index}", "needle"))
    views.append(_doc_view("mid", "needle needle"))
    views.append(_doc_view("strong", "needle needle needle"))
    return views


async def _traverse_all(env: LexicalEnv, reader, text: str, page_size: int):
    rows: list[tuple[str, int]] = []
    after = None
    while True:
        page = await reader.search_after(text, "all_terms", limit=page_size, after=after)
        rows.extend((hit.record_id, hit.row_index) for hit in page.hits)
        if not page.has_more or not page.next_after:
            return rows
        after = page.next_after


async def test_keyset_traversal_invariant_to_page_size(lex_env) -> None:
    env = lex_env
    await _publish(env, "ws-keyset", _tie_corpus())
    reader = await open_published_reader(env.session_factory, "ws-keyset")
    assert reader is not None
    try:
        canonical = [
            (hit.record_id, hit.row_index)
            for hit in await reader.search("needle", limit=100)
        ]
        # Distinct + tie scores, several pages, non-divisor sizes.
        for page_size in (1, 2, 3, 7):
            walked = await _traverse_all(env, reader, "needle", page_size)
            assert walked == canonical, f"page size {page_size} diverged"
            assert len({row for row in walked}) == len(walked)  # no duplicates
        assert len(canonical) == 6  # no gaps: every corpus row present
    finally:
        await reader.close()


async def test_continuation_survives_activation_under_pin(lex_env) -> None:
    """I2/D: a cursor stays bound to its generation across a new
    publication while pinned; fresh readers see the new set; binding
    mismatches fail closed."""
    env = lex_env
    _, first = await _publish(env, "ws-pin", _tie_corpus())
    reader = await open_published_reader(env.session_factory, "ws-pin")
    assert reader is not None
    page_one = await reader.search_after("needle", "all_terms", limit=2)
    assert page_one.next_after is not None
    bound_after = page_one.next_after

    # Build and activate a disjoint second generation mid-traversal.
    await _commit_doc(env, "ws-pin", _doc_view("other", "unrelated words"))
    gen2 = await _materialized(env.session_factory, "ws-pin")
    second = await env.pubs.publish(materialized_generation_id=gen2.generation_id)
    assert second.publication_set_id != first.publication_set_id

    # The already-open reader continues coherently on the OLD generation.
    page_two = await reader.search_after(
        "needle", "all_terms", limit=2, after=bound_after
    )
    remaining = [hit.record_id for hit in page_two.hits]
    walked = [hit.record_id for hit in page_one.hits] + remaining
    canonical = [
        hit.record_id for hit in await reader.search("needle", limit=100)
    ]
    assert walked == canonical[: len(walked)]
    await reader.close()

    # A fresh reader resolves the NEW set; a cursor from the old set
    # never resumes against it.
    fresh = await open_published_reader(env.session_factory, "ws-pin")
    assert fresh is not None
    try:
        assert [h.record_id for h in await fresh.search("unrelated")] == ["view.other"]
        with pytest.raises(PublicationIntegrityError):
            await fresh.search_after(
                "needle", "all_terms", limit=2, after=bound_after
            )
    finally:
        await fresh.close()


async def test_continuation_binding_mismatch_fail_safe(lex_env) -> None:
    env = lex_env
    _, ref = await _publish(env, "ws-bind", _tie_corpus())
    reader = await open_pinned_publication(env.session_factory, ref.publication_set_id)
    from app.kernel.publications import LexicalSearchAfter

    page = await reader.search_after("needle", "all_terms", limit=1)
    assert page.next_after is not None

    # Query mismatch: same reader, different logical query.
    with pytest.raises(PublicationIntegrityError):
        await reader.search_after(
            "zebra", "all_terms", limit=1, after=page.next_after
        )
    # Mode is part of the logical query identity.
    with pytest.raises(PublicationIntegrityError):
        await reader.search_after(
            "needle", "any_term", limit=1, after=page.next_after
        )
    # A pre-PR83B2 cursor (query_hash over a compiled FTS5 string) is
    # rejected as a binding mismatch, never silently resumed.
    legacy = LexicalSearchAfter(
        publication_set_id=page.next_after.publication_set_id,
        lexical_generation_id=page.next_after.lexical_generation_id,
        rank=page.next_after.rank,
        row_index=page.next_after.row_index,
        query_hash=lexical_query_hash("needle", "all_terms").upper(),
    )
    with pytest.raises(PublicationIntegrityError):
        await reader.search_after("needle", "all_terms", limit=1, after=legacy)
    await reader.close()


# ---------------------------------------------------------------------------
# High-assurance authorization partition on a real backend
# ---------------------------------------------------------------------------


def _blob(value: int) -> str:
    return f"sha256:{value:064x}"


async def _seed_domain_doc(
    env: LexicalEnv, workspace: str, tag: str, domain: str, node_text: str
) -> None:
    source = SourceIdentityRecord(
        record_id=f"src.{tag}",
        source_kind="local_path",
        source_key=f"C:/docs/{tag}.md",
    )
    revision = ContentRevisionRecord(
        record_id=f"rev.{tag}",
        source_ref=source.record_id,
        blob_key=_blob(abs(hash(tag)) % (1 << 63)),
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
    graph = ReadingOrderGraph.build((OrderNode(node_id="n1"),), ())
    view = ViewDocumentRecord(
        record_id=f"view.{tag}",
        content_revision_ref=revision.record_id,
        graph=graph,
        texts={"n1": node_text},
        view_id=f"doc-{tag}",
    )
    await env.service.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(source, revision, assignment, view),
        )
    )


async def test_high_assurance_partition_isolates_forbidden_corpus(lex_env) -> None:
    """F6: forbidden content is structurally outside the partition's own
    generation — it cannot leak rows, ranks, counts, or metadata, and a
    highly relevant forbidden document cannot outrank allowed results."""
    env = lex_env
    workspace = "ws-ha"
    await _seed_domain_doc(
        env, workspace, "allowed", "domain-open", "single needle sighting"
    )
    await _seed_domain_doc(
        env,
        workspace,
        "forbidden",
        "domain-secret",
        "needle needle needle needle needle",
    )
    gen = await _materialized(env.session_factory, workspace)
    # Sanity: the SHARED generation sees both documents.
    shared = await env.pubs.publish(materialized_generation_id=gen.generation_id)
    shared_reader = await open_published_reader(env.session_factory, workspace)
    assert shared_reader is not None
    assert len(await shared_reader.search("needle")) == 2
    await shared_reader.close()

    # The partition sees only the allowed domain.
    partition_ref = await env.pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"domain-open"}),
    )
    ha_profile = partition_ref.profile
    assert partition_ref.publication_set_id != shared.publication_set_id
    reader = await open_published_reader(
        env.session_factory, workspace, profile=ha_profile
    )
    assert reader is not None
    try:
        hits = await reader.search("needle")
        assert [hit.record_id for hit in hits] == ["view.allowed"]
        # The forbidden corpus cannot influence ranking among allowed
        # rows or expose any metadata (counts/ids) of any kind.
        assert all("forbidden" not in hit.record_id for hit in hits)
        keyset = await reader.search_after("needle", "all_terms", limit=1)
        assert len(keyset.hits) == 1
        assert keyset.has_more is False
        assert keyset.next_after is None
    finally:
        await reader.close()
    # A never-published partition fails closed (no shared fallback).
    absent = await open_published_reader(
        env.session_factory, workspace, profile=high_assurance_profile({"no-such"})
    )
    assert absent is None


# ---------------------------------------------------------------------------
# Mixed-generation coherence during activation (controlled race)
# ---------------------------------------------------------------------------


async def test_readers_churning_across_activation_never_blend(lex_env) -> None:
    """11.7/D: while a second generation activates, concurrent readers
    each see exactly one generation per page — never a hybrid."""
    import asyncio

    env = lex_env
    workspace = "ws-race"
    _, first = await _publish(env, "ws-race", _tie_corpus())

    async def reader_loop(rounds: int) -> list[frozenset]:
        page_sets: list[frozenset] = []
        for _ in range(rounds):
            reader = await open_published_reader(env.session_factory, workspace)
            assert reader is not None
            try:
                after = None
                for _step in range(3):
                    page = await reader.search_after(
                        "needle", "all_terms", limit=2, after=after
                    )
                    if page.hits:
                        # One page carries exactly one publication-set
                        # identity: no hybrid generation ever surfaces.
                        page_sets.append(
                            frozenset(hit.publication_set_id for hit in page.hits)
                        )
                        assert len(page_sets[-1]) == 1
                    if not page.has_more or page.next_after is None:
                        break
                    after = page.next_after
            finally:
                await reader.close()
        return page_sets

    await _commit_doc(env, workspace, _doc_view("next", "needle zebra"))
    gen2 = await _materialized(env.session_factory, workspace)
    readers = [asyncio.create_task(reader_loop(2)) for _ in range(3)]
    await asyncio.sleep(0)  # let readers start on the old generation
    second = await env.pubs.publish(materialized_generation_id=gen2.generation_id)
    results = await asyncio.gather(*readers)
    # Every observed page belonged to exactly one coherent set; readers
    # that resolved before activation saw the old set, after it the new.
    observed = {set_id for pages in results for page in pages for set_id in page}
    assert observed
    assert observed <= {first.publication_set_id, second.publication_set_id}
    # After activation, a fresh reader resolves the new set.
    fresh = await open_published_reader(env.session_factory, workspace)
    assert fresh is not None
    try:
        hits = await fresh.search("zebra")
        assert [h.record_id for h in hits] == ["view.next"]
        assert hits[0].publication_set_id == second.publication_set_id
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Failure injection at build/validation/activation boundaries
# ---------------------------------------------------------------------------


async def test_staged_crash_residue_is_resumable_and_invisible(lex_env) -> None:
    env = lex_env
    await _commit_doc(env, "ws-fault", _doc_view("one", "needle"))
    gen = await _materialized(env.session_factory, "ws-fault")
    with pytest.raises(InjectedFaultError):
        await env.pubs.build_lexical(
            gen.generation_id, _inject_fault_at=PHASE_PUB_LEXICAL_VALIDATE_BEGIN
        )
    async with env.session_factory() as session:
        staged_rows = list(
            (
                await session.execute(
                    select(KernelLexicalGeneration).where(
                        KernelLexicalGeneration.state == "staged"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(staged_rows) == 1
    staged = staged_rows[0]
    # Nothing is query-visible: no published set exists yet.
    assert await resolve_published_set(env.session_factory, "ws-fault") is None
    # The physical artifact exists (staging committed) and is intact.
    assert await _physical_table_exists(env, staged.fts_table)
    resumed = await env.pubs.validate_lexical(staged.lexical_generation_id)
    assert resumed.state == "validated"


async def test_failed_build_leaves_no_physical_residue(lex_env) -> None:
    """I11 on PostgreSQL: DDL is transactional, so an injected failure
    before the staging commit must leave no table behind; retry then
    converges."""
    env = lex_env
    await _commit_doc(env, "ws-residue", _doc_view("one", "needle"))
    gen = await _materialized(env.session_factory, "ws-residue")
    with pytest.raises(InjectedFaultError):
        await env.pubs.publish(
            materialized_generation_id=gen.generation_id,
            _inject_fault_at=PHASE_PUB_LEXICAL_ROWS_MATERIALIZED,
        )
    # No manifest rows, no physical tables with our prefix.
    async with env.session_factory() as session:
        if env.backend == "postgresql":
            orphans = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE tablename LIKE 'kernel_fts_%'"
                )
            )
        else:
            orphans = await session.scalar(
                text(
                    "SELECT count(*) FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'kernel_fts_%'"
                )
            )
    assert orphans == 0
    ref = await env.pubs.publish(materialized_generation_id=gen.generation_id)
    assert ref.state == "published"


async def test_pre_activate_fault_rolls_back_then_converges(lex_env) -> None:
    env = lex_env
    _, first = await _publish(env, "ws-act", [_doc_view("one", "alpha")])
    await _commit_doc(env, "ws-act", _doc_view("two", "beta"))
    gen2 = await _materialized(env.session_factory, "ws-act")
    with pytest.raises(InjectedFaultError):
        await env.pubs.publish(
            materialized_generation_id=gen2.generation_id,
            _inject_fault_at=PHASE_PUB_PRE_ACTIVATE,
        )
    # Old publication still authoritative.
    resolved = await resolve_published_set(env.session_factory, "ws-act")
    assert resolved is not None
    assert resolved.publication_set_id == first.publication_set_id
    # Retry converges to the new set.
    second = await env.pubs.publish(materialized_generation_id=gen2.generation_id)
    resolved = await resolve_published_set(env.session_factory, "ws-act")
    assert resolved is not None
    assert resolved.publication_set_id == second.publication_set_id


# ---------------------------------------------------------------------------
# GC: proof-closed retirement of the real physical artifact
# ---------------------------------------------------------------------------


async def test_gc_retires_real_artifact_proof_closed(lex_env) -> None:
    env = lex_env
    _, first = await _publish(env, "ws-gc", _tie_corpus())
    await _commit_doc(env, "ws-gc", _doc_view("next", "fresh"))
    gen2 = await _materialized(env.session_factory, "ws-gc")
    second = await env.pubs.publish(materialized_generation_id=gen2.generation_id)

    async with env.session_factory() as session:
        first_manifest = await session.get(
            KernelLexicalGeneration, first.lexical_generation_id
        )
        second_manifest = await session.get(
            KernelLexicalGeneration, second.lexical_generation_id
        )
    assert first_manifest is not None and second_manifest is not None

    # While an unexpired pin protects the superseded set, its generation
    # is rescued.
    pin = await acquire_publication_pin(
        env.session_factory, first.publication_set_id, lease_seconds=60.0
    )
    plan = await plan_collection(env.session_factory, env.store)
    assert first.lexical_generation_id not in plan.eligible_lexical_generations
    await execute_collection(env.session_factory, env.store, plan)
    assert await _physical_table_exists(env, first_manifest.fts_table)

    # Release the pin: the superseded set and its generation retire; the
    # physical table disappears from the backend catalog; the CURRENT
    # artifact survives untouched.
    await release_publication_pin(env.session_factory, pin.pin_id)
    plan = await plan_collection(env.session_factory, env.store)
    assert first.lexical_generation_id in plan.eligible_lexical_generations
    report = await execute_collection(env.session_factory, env.store, plan)
    assert report.lexical_generations_retired >= 1
    assert not await _physical_table_exists(env, first_manifest.fts_table)
    assert await _physical_table_exists(env, second_manifest.fts_table)
    reader = await open_published_reader(env.session_factory, "ws-gc")
    assert reader is not None
    try:
        assert [h.record_id for h in await reader.search("fresh")] == ["view.next"]
    finally:
        await reader.close()
    # Idempotent: a second collection pass changes nothing.
    plan2 = await plan_collection(env.session_factory, env.store)
    await execute_collection(env.session_factory, env.store, plan2)
    assert await _physical_table_exists(env, second_manifest.fts_table)


# ---------------------------------------------------------------------------
# Query-plan sanity: the GIN index must be usable by the planner (PG)
# ---------------------------------------------------------------------------


async def test_query_plan_and_artifact_shape_sanity(lex_env) -> None:
    """Query-plan sanity (11.11), backend-appropriate.

    PostgreSQL: the cost model legitimately prefers a sequential scan on
    small relations (tsvector has no MCV statistics, so GIN selectivity
    estimates are coarse) — observed directly: even 50k-row corpora plan
    as Seq Scan by default. The property that must NOT hold is "the
    query shape can never use the index": with sequential scans disabled
    the planner must produce a Bitmap Index Scan over the generation's
    GIN index, proving the served predicate matches the indexed column.
    SQLite: FTS5 serves from its own virtual-table structure; the
    artifact-shape sanity is that the physical table really is an FTS5
    table with the unicode61 tokenizer.
    """
    env = lex_env
    workspace = "ws-plan"
    views = []
    for index in range(1000):
        if index == 7:
            views.append(_doc_view(f"d{index}", f"document {index} xyzzamus rare"))
        else:
            views.append(_doc_view(f"d{index}", f"document {index} ordinary text"))
    await _publish(env, workspace, views)
    reader = await open_published_reader(env.session_factory, workspace)
    assert reader is not None
    try:
        hits = await reader.search("xyzzamus")
        assert [h.record_id for h in hits] == ["view.d7"]
    finally:
        await reader.close()
    async with env.session_factory() as session:
        resolved = await resolve_published_set(env.session_factory, workspace)
        assert resolved is not None
        manifest = await session.get(
            KernelLexicalGeneration, resolved.lexical_generation_id
        )
        assert manifest is not None
        if env.backend == "sqlite":
            ddl = await session.scalar(
                text("SELECT sql FROM sqlite_master WHERE name = :n"),
                {"n": manifest.fts_table},
            )
            assert "fts5" in str(ddl) and "unicode61" in str(ddl)
            return
        # The session autobegins a transaction; SET LOCAL scopes the
        # planner override to it and vanishes at commit/rollback.
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan_rows = (
            (
                await session.execute(
                    text(
                        f'EXPLAIN (COSTS OFF) SELECT row_index FROM '
                        f'"{manifest.fts_table}" '
                        "WHERE tsv @@ (phraseto_tsquery('simple', :term))"
                    ),
                    {"term": "xyzzamus"},
                )
            )
            .scalars()
            .all()
        )
    plan_text = "\n".join(str(line) for line in plan_rows)
    assert "Bitmap Index Scan" in plan_text, (
        f"planner cannot use the GIN index; plan: {plan_text}"
    )
    assert f"{manifest.fts_table}_tsv_ix" in plan_text
