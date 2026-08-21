"""Unit tests for the backend-portable lexical physical layer (PR83B2).

Pure-function coverage: identities, tokenizer acceptance, logical query
validation/hashing, per-backend query compilation, and physical-SQL
rendering. Database behavior (staging, integrity, serving) is proven by
the publication conformance suites on real SQLite and PostgreSQL.
"""

import pytest

from app.kernel.dialects import POSTGRESQL, SQLITE
from app.kernel.errors import KernelError, LexicalQueryError
from app.kernel.lexical import (
    LEXICAL_QUERY_SCHEMA,
    POSTGRES_LEXICAL_INDEX_ID,
    POSTGRES_LEXICAL_TOKENIZER,
    POSTGRES_TEXT_SEARCH_CONFIG,
    SQLITE_LEXICAL_INDEX_ID,
    SQLITE_LEXICAL_TOKENIZER,
    anchor_query,
    compile_sqlite_match,
    default_tokenizer,
    index_identity,
    lexical_query_hash,
    page_query,
    pg_index_name,
    postgres_query_expression,
    supported_tokenizer_config_keys,
    supported_tokenizers,
    validate_logical_query,
)
from app.utils.canonical import payload_byte_hash


# -- identities and tokenizer acceptance ------------------------------------


def test_index_identity_per_backend() -> None:
    assert index_identity(SQLITE) == (SQLITE_LEXICAL_INDEX_ID, "1.0.0")
    assert index_identity(POSTGRESQL) == (POSTGRES_LEXICAL_INDEX_ID, "1.0.0")
    # The identities must be distinct: a PostgreSQL generation is not an
    # FTS5 generation and must never be labeled as one.
    assert SQLITE_LEXICAL_INDEX_ID != POSTGRES_LEXICAL_INDEX_ID


def test_index_identity_fails_closed_on_unknown_backend() -> None:
    with pytest.raises(KernelError, match="no implementation for backend 'mysql'"):
        index_identity("mysql")


def test_tokenizer_acceptance_is_backend_scoped() -> None:
    assert supported_tokenizers(SQLITE) == frozenset({SQLITE_LEXICAL_TOKENIZER})
    assert supported_tokenizers(POSTGRESQL) == frozenset({POSTGRES_LEXICAL_TOKENIZER})
    assert default_tokenizer(SQLITE) == SQLITE_LEXICAL_TOKENIZER
    assert default_tokenizer(POSTGRESQL) == POSTGRES_LEXICAL_TOKENIZER
    assert supported_tokenizer_config_keys(SQLITE_LEXICAL_TOKENIZER) == frozenset()
    assert supported_tokenizer_config_keys(POSTGRES_LEXICAL_TOKENIZER) == frozenset(
        {"text_search_config"}
    )
    with pytest.raises(KernelError):
        supported_tokenizers("mysql")


# -- logical query validation and hashing ------------------------------------


@pytest.mark.parametrize(
    "text,mode",
    [
        ("", "all_terms"),
        ("   ", "any_term"),
        (42, "all_terms"),  # type: ignore[arg-type]
        ("alpha", "bogus_mode"),
        ("alpha", ""),
    ],
)
def test_validate_logical_query_rejects_malformed_input(text, mode) -> None:
    with pytest.raises(LexicalQueryError):
        validate_logical_query(text, mode)


def test_validate_logical_query_returns_tokens() -> None:
    assert validate_logical_query("alpha beta", "all_terms") == ["alpha", "beta"]


def test_query_hash_binds_logical_form_not_backend_rendering() -> None:
    # Same logical query hashes identically regardless of backend; the
    # hash input is the schema tag, mode, and canonical text.
    expected = payload_byte_hash(
        "\x1f".join((LEXICAL_QUERY_SCHEMA, "all_terms", "alpha beta")).encode("utf-8")
    )
    assert lexical_query_hash("alpha beta", "all_terms") == expected


def test_query_hash_separates_mode_and_text() -> None:
    base = lexical_query_hash("alpha beta", "all_terms")
    assert lexical_query_hash("alpha beta", "any_term") != base
    assert lexical_query_hash("alpha beta", "phrase") != base
    assert lexical_query_hash("beta alpha", "all_terms") != base
    # Semantically identical normalized input (collapsed whitespace) is
    # stable: callers upstream already collapse, and the hash never
    # depends on incidental spacing.
    assert lexical_query_hash("alpha  beta", "all_terms") == base
    with pytest.raises(LexicalQueryError):
        lexical_query_hash("  ", "all_terms")


# -- SQLite MATCH compilation (moved from the FTS5-only compiler) ------------


def test_compile_sqlite_match_modes() -> None:
    assert compile_sqlite_match("alpha beta", "all_terms") == '"alpha" AND "beta"'
    assert compile_sqlite_match("alpha beta", "any_term") == '"alpha" OR "beta"'
    assert compile_sqlite_match("alpha beta", "phrase") == '"alpha beta"'


def test_compile_sqlite_match_neutralizes_match_grammar() -> None:
    adversarial = 'NEAR(a b) OR "c" AND column: '
    compiled = compile_sqlite_match(adversarial, "all_terms")
    assert compiled == (
        '"NEAR(a" AND "b)" AND "OR" AND """c""" AND "AND" AND "column:"'
    )


def test_compile_sqlite_match_rejects_empty() -> None:
    with pytest.raises(LexicalQueryError):
        compile_sqlite_match("   ", "all_terms")


# -- PostgreSQL expression compilation ----------------------------------------


def test_postgres_expression_all_terms() -> None:
    sql, params = postgres_query_expression("alpha beta", "all_terms")
    assert sql == (
        "phraseto_tsquery(:cfg, :t0) && phraseto_tsquery(:cfg, :t1)"
    )
    assert params == {"cfg": POSTGRES_TEXT_SEARCH_CONFIG, "t0": "alpha", "t1": "beta"}


def test_postgres_expression_any_term() -> None:
    sql, params = postgres_query_expression("alpha beta", "any_term")
    assert sql == (
        "phraseto_tsquery(:cfg, :t0) || phraseto_tsquery(:cfg, :t1)"
    )
    assert params == {"cfg": "simple", "t0": "alpha", "t1": "beta"}


def test_postgres_expression_phrase_is_single_bound_parameter() -> None:
    sql, params = postgres_query_expression("alpha beta", "phrase")
    assert sql == "phraseto_tsquery(:cfg, :qtext)"
    assert params == {"cfg": "simple", "qtext": "alpha beta"}


def test_postgres_expression_carries_no_user_text_in_sql() -> None:
    # Operator-bearing user text must appear only in bound parameters;
    # the SQL expression itself is a fixed template over :t0..:tN params.
    text = "a & b | !c"
    tokens = text.split()
    for mode in ("all_terms", "any_term"):
        sql, params = postgres_query_expression(text, mode)
        calls = [f"phraseto_tsquery(:cfg, :t{i})" for i in range(len(tokens))]
        joiner = " && " if mode == "all_terms" else " || "
        assert sql == joiner.join(calls)
        assert [params[f"t{i}"] for i in range(len(tokens))] == tokens
        assert params["cfg"] == POSTGRES_TEXT_SEARCH_CONFIG


def test_postgres_expression_rejects_malformed_input() -> None:
    with pytest.raises(LexicalQueryError):
        postgres_query_expression("   ", "all_terms")
    with pytest.raises(LexicalQueryError):
        postgres_query_expression("alpha", "bogus")


# -- physical SQL rendering -----------------------------------------------------


def test_page_query_sqlite_preserves_fts5_semantics() -> None:
    sql, params = page_query(
        SQLITE, "kernel_fts_abc", text="alpha beta", mode="all_terms", limit=5
    )
    assert 'FROM "kernel_fts_abc"' in sql
    assert 'MATCH :query' in sql
    assert "bm25" in sql
    assert "ORDER BY rank_value ASC, row_index ASC LIMIT :limit" in sql
    assert params == {"query": '"alpha" AND "beta"', "limit": 5}


def test_page_query_sqlite_keyset_predicate_resumes_forward() -> None:
    sql, params = page_query(
        SQLITE,
        "kernel_fts_abc",
        text="alpha",
        mode="all_terms",
        limit=5,
        after_rank=-1.25,
        after_row_index=3,
    )
    # FTS5 negates BM25: better matches are numerically smaller, so the
    # keyset resumes strictly *after* via (rank >, rowid >).
    assert "(bm25(\"kernel_fts_abc\") > :after_rank" in sql
    assert "rowid > :after_row_index" in sql
    assert params["after_rank"] == -1.25
    assert params["after_row_index"] == 3


def test_page_query_postgres_rank_direction_is_descending() -> None:
    sql, params = page_query(
        POSTGRESQL, "kernel_fts_abc", text="alpha", mode="all_terms", limit=5
    )
    assert "ts_rank(tsv, (phraseto_tsquery(:cfg, :t0)))" in sql
    assert "ORDER BY rank_value DESC, row_index ASC LIMIT :limit" in sql
    assert 'tsv @@ (phraseto_tsquery(:cfg, :t0))' in sql
    assert params["t0"] == "alpha"


def test_page_query_postgres_keyset_mirrors_descending_direction() -> None:
    sql, params = page_query(
        POSTGRESQL,
        "kernel_fts_abc",
        text="alpha",
        mode="all_terms",
        limit=5,
        after_rank=0.75,
        after_row_index=9,
    )
    # ts_rank: better matches are numerically larger; the keyset resumes
    # via (rank <, row_index >). Direction must NOT be inherited from FTS5.
    assert "ts_rank(tsv, (phraseto_tsquery(:cfg, :t0))) < :after_rank" in sql
    assert "row_index > :after_row_index" in sql
    assert params["after_rank"] == 0.75


def test_page_query_offset_only_without_keyset() -> None:
    sql, params = page_query(
        SQLITE, "kernel_fts_abc", text="alpha", mode="all_terms", limit=2, offset=4
    )
    assert "OFFSET :offset" in sql
    assert params["offset"] == 4
    assert "after_rank" not in params


def test_page_query_fails_closed_on_unknown_backend() -> None:
    with pytest.raises(KernelError, match="no implementation for backend 'mysql'"):
        page_query("mysql", "t", text="a", mode="all_terms", limit=1)


def test_anchor_query_both_backends() -> None:
    sql, params = anchor_query(
        SQLITE, "kernel_fts_abc", text="alpha", mode="all_terms", after_row_index=7
    )
    assert "rowid = :after_row_index" in sql
    assert 'MATCH :query' in sql
    assert params == {"query": '"alpha"', "after_row_index": 7}

    sql, params = anchor_query(
        POSTGRESQL, "kernel_fts_abc", text="alpha", mode="all_terms", after_row_index=7
    )
    assert "row_index = :after_row_index" in sql
    assert "tsv @@ (phraseto_tsquery(:cfg, :t0))" in sql
    assert "ts_rank(tsv, (" in sql
    assert params["after_row_index"] == 7


def test_pg_index_name_within_identifier_limit() -> None:
    # PostgreSQL caps identifiers at 63 bytes; the longest table name is
    # kernel_fts_ + 40 hex chars, and the index suffix must still fit.
    table = "kernel_fts_" + "a" * 40
    assert len(pg_index_name(table)) <= 63
