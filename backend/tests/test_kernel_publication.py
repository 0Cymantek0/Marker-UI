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
    UnknownGenerationError,
)
from app.kernel.generations import GenerationService
from app.kernel.models import KernelLexicalRow
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord
from app.kernel.publications import (
    LEXICAL_STATE_STAGED,
    LEXICAL_STATE_VALIDATED,
    LEXICAL_TOKENIZER,
    PublicationService,
    extract_lexical_corpus,
    fts_table_name,
    verify_lexical_generation,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
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


def test_corpus_extracts_latest_revision_per_view_only() -> None:
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


def test_corpus_groups_by_view_and_orders_deterministically() -> None:
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


def test_corpus_rejects_non_mapping_texts() -> None:
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
