"""Backend-portable lexical-index physical layer (PR83B2).

A publication's lexical generation is a logical, immutable artifact:
one deterministic row sequence per (source generation, tokenizer,
partition). Its *physical* embodiment depends on the database profile,
and every construct whose rendering or semantics depend on that choice
lives here — behind one seam, mirroring how :mod:`app.kernel.dialects`
owns transaction/insert portability.

Profiles
--------
**SQLite (local).** One FTS5 virtual table per generation
(``kernel_fts_<full-digest>``, ``unicode61`` tokenizer). Best matches
have the *lowest* ``bm25()`` values (FTS5 negates conventional BM25),
so canonical order is ``rank ASC, row_index ASC``.

**PostgreSQL (industrial).** One immutable plain table per generation
(``kernel_fts_<digest-40>``, under PostgreSQL's 63-byte identifier
limit) carrying a STORED ``tsvector`` column generated with the
explicit ``simple`` configuration plus a GIN index. ``simple``
lowercases and applies no stemming and no stop-word removal — the
closest native match to ``unicode61``'s deliberate minimalism. The
configuration is pinned in every ``to_tsvector``/``phraseto_tsquery``
call and recorded in generation identity, never inherited from a
server default. Best matches have the *highest* ``ts_rank`` values, so
canonical order is ``rank DESC, row_index ASC``.

Because the table is created, populated, and indexed inside the
generation's staging transaction (PostgreSQL DDL is transactional),
a failed or crashed build leaves no physical residue: there is no
non-transactional DDL window and no ``INVALID``-index state to
reconcile. A regular (non-``CONCURRENTLY``) index build is correct
here because the relation is brand new and not yet readable by any
publication reader.

Deliberate, documented cross-profile tokenization differences (see
the PR83B2 evidence bundle): PostgreSQL ``simple`` does not fold Latin
diacritics (``unicode61`` does), and the two engines' parsers split
punctuation/hyphen boundaries differently. Logical query semantics
(terms AND/OR, phrase adjacency) are preserved; raw scores are never
compared across profiles.

Query compilation
-----------------
Callers express lexical intent *logically* — ``(text, mode)`` with
``mode ∈ {all_terms, any_term, phrase}``. This module compiles that
intent per backend: an FTS5 MATCH string on SQLite, and on PostgreSQL
a ``tsquery`` expression built entirely from bound parameters via
``phraseto_tsquery`` — user text never passes through a query grammar,
so backend-syntax or SQL injection is structurally impossible. The
continuation ``query_hash`` is derived from the *logical* form, not
from any backend rendering, so paging and the reader can never
disagree about query identity.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from sqlalchemy import text

from app.kernel.dialects import POSTGRESQL, SQLITE
from app.kernel.errors import KernelError, LexicalQueryError
from app.utils.canonical import payload_byte_hash

__all__ = [
    "LEXICAL_QUERY_SCHEMA",
    "POSTGRES_LEXICAL_INDEX_ID",
    "POSTGRES_LEXICAL_INDEX_VERSION",
    "POSTGRES_LEXICAL_TOKENIZER",
    "POSTGRES_TEXT_SEARCH_CONFIG",
    "POSTGRES_TOKENIZER_CONFIG_KEY",
    "SUPPORTED_LEXICAL_MODES",
    "SQLITE_LEXICAL_INDEX_ID",
    "SQLITE_LEXICAL_INDEX_VERSION",
    "SQLITE_LEXICAL_TOKENIZER",
    "compile_sqlite_match",
    "default_tokenizer",
    "index_identity",
    "lexical_query_hash",
    "pg_index_name",
    "postgres_query_expression",
    "supported_tokenizer_config_keys",
    "supported_tokenizers",
    "validate_logical_query",
]


# -- identities -------------------------------------------------------------

#: SQLite profile: the PR76 FTS5 projection. Unchanged so existing
#: generation identities stay byte-identical.
SQLITE_LEXICAL_INDEX_ID = "marker.kernel.lexical.fts5.v1"
SQLITE_LEXICAL_INDEX_VERSION = "1.0.0"

#: PostgreSQL profile: a distinct, self-describing identity. A
#: PostgreSQL lexical generation is *not* an FTS5 generation and must
#: never be labeled as one (continuations and readers interpret rank
#: direction through the owning generation's manifest).
POSTGRES_LEXICAL_INDEX_ID = "marker.kernel.lexical.pg_tsvector.v1"
POSTGRES_LEXICAL_INDEX_VERSION = "1.0.0"

SQLITE_LEXICAL_TOKENIZER = "unicode61"
POSTGRES_LEXICAL_TOKENIZER = "pg_tsvector"

#: The one PostgreSQL text-search configuration the industrial profile
#: uses, for both indexing (generated column) and querying. Explicit in
#: every call — a server-varying ``default_text_search_config`` can
#: never change lexical semantics.
POSTGRES_TEXT_SEARCH_CONFIG = "simple"

#: The single tokenizer-config key the PostgreSQL profile records.
POSTGRES_TOKENIZER_CONFIG_KEY = "text_search_config"

#: Logical modes shared by the typed query contract and the reader.
SUPPORTED_LEXICAL_MODES = frozenset({"all_terms", "any_term", "phrase"})

#: Schema tag mixed into the logical query hash so the derivation is
#: self-describing; changing query-hash semantics requires a new tag.
LEXICAL_QUERY_SCHEMA = "marker.lexical.query.v1"


def index_identity(backend: str) -> tuple[str, str]:
    """``(index_id, version)`` a generation on ``backend`` must carry."""
    if backend == SQLITE:
        return SQLITE_LEXICAL_INDEX_ID, SQLITE_LEXICAL_INDEX_VERSION
    if backend == POSTGRESQL:
        return POSTGRES_LEXICAL_INDEX_ID, POSTGRES_LEXICAL_INDEX_VERSION
    raise KernelError(
        f"lexical index has no implementation for backend {backend!r}; "
        "supported backends: sqlite, postgresql"
    )


def default_tokenizer(backend: str) -> str:
    """The tokenizer a generation on ``backend`` is built with when the
    caller does not name one explicitly."""
    if backend == SQLITE:
        return SQLITE_LEXICAL_TOKENIZER
    if backend == POSTGRESQL:
        return POSTGRES_LEXICAL_TOKENIZER
    raise KernelError(
        f"lexical index has no implementation for backend {backend!r}; "
        "supported backends: sqlite, postgresql"
    )


def supported_tokenizers(backend: str) -> frozenset[str]:
    """Tokenizers a backend can honestly build. A tokenizer change is a
    new projection identity, never a silent reindex of an accepted one."""
    if backend == SQLITE:
        return frozenset({SQLITE_LEXICAL_TOKENIZER})
    if backend == POSTGRESQL:
        return frozenset({POSTGRES_LEXICAL_TOKENIZER})
    raise KernelError(
        f"lexical index has no implementation for backend {backend!r}; "
        "supported backends: sqlite, postgresql"
    )


def supported_tokenizer_config_keys(tokenizer: str) -> frozenset[str]:
    if tokenizer == SQLITE_LEXICAL_TOKENIZER:
        return frozenset()
    if tokenizer == POSTGRES_LEXICAL_TOKENIZER:
        return frozenset({POSTGRES_TOKENIZER_CONFIG_KEY})
    raise KernelError(
        f"unsupported tokenizer {tokenizer!r}; supported: "
        "unicode61 (sqlite), pg_tsvector (postgresql)"
    )


def pg_index_name(table: str) -> str:
    """Deterministic GIN index name for one PostgreSQL lexical table."""
    return f"{table}_tsv_ix"


# -- logical query compilation ---------------------------------------------


def validate_logical_query(text: str, mode: str) -> list[str]:
    """Validate one logical lexical query; return its tokens.

    ``text`` must already be contract-normalized upstream (NFC,
    whitespace-collapsed); the reader re-validates structurally so an
    unnormalized caller fails closed at the trust boundary.
    """
    if not isinstance(text, str) or not text.strip():
        raise LexicalQueryError("lexical query must be a non-empty string")
    if mode not in SUPPORTED_LEXICAL_MODES:
        raise LexicalQueryError(
            f"unsupported lexical mode {mode!r}; supported: "
            f"{sorted(SUPPORTED_LEXICAL_MODES)}"
        )
    tokens = text.split()
    if not tokens:
        raise LexicalQueryError("lexical query must contain non-whitespace tokens")
    return tokens


def lexical_query_hash(text: str, mode: str) -> str:
    """Hash of the *logical* query — backend-independent by design.

    Continuations bind to this value, so the same logical query hashes
    identically no matter which physical backend renders it, and any
    semantic change to text or mode invalidates old continuations. The
    hash input is the whitespace-canonical token form, matching the
    contract's normalization upstream.
    """
    tokens = validate_logical_query(text, mode)
    payload = "\x1f".join((LEXICAL_QUERY_SCHEMA, mode, " ".join(tokens)))
    return payload_byte_hash(payload.encode("utf-8"))


def _quote_fts5(term: str) -> str:
    """Quote one term as an FTS5 phrase so its bytes can never be
    parsed as MATCH grammar (inner quotes are doubled per FTS5)."""
    return '"' + term.replace('"', '""') + '"'


def compile_sqlite_match(text: str, mode: str) -> str:
    """Compile typed lexical intent into a bounded, safely quoted FTS5
    MATCH expression.

    Tokens are whitespace-separated; each becomes a quoted phrase, so
    ``OR``, ``NEAR(...)``, column filters, and bareword operators
    inside user text are always literal content.
    """
    tokens = validate_logical_query(text, mode)
    if mode == "phrase":
        return _quote_fts5(" ".join(tokens))
    joiner = " AND " if mode == "all_terms" else " OR "
    return joiner.join(_quote_fts5(token) for token in tokens)


def postgres_query_expression(text: str, mode: str) -> tuple[str, dict[str, Any]]:
    """Compile typed lexical intent into a parameterized PostgreSQL
    ``tsquery`` expression: ``(sql_expression, bound_params)``.

    Every user-controlled value flows through a bound parameter into
    ``phraseto_tsquery``, which parses *document text*, not query
    grammar — so operator injection is impossible. Each token becomes
    one phrase query (preserving within-token adjacency, mirroring the
    quoted-phrase semantics of the SQLite compiler); modes combine
    phrases with ``&&`` (all), ``||`` (any), or a single phrase over
    the whole text (phrase mode).
    """
    tokens = validate_logical_query(text, mode)
    params: dict[str, Any] = {"cfg": POSTGRES_TEXT_SEARCH_CONFIG}
    if mode == "phrase":
        return "phraseto_tsquery(:cfg, :qtext)", {**params, "qtext": text}
    parts = []
    for index, token in enumerate(tokens):
        name = f"t{index}"
        params[name] = token
        parts.append(f"phraseto_tsquery(:cfg, :{name})")
    joiner = " && " if mode == "all_terms" else " || "
    return joiner.join(parts), params


# -- physical staging -------------------------------------------------------


async def stage_physical(
    session: Any,
    *,
    backend: str,
    table: str,
    tokenizer: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Create and populate one generation's physical index artifact.

    Runs inside the caller's staging transaction on both backends.
    ``rows`` are mappings with ``row_index``/``record_id``/``view_id``/
    ``node_id``/``text``.
    """
    if backend == SQLITE:
        if tokenizer != SQLITE_LEXICAL_TOKENIZER:
            raise KernelError(
                f"the SQLite lexical profile requires tokenizer "
                f"{SQLITE_LEXICAL_TOKENIZER!r}, got {tokenizer!r}"
            )
        await session.execute(
            text(
                'CREATE VIRTUAL TABLE "{table}" USING fts5('
                "record_id UNINDEXED, view_id UNINDEXED, node_id UNINDEXED, "
                "text, tokenize='{tokenizer}')".format(table=table, tokenizer=tokenizer)
            )
        )
        insert_sql = (
            'INSERT INTO "{table}"(rowid, record_id, view_id, node_id, text) '
            "VALUES (:row_index, :record_id, :view_id, :node_id, :text)"
        ).format(table=table)
    elif backend == POSTGRESQL:
        if tokenizer != POSTGRES_LEXICAL_TOKENIZER:
            raise KernelError(
                f"the PostgreSQL lexical profile requires tokenizer "
                f"{POSTGRES_LEXICAL_TOKENIZER!r}, got {tokenizer!r}"
            )
        await session.execute(
            text(
                'CREATE TABLE "{table}" (\n'
                "    row_index BIGINT PRIMARY KEY,\n"
                "    record_id TEXT NOT NULL,\n"
                "    view_id TEXT NOT NULL,\n"
                "    node_id TEXT NOT NULL,\n"
                '    "text" TEXT NOT NULL,\n'
                "    tsv tsvector GENERATED ALWAYS AS "
                "(to_tsvector('{cfg}'::regconfig, \"text\")) STORED\n"
                ")".format(table=table, cfg=POSTGRES_TEXT_SEARCH_CONFIG)
            )
        )
        await session.execute(
            text(
                'CREATE INDEX "{ix}" ON "{table}" USING GIN (tsv)'.format(
                    ix=pg_index_name(table), table=table
                )
            )
        )
        insert_sql = (
            'INSERT INTO "{table}" (row_index, record_id, view_id, node_id, "text") '
            "VALUES (:row_index, :record_id, :view_id, :node_id, :text)"
        ).format(table=table)
    else:
        raise KernelError(
            f"lexical index has no implementation for backend {backend!r}; "
            "supported backends: sqlite, postgresql"
        )
    if rows:
        await session.execute(
            text(insert_sql),
            [dict(row) for row in rows],
        )


def drop_physical_sql(table: str) -> str:
    """Portable drop for one generation's physical artifact (the GIN
    index, if any, dies with its table)."""
    return f'DROP TABLE IF EXISTS "{table}"'


def read_back_sql(backend: str, table: str) -> str:
    """Canonical ordered read-back of one generation's physical rows."""
    if backend == SQLITE:
        return (
            "SELECT rowid AS row_index, record_id, view_id, node_id, text "
            f'FROM "{table}" ORDER BY row_index'
        )
    if backend == POSTGRESQL:
        return (
            "SELECT row_index, record_id, view_id, node_id, text "
            f'FROM "{table}" ORDER BY row_index'
        )
    raise KernelError(
        f"lexical index has no implementation for backend {backend!r}; "
        "supported backends: sqlite, postgresql"
    )


async def physical_integrity_problems(session: Any, backend: str, table: str) -> list[str]:
    """Backend-native structural integrity checks for one artifact.

    SQLite: FTS5's own ``integrity-check`` command (verifies the index
    against its stored content). PostgreSQL: the GIN index must exist
    and be valid, and every stored ``tsvector`` must equal the
    regeneration of its row's text under the pinned configuration.
    """
    if backend == SQLITE:
        try:
            await session.execute(
                text(f'INSERT INTO "{table}"("{table}") VALUES(\'integrity-check\')')
            )
        except Exception as exc:  # noqa: BLE001 - reported as a problem
            return [f"FTS integrity-check rejected: {exc}"]
        return []
    if backend == POSTGRESQL:
        problems: list[str] = []
        valid = await session.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :ixname "
                "AND i.indrelid = CAST(:relname AS regclass) "
                "LIMIT 1"
            ),
            {"ixname": pg_index_name(table), "relname": table},
        )
        if valid is not True:
            problems.append(
                f"GIN index {pg_index_name(table)!r} on {table!r} is missing "
                "or invalid"
            )
        diverged = await session.scalar(
            text(
                f'SELECT count(*) FROM "{table}" '
                "WHERE tsv IS DISTINCT FROM "
                "to_tsvector('{cfg}'::regconfig, \"text\")".format(
                    cfg=POSTGRES_TEXT_SEARCH_CONFIG
                )
            )
        )
        if (diverged or 0) > 0:
            problems.append(
                f"{diverged} row(s) diverge from tsvector regeneration under "
                f"config {POSTGRES_TEXT_SEARCH_CONFIG!r}"
            )
        return problems
    raise KernelError(
        f"lexical index has no implementation for backend {backend!r}; "
        "supported backends: sqlite, postgresql"
    )


# -- reader query rendering --------------------------------------------------


def _sqlite_page_query(table: str, query_param: str, keyset: bool) -> str:
    where = f'WHERE "{table}" MATCH :{query_param}'
    if keyset:
        where += (
            f' AND (bm25("{table}") > :after_rank OR '
            f'(bm25("{table}") = :after_rank AND rowid > :after_row_index))'
        )
    return (
        f'SELECT rowid AS row_index, record_id, view_id, node_id, text, '
        f'bm25("{table}") AS rank_value FROM "{table}" {where} '
        "ORDER BY rank_value ASC, row_index ASC LIMIT :limit"
    )


def _postgres_page_query(table: str, q: str, keyset: bool) -> str:
    # The tsquery expression is parenthesized everywhere: ``@@`` and the
    # tsquery operators share precedence and associate left, so an
    # unparenthesized ``tsv @@ a && b`` would parse as ``(tsv @@ a) && b``.
    where = f"WHERE tsv @@ ({q})"
    if keyset:
        where += (
            f" AND (ts_rank(tsv, ({q})) < :after_rank OR "
            f"(ts_rank(tsv, ({q})) = :after_rank AND row_index > :after_row_index))"
        )
    return (
        f'SELECT row_index, record_id, view_id, node_id, "text", '
        f"ts_rank(tsv, ({q})) AS rank_value FROM \"{table}\" {where} "
        "ORDER BY rank_value DESC, row_index ASC LIMIT :limit"
    )


def page_query(
    backend: str,
    table: str,
    *,
    text: str,
    mode: str,
    limit: int,
    offset: int = 0,
    after_rank: float | None = None,
    after_row_index: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """One ranked page (optionally keyset-resumed) over the artifact.

    ``after_rank``/``after_row_index`` together switch the statement to
    the keyset form. Rank direction is backend-owned — ascending for
    FTS5's negated BM25, descending for PostgreSQL ``ts_rank`` — and
    the keyset predicate mirrors it, so callers never encode direction.
    """
    keyset = after_rank is not None and after_row_index is not None
    if backend == SQLITE:
        sql = _sqlite_page_query(table, "query", keyset)
        params: dict[str, Any] = {
            "query": compile_sqlite_match(text, mode),
            "limit": limit,
        }
    elif backend == POSTGRESQL:
        q, params = postgres_query_expression(text, mode)
        sql = _postgres_page_query(table, q, keyset)
        params["limit"] = limit
    else:
        raise KernelError(
            f"lexical index has no implementation for backend {backend!r}; "
            "supported backends: sqlite, postgresql"
        )
    if offset:
        sql = sql.replace("LIMIT :limit", "LIMIT :limit OFFSET :offset")
        params["offset"] = offset
    if keyset:
        params["after_rank"] = float(after_rank)  # type: ignore[arg-type]
        params["after_row_index"] = int(after_row_index)  # type: ignore[arg-type]
    return sql, params


def anchor_query(
    backend: str,
    table: str,
    *,
    text: str,
    mode: str,
    after_row_index: int,
) -> tuple[str, dict[str, Any]]:
    """Fetch the continuation anchor row (keyset resume validity check).

    The anchor must still match the query; its rank is compared with
    the continuation's stored rank by the reader.
    """
    if backend == SQLITE:
        sql = (
            f'SELECT rowid AS row_index, record_id, view_id, node_id, text, '
            f'bm25("{table}") AS rank_value FROM "{table}" '
            f'WHERE "{table}" MATCH :query AND rowid = :after_row_index LIMIT 1'
        )
        params: dict[str, Any] = {"query": compile_sqlite_match(text, mode)}
    elif backend == POSTGRESQL:
        q, params = postgres_query_expression(text, mode)
        sql = (
            f'SELECT row_index, record_id, view_id, node_id, "text", '
            f"ts_rank(tsv, ({q})) AS rank_value FROM \"{table}\" "
            f"WHERE row_index = :after_row_index AND tsv @@ ({q}) LIMIT 1"
        )
    else:
        raise KernelError(
            f"lexical index has no implementation for backend {backend!r}; "
            "supported backends: sqlite, postgresql"
        )
    params["after_row_index"] = int(after_row_index)
    return sql, params
