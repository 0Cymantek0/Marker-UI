"""Publication set and lexical generation tests (V3.2 PR76, plan
matrices 10 + 11).

Deterministic lexical identity over declared inputs, corpus selection
from the pinned materialized generation only, staging invisibility,
immutability on rebuild, deep verification catching tampering, empty
corpora, Unicode/duplicate-text fixtures, and atomic set validation and
activation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    KernelError,
    LexicalIntegrityError,
    LexicalQueryError,
    UnknownGenerationError,
)
from app.kernel.generations import GenerationService
from app.kernel.models import KernelLexicalRow
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord
from app.kernel.publications import (
    LEXICAL_STATE_STAGED,
    LEXICAL_STATE_VALIDATED,
    LEXICAL_TOKENIZER,
    PublicationReader,
    PublicationService,
    active_publication_pins,
    extract_lexical_corpus,
    fts_table_name,
    open_published_reader,
    resolve_published_set,
    verify_lexical_generation,
    verify_publication_set,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
from app.utils.canonical import payload_byte_hash
from app.kernel.records import ClaimAssertionRecord
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


def _sql_rows(db_path: Path, statement: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return list(conn.execute(statement, params))


async def _materialized(factory: async_sessionmaker, workspace: str):
    gen_service = GenerationService(factory)
    return await gen_service.build_and_activate(
        await resolve_snapshot(factory, workspace)
    )


async def _lexical_rows(
    factory: async_sessionmaker, lexical_generation_id: str
) -> list[KernelLexicalRow]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(KernelLexicalRow)
                    .where(
                        KernelLexicalRow.lexical_generation_id == lexical_generation_id
                    )
                    .order_by(KernelLexicalRow.row_index.asc())
                )
            )
            .scalars()
            .all()
        )


def _row_tuples(rows: list[KernelLexicalRow]) -> list[tuple]:
    return [
        (
            row.row_index,
            row.record_id,
            row.view_id,
            row.node_id,
            row.revision_ref,
            row.text_hash,
            row.text_chars,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# corpus extraction (pure)
# ---------------------------------------------------------------------------


def _corpus_record(record_id: str, commit: int, payload: dict) -> object:
    from app.kernel.publications import _CorpusRecord

    return _CorpusRecord(
        record_id=record_id,
        kernel_commit_id=commit,
        identity_hash=f"sha256:{record_id}",
        payload=payload,
    )


async def test_corpus_extracts_latest_revision_per_view_only() -> None:
    records = [
        _corpus_record(
            "view-1",
            1,
            {"texts": {"n1": "alpha one", "n2": "alpha two"}},
        ),
        _corpus_record(
            "view-2",
            2,
            {"texts": {"n1": "beta one", "n2": "beta two"}},
        ),
    ]
    rows = extract_lexical_corpus(records)
    assert [(row.view_id, row.node_id, row.text) for row in rows] == [
        ("document", "n1", "beta one"),
        ("document", "n2", "beta two"),
    ]
    assert all(row.revision_ref == "sha256:view-2" for row in rows)
    assert all(row.record_id == "view-2" for row in rows)


async def test_corpus_groups_by_view_and_orders_deterministically() -> None:
    records = [
        _corpus_record(
            "view-a",
            3,
            {"texts": {"z-node": "late text", "a-node": "early text"}},
        ),
        _corpus_record(
            "view-b",
            1,
            {"texts": {"m-node": "other view text"}, "view_id": "aux"},
        ),
    ]
    rows = extract_lexical_corpus(records)
    assert [(row.view_id, row.node_id) for row in rows] == [
        ("aux", "m-node"),
        ("document", "a-node"),
        ("document", "z-node"),
    ]


async def test_corpus_rejects_non_mapping_texts() -> None:
    records = [_corpus_record("view-1", 1, {"texts": ["not", "a", "mapping"]})]
    with pytest.raises(LexicalIntegrityError):
        extract_lexical_corpus(records)


# ---------------------------------------------------------------------------
# lexical generation build determinism + content
# ---------------------------------------------------------------------------


async def test_lexical_build_deterministic_and_idempotent(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await _materialized(factory, "ws-a")

    pubs = PublicationService(factory)
    first = await pubs.build_lexical(gen.generation_id)
    rows_after_first = await _lexical_rows(factory, first.lexical_generation_id)
    fts_table = fts_table_name(first.lexical_generation_id)

    rebuilt = await pubs.build_lexical(gen.generation_id)
    assert rebuilt.lexical_generation_id == first.lexical_generation_id
    assert rebuilt.content_digest == first.content_digest
    assert rebuilt.state == LEXICAL_STATE_VALIDATED
    assert _row_tuples(await _lexical_rows(factory, first.lexical_generation_id)) == (
        _row_tuples(rows_after_first)
    )
    assert (await verify_lexical_generation(factory, first.lexical_generation_id)).ok

    # exactly one FTS virtual table exists and it belongs to this generation
    tables = {
        row[0]
        for row in _sql_rows(
            _db_path(factory),
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'kernel_fts_%'",
        )
    }
    assert tables == {
        fts_table,
        f"{fts_table}_data",
        f"{fts_table}_idx",
        f"{fts_table}_content",
        f"{fts_table}_docsize",
        f"{fts_table}_config",
    }
    assert _sql_rows(_db_path(factory), f'SELECT COUNT(*) FROM "{fts_table}"') == [
        (1,)
    ]


async def test_lexical_indexes_latest_revision_and_ignores_other_records(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    # a later committed view revision supersedes the corpus selection
    # even when the view head itself still names the first revision:
    # extraction is derived from generation content alone
    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                ClaimAssertionRecord(
                    claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
        )
    )
    gen = await _materialized(factory, "ws-a")

    pubs = PublicationService(factory)
    ref = await pubs.build_lexical(gen.generation_id)
    rows = await _lexical_rows(factory, ref.lexical_generation_id)

    assert ref.row_count == 1
    assert [(row.record_id, row.node_id) for row in rows] == [("view-2", "n1")]
    assert rows[0].revision_ref == _view("view-2", {"n1": "beta"}, "rev-s2").view_revision_id()
    assert rows[0].text_chars == len("beta")


async def test_lexical_empty_corpus_is_a_valid_empty_generation(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-empty",
            records=(
                ClaimAssertionRecord(
                    claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
        )
    )
    gen = await _materialized(factory, "ws-empty")

    pubs = PublicationService(factory)
    ref = await pubs.build_lexical(gen.generation_id)
    assert ref.row_count == 0 and ref.text_char_count == 0
    assert ref.state == LEXICAL_STATE_VALIDATED
    assert (await verify_lexical_generation(factory, ref.lexical_generation_id)).ok
    assert await _lexical_rows(factory, ref.lexical_generation_id) == []
    assert _sql_rows(
        _db_path(factory), f'SELECT COUNT(*) FROM "{ref.fts_table}"'
    ) == [(0,)]


async def test_lexical_unicode_and_duplicate_text_stay_distinguishable(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    duplicated = "naïve résumé café"
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    await _commit_view(
        service,
        "ws-u",
        _view(
            "view-1",
            {
                "left": duplicated,
                "right": duplicated,
                "composed": f"drink {composed} now",
                "decomposed": f"drink {decomposed} now",
            },
            "rev-s1",
        ),
    )
    gen = await _materialized(factory, "ws-u")

    pubs = PublicationService(factory)
    ref = await pubs.build_lexical(gen.generation_id)
    rows = await _lexical_rows(factory, ref.lexical_generation_id)

    assert ref.row_count == 4
    # identical text in distinct source locations: distinct rows, same hash
    left = next(row for row in rows if row.node_id == "left")
    right = next(row for row in rows if row.node_id == "right")
    assert left.text_hash == right.text_hash
    assert (left.row_index, right.row_index) == (2, 3)  # node-id ordered
    # unicode61 tokenizes composed/decomposed differently: documented,
    # deterministic, and reflected in distinct hashes
    composed_row = next(row for row in rows if row.node_id == "composed")
    decomposed_row = next(row for row in rows if row.node_id == "decomposed")
    assert composed_row.text_hash != decomposed_row.text_hash

    fts_table = ref.fts_table
    assert _sql_rows(
        _db_path(factory),
        f'SELECT COUNT(*) FROM "{fts_table}" WHERE "{fts_table}" MATCH ?',
        ("résumé",),
    ) == [(2,)]


async def test_lexical_large_text_row(payload_env: tuple) -> None:
    factory, store, service = payload_env
    big = ("lorem ipsum dolor sit amet " * 5000).strip()
    await _commit_view(service, "ws-big", _view("view-1", {"body": big}, "rev-s1"))
    gen = await _materialized(factory, "ws-big")

    pubs = PublicationService(factory)
    ref = await pubs.build_lexical(gen.generation_id)
    assert ref.row_count == 1 and ref.text_char_count == len(big)
    assert (await verify_lexical_generation(factory, ref.lexical_generation_id)).ok


async def test_lexical_unknown_source_generation_rejected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs = PublicationService(factory)
    with pytest.raises(UnknownGenerationError):
        await pubs.build_lexical("sha256:" + "0" * 64)


async def test_lexical_unsupported_tokenizer_and_config_rejected(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)

    with pytest.raises(KernelError, match="unsupported tokenizer"):
        await pubs.build_lexical(gen.generation_id, tokenizer="porter")
    with pytest.raises(KernelError, match="unsupported tokenizer config"):
        await pubs.build_lexical(
            gen.generation_id, tokenizer_config={"case_sensitive": 1}
        )


async def test_lexical_rebuild_after_tamper_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    ref = await pubs.build_lexical(gen.generation_id)

    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            f'UPDATE "{ref.fts_table}" SET text = ? WHERE rowid = 0',
            ("tampered",),
        )
        conn.commit()

    verification = await verify_lexical_generation(factory, ref.lexical_generation_id)
    assert not verification.ok
    assert any("text diverges" in problem for problem in verification.problems)

    # rebuild stays idempotent (immutable rows are trusted, mirroring
    # generations); deep verification remains the explicit tamper
    # detector, and published reads re-verify per-hit hashes
    idempotent = await pubs.build_lexical(gen.generation_id)
    assert idempotent.lexical_generation_id == ref.lexical_generation_id
    assert not (
        await verify_lexical_generation(factory, ref.lexical_generation_id)
    ).ok


async def test_lexical_staged_residue_is_resumable_and_invisible(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)

    from app.kernel.publications import PHASE_PUB_LEXICAL_STAGED

    with pytest.raises(Exception, match="injected fault"):
        await pubs.build_lexical(gen.generation_id, _inject_fault_at=PHASE_PUB_LEXICAL_STAGED)

    # durable staged residue exists, is identifiable, and is not validated
    staged = [
        ref
        for ref in await pubs.list_lexical_generations(workspace_id="ws-a")
        if ref.state == LEXICAL_STATE_STAGED
    ]
    assert len(staged) == 1

    # a fresh process resumes validation from durable state alone
    fresh_factory = _fresh_factory(_db_path(factory))
    fresh_pubs = PublicationService(fresh_factory)
    resumed = await fresh_pubs.validate_lexical(staged[0].lexical_generation_id)
    assert resumed.state == LEXICAL_STATE_VALIDATED
    assert (
        await verify_lexical_generation(fresh_factory, resumed.lexical_generation_id)
    ).ok


def _fresh_factory(db_path: Path) -> async_sessionmaker:
    """A brand-new engine over the same durable file (restart view)."""
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# publication set staging, validation, activation, resolution
# ---------------------------------------------------------------------------


async def _publish_first(
    factory: async_sessionmaker, service: KernelCommitService, workspace: str
):
    pubs = PublicationService(factory)
    await _commit_view(service, workspace, _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen = await _materialized(factory, workspace)
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


async def test_publish_first_set_resolves_published(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    assert p1.state == "published"
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id
    assert resolved.materialized_generation_id == gen.generation_id
    assert resolved.kernel_commit_id == gen.kernel_commit_id
    assert (await verify_publication_set(factory, p1.publication_set_id)).ok


async def test_staged_second_set_is_invisible(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")
    staged = await pubs.stage_publication_set(
        materialized_generation_id=gen2.generation_id
    )
    assert staged.state == "staged"

    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id


async def test_failed_validation_keeps_prior_set_published(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")
    staged = await pubs.stage_publication_set(
        materialized_generation_id=gen2.generation_id
    )
    lexical = await pubs.get_lexical_generation(staged.lexical_generation_id)

    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            f'UPDATE "{lexical.fts_table}" SET text = ? WHERE rowid = 0',
            ("corrupted",),
        )
        conn.commit()

    from app.kernel.errors import PublicationIntegrityError

    with pytest.raises(PublicationIntegrityError, match="lexical integrity"):
        await pubs.validate_publication_set(staged.publication_set_id)
    failed = await pubs.get_publication_set(staged.publication_set_id)
    assert failed.state == "failed"

    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id
    assert (await verify_publication_set(factory, p1.publication_set_id)).ok


async def test_activate_second_set_supersedes_first(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")
    p2 = await pubs.publish(materialized_generation_id=gen2.generation_id)

    assert p2.state == "published"
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p2.publication_set_id
    assert (
        await pubs.get_publication_set(p1.publication_set_id)
    ).state == "superseded"
    assert (await verify_publication_set(factory, p2.publication_set_id)).ok


async def test_publish_is_idempotent_for_live_set(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    again = await pubs.publish(materialized_generation_id=gen.generation_id)
    assert again.publication_set_id == p1.publication_set_id
    assert again.state == "published"
    sets = await pubs.list_publication_sets(workspace_id="ws-a")
    assert len(sets) == 1


async def test_activation_requires_validated_state(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")
    staged = await pubs.stage_publication_set(
        materialized_generation_id=gen2.generation_id
    )

    from app.kernel.errors import PublicationStateError

    with pytest.raises(PublicationStateError, match="cannot activate"):
        await pubs.activate_publication_set(staged.publication_set_id)
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id


async def test_mixed_cut_members_rejected_at_staging(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen1 = await _materialized(factory, "ws-a")
    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")

    pubs = PublicationService(factory)
    lexical1 = await pubs.build_lexical(gen1.generation_id)
    from app.kernel.errors import PublicationIntegrityError

    with pytest.raises(PublicationIntegrityError, match="incompatible"):
        await pubs.stage_publication_set(
            materialized_generation_id=gen2.generation_id,
            lexical_generation_id=lexical1.lexical_generation_id,
        )


async def test_missing_required_member_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    from app.kernel.errors import PublicationIntegrityError

    raw_set_id = "sha256:" + "0" * 64
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "INSERT INTO kernel_publication_sets "
            "(publication_set_id, workspace_id, profile, kernel_commit_id, "
            " snapshot_id, materialized_generation_id, lexical_generation_id, "
            " vector_generation_id, content_digest, state) "
            "VALUES (?, 'ws-a', 'default', 99, 'sha256:x', "
            " 'sha256:missing-gen', 'sha256:missing-lex', NULL, 'sha256:d', "
            " 'staged')",
            (raw_set_id,),
        )
        conn.commit()

    with pytest.raises(PublicationIntegrityError, match="missing"):
        await pubs.validate_publication_set(raw_set_id)
    failed = await pubs.get_publication_set(raw_set_id)
    assert failed.state == "failed"
    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p1.publication_set_id


async def test_orphan_lexical_locator_rejected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")
    lexical2 = await pubs.build_lexical(gen2.generation_id)

    async with factory() as session:
        session.add(
            KernelLexicalRow(
                lexical_generation_id=lexical2.lexical_generation_id,
                row_index=99,
                record_id="orphan-record",
                view_id="document",
                node_id="ghost",
                revision_ref="sha256:ghost",
                text_hash=payload_byte_hash(b"ghost"),
                text_chars=5,
            )
        )
        await session.commit()

    staged = await pubs.stage_publication_set(
        materialized_generation_id=gen2.generation_id,
        lexical_generation_id=lexical2.lexical_generation_id,
    )
    from app.kernel.errors import PublicationIntegrityError

    with pytest.raises(PublicationIntegrityError, match="outside the materialized"):
        await pubs.validate_publication_set(staged.publication_set_id)
    assert (
        await pubs.get_publication_set(staged.publication_set_id)
    ).state == "failed"


async def test_vector_absence_is_explicit_never_inherited(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit_view(service, "ws-a", _view("view-1", {"n1": "alpha"}, "rev-s1"))
    gen1 = await _materialized(factory, "ws-a")
    pubs = PublicationService(factory)
    p1 = await pubs.publish(
        materialized_generation_id=gen1.generation_id,
        vector_generation_id="vector-v1",
    )
    assert p1.vector_generation_id == "vector-v1"

    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
    )
    gen2 = await _materialized(factory, "ws-a")
    p2 = await pubs.publish(materialized_generation_id=gen2.generation_id)
    assert p2.vector_generation_id is None  # absent, not borrowed from P1

    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id == p2.publication_set_id
    assert resolved.vector_generation_id is None

    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_publication_sets SET vector_generation_id = "
            "'vector-v1' WHERE publication_set_id = ?",
            (p2.publication_set_id,),
        )
        conn.commit()
    assert not (
        await verify_publication_set(factory, p2.publication_set_id)
    ).ok


async def test_profiles_are_independent_scopes(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    aux = await pubs.publish(
        materialized_generation_id=gen.generation_id, profile="aux"
    )
    assert aux.publication_set_id != p1.publication_set_id

    default_resolved = await resolve_published_set(factory, "ws-a")
    aux_resolved = await resolve_published_set(factory, "ws-a", profile="aux")
    assert default_resolved is not None
    assert aux_resolved is not None
    assert default_resolved.publication_set_id == p1.publication_set_id
    assert aux_resolved.publication_set_id == aux.publication_set_id


async def test_full_rebuild_reproduces_set_identity(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    rebuilt = await pubs.publish(
        materialized_generation_id=gen.generation_id,
        vector_generation_id=p1.vector_generation_id,
    )
    assert rebuilt.publication_set_id == p1.publication_set_id
    assert rebuilt.content_digest == p1.content_digest


# ---------------------------------------------------------------------------
# pinned publication reader + lexical search
# ---------------------------------------------------------------------------


async def test_reader_search_resolves_hits_to_sources(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    reader = await open_published_reader(factory, "ws-a")
    assert reader is not None and reader.pinned
    try:
        hits = await reader.search("alpha")
        assert len(hits) == 1
        hit = hits[0]
        assert hit.publication_set_id == p1.publication_set_id
        assert hit.node_id == "n1"
        assert hit.text == "alpha"
        assert hit.revision_ref == _view(
            "view-1", {"n1": "alpha"}, "rev-s1"
        ).view_revision_id()
        assert hit.text_hash == payload_byte_hash(b"alpha")
        explain = reader.explain()
        assert explain["publication_set_id"] == p1.publication_set_id
        assert explain["lexical_generation_id"] == p1.lexical_generation_id
        assert explain["tokenizer"] == "unicode61"
    finally:
        await reader.close()
    assert not reader.pinned
    assert (await active_publication_pins(factory)) == ()


async def test_reader_pinned_across_publication_switch(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    reader = await open_published_reader(factory, "ws-a")
    assert reader is not None
    try:
        await _commit_view(
            service, "ws-a", _view("view-2", {"n1": "beta"}, "rev-s2"), advance=False
        )
        gen2 = await _materialized(factory, "ws-a")
        p2 = await pubs.publish(materialized_generation_id=gen2.generation_id)

        # the pinned reader keeps resolving P1/L1 only
        hits = await reader.search("alpha")
        assert [hit.text for hit in hits] == ["alpha"]
        assert hits[0].publication_set_id == p1.publication_set_id
        assert hits[0].lexical_generation_id == p1.lexical_generation_id
        assert await reader.search("beta") == ()

        # a fresh reader resolves P2/L2 only
        fresh = await open_published_reader(factory, "ws-a")
        assert fresh is not None
        try:
            assert fresh.publication_set_id == p2.publication_set_id
            assert await fresh.search("alpha") == ()
            beta_hits = await fresh.search("beta")
            assert [hit.text for hit in beta_hits] == ["beta"]
        finally:
            await fresh.close()
    finally:
        await reader.close()


async def test_reindex_never_blends_generations(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    await _commit_view(
        service,
        "ws-a",
        _view("view-2", {"n1": "alpha beta", "n2": "beta"}, "rev-s2"),
        advance=False,
    )
    gen2 = await _materialized(factory, "ws-a")
    p2 = await pubs.publish(materialized_generation_id=gen2.generation_id)

    pinned_p1 = await open_published_reader(factory, "ws-a")
    assert pinned_p1 is not None
    assert pinned_p1.publication_set_id == p2.publication_set_id  # new default
    await pinned_p1.close()

    # every query is attributable wholly to L1 or L2 — never a mixture
    p1_reader = PublicationReader(
        factory,
        await pubs.get_publication_set(p1.publication_set_id),
        await pubs.get_lexical_generation(p1.lexical_generation_id),
    )
    p1_alpha = await p1_reader.search("alpha")
    p1_beta = await p1_reader.search("beta")
    assert [hit.node_id for hit in p1_alpha] == ["n1"]
    assert p1_beta == ()
    assert all(
        hit.lexical_generation_id == p1.lexical_generation_id for hit in p1_alpha
    )

    p2_reader = PublicationReader(
        factory,
        await pubs.get_publication_set(p2.publication_set_id),
        await pubs.get_lexical_generation(p2.lexical_generation_id),
    )
    p2_alpha = await p2_reader.search("alpha")
    p2_beta = await p2_reader.search("beta")
    assert [hit.node_id for hit in sorted(p2_alpha, key=lambda h: h.row_index)] == ["n1"]
    assert [hit.node_id for hit in sorted(p2_beta, key=lambda h: h.row_index)] == [
        "n1",
        "n2",
    ]
    assert all(
        hit.lexical_generation_id == p2.lexical_generation_id for hit in p2_beta
    )


async def test_reader_search_rejects_malformed_query(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish_first(factory, service, "ws-a")

    reader = await open_published_reader(factory, "ws-a")
    assert reader is not None
    try:
        with pytest.raises(LexicalQueryError):
            await reader.search('"unbalanced OR AND (')
        with pytest.raises(LexicalQueryError):
            await reader.search("   ")
    finally:
        await reader.close()


async def test_reader_hit_tamper_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish_first(factory, service, "ws-a")

    lexical = await pubs.get_lexical_generation(p1.lexical_generation_id)
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            f'UPDATE "{lexical.fts_table}" SET text = ? WHERE rowid = 0',
            ("silently swapped",),
        )
        conn.commit()

    reader = await open_published_reader(factory, "ws-a")
    assert reader is not None
    try:
        from app.kernel.errors import PublicationIntegrityError

        # querying the tampered text surfaces the hash mismatch
        with pytest.raises(PublicationIntegrityError, match="tampered"):
            await reader.search("swapped")
    finally:
        await reader.close()


async def test_open_published_reader_without_publication(payload_env: tuple) -> None:
    factory, store, service = payload_env
    assert await open_published_reader(factory, "ws-never") is None


async def test_reader_pin_renew_extends_lease(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish_first(factory, service, "ws-a")

    reader = await open_published_reader(factory, "ws-a", pin_lease_seconds=60)
    assert reader is not None
    try:
        (pin,) = await active_publication_pins(factory)
        first_expiry = pin.expires_at
        await reader.renew(lease_seconds=120)
        (renewed,) = await active_publication_pins(factory)
        assert renewed.expires_at > first_expiry
    finally:
        await reader.close()
