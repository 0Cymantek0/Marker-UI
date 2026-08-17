"""Atomic publication sets and immutable lexical generations (PR76).

A publication set is the one object that means "these exact queryable
products belong together and are the currently accepted published
state". It names immutable member generations — the required
materialized kernel generation and lexical (FTS5) index generation,
plus an optional vector slot whose ``None`` is an explicit *absent*,
never a fallback to an older publication — and it moves to published
through exactly one transactional pointer switch on
``kernel_publication_heads``.

Like materialized generations, everything here is **rebuildable
derived serving state over kernel truth, never a second truth
authority**: ``kernel_commit_id`` ordering remains the only document
truth, and every row can be discarded and rebuilt from its pinned cut.

Lifecycle (each step durable and atomic):

1. **build lexical → staged.** One transaction reads the pinned source
   materialized generation's ``view_document`` records, extracts the
   deterministic lexical corpus (latest view revision per view at the
   cut, one row per content node), creates the generation-scoped FTS5
   virtual table, inserts its rows, and writes the manifest row in
   state ``staged``. A crash inside rolls back; after it, at most an
   identifiable ``staged``/``failed`` residue remains — never visible
   to readers, because only a *published set* names queryable state.
2. **validate lexical → validated.** The content digest is recomputed
   from the stored rows *and* the FTS table's own read-back (text
   hashes re-derived from indexed text, row counts cross-checked, FTS
   ``integrity-check`` executed). Any divergence marks the generation
   ``failed`` and raises.
3. **stage set → staged; validate set → validated.** The set manifest
   names its members; validation enforces the compatibility key:
   workspace, kernel cut, snapshot, source-generation lineage, member
   presence and state, lexical integrity, locator membership, and
   digest agreement.
4. **activate set.** One transaction supersedes the previous published
   set, conditionally flips ``kernel_publication_heads``, and marks the
   set ``published``. That database commit is the linearization point:
   readers observe the old complete published set or the new complete
   one, never a mix.

Readers pin the published set once per request
(:func:`open_published_reader`); a durable publication pin protects
the set and every member from collection until released or lapsed.
Physical retirement stays with :mod:`app.kernel.gc` (PR65B contract).

FTS5 storage mode: one *self-contained* (non-external-content) FTS5
virtual table per lexical generation, named ``kernel_fts_<hex>`` and
created at build time — not by Alembic — so index bytes live and die
with their immutable generation. SQLite's own documentation warns that
external-content FTS consistency is the application's responsibility;
per-generation self-contained tables make that relationship provable
by construction instead (the index is never shared across generations
and never mutated after staging). The migration contract comparison
excludes the ``kernel_fts_`` prefix symmetrically (see
:mod:`app.db_migration`).

Vector layers are intentionally absent in this slice: the slot exists
so PR81 can name one, and absence is recorded explicitly.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    InjectedFaultError,
    InvalidPublicationProfileError,
    KernelError,
    LexicalIntegrityError,
    LexicalStateError,
    UnknownGenerationError,
    UnknownLexicalGenerationError,
)
from app.kernel.models import (
    KernelGeneration,
    KernelGenerationHead,
    KernelGenerationRecord,
    KernelLexicalGeneration,
    KernelLexicalRow,
)
from app.utils.canonical import (
    CanonicalValueError,
    canonical_json_bytes,
    canonical_json_str,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "DEFAULT_PUBLICATION_PROFILE",
    "FTS_TABLE_PREFIX",
    "LEXICAL_INDEX_ID",
    "LEXICAL_INDEX_VERSION",
    "LEXICAL_SCHEMA_VERSION",
    "LEXICAL_STATE_FAILED",
    "LEXICAL_STATE_STAGED",
    "LEXICAL_STATE_VALIDATED",
    "LEXICAL_TOKENIZER",
    "LexicalGenerationRef",
    "LexicalRowRef",
    "LexicalVerification",
    "PHASE_PUB_LEXICAL_BEGIN",
    "PHASE_PUB_LEXICAL_ROWS_MATERIALIZED",
    "PHASE_PUB_LEXICAL_SOURCE_READ",
    "PHASE_PUB_LEXICAL_STAGED",
    "PHASE_PUB_LEXICAL_VALIDATE_BEGIN",
    "PHASE_PUB_LEXICAL_VALIDATED",
    "PUBLICATION_FAULT_PHASES",
    "PUBLICATION_STATE_FAILED",
    "PUBLICATION_STATE_PUBLISHED",
    "PUBLICATION_STATE_STAGED",
    "PUBLICATION_STATE_SUPERSEDED",
    "PUBLICATION_STATE_VALIDATED",
    "PUBLICATION_SET_RECORD_TYPE",
    "PublicationService",
    "compute_lexical_identity",
    "compute_publication_set_identity",
    "default_publication_service",
    "extract_lexical_corpus",
    "fts_table_name",
    "validate_publication_profile",
    "verify_lexical_generation",
]

#: Framing domains separating PR76 identities from other kernel hashes.
LEXICAL_RECORD_TYPE = "marker.kernel.lexical_generation.v1"
LEXICAL_ID_SCHEMA_VERSION = "1.0.0"
PUBLICATION_SET_RECORD_TYPE = "marker.kernel.publication_set.v1"
PUBLICATION_SET_ID_SCHEMA_VERSION = "1.0.0"

#: Identity of the PR76 lexical index projection.
LEXICAL_INDEX_ID = "marker.kernel.lexical.fts5.v1"
LEXICAL_INDEX_VERSION = "1.0.0"
LEXICAL_SCHEMA_VERSION = "1.0.0"

#: The one tokenizer v1 supports. A generation's identity carries the
#: tokenizer and config, so a future tokenizer change can only ever
#: produce a new generation — never a rewrite of an accepted one.
LEXICAL_TOKENIZER = "unicode61"
_SUPPORTED_TOKENIZERS = frozenset({LEXICAL_TOKENIZER})
_SUPPORTED_TOKENIZER_CONFIG_KEYS = frozenset()

#: FTS5 virtual tables created by this module share this prefix; the
#: migration contract comparison excludes it symmetrically.
FTS_TABLE_PREFIX = "kernel_fts_"

#: Lexical generation lifecycle (a lexical generation has no "active"
#: state of its own: it is queryable only through a published set).
LEXICAL_STATE_STAGED = "staged"
LEXICAL_STATE_VALIDATED = "validated"
LEXICAL_STATE_FAILED = "failed"

#: Publication set lifecycle.
PUBLICATION_STATE_STAGED = "staged"
PUBLICATION_STATE_VALIDATED = "validated"
PUBLICATION_STATE_PUBLISHED = "published"
PUBLICATION_STATE_SUPERSEDED = "superseded"
PUBLICATION_STATE_FAILED = "failed"

#: The default publication scope; v1 services pin this profile.
DEFAULT_PUBLICATION_PROFILE = "default"

PUBLICATION_PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
MAX_RETRY_DELAY = 0.5

_BUSY_MARKERS = ("database is locked", "database table is locked", "database is busy")

# Deterministic fault-injection phases (test-only parameters).
PHASE_PUB_LEXICAL_BEGIN = "pub-lexical-begin"
PHASE_PUB_LEXICAL_SOURCE_READ = "pub-lexical-source-read"
PHASE_PUB_LEXICAL_ROWS_MATERIALIZED = "pub-lexical-rows-materialized"
PHASE_PUB_LEXICAL_STAGED = "pub-lexical-staged"
PHASE_PUB_LEXICAL_VALIDATE_BEGIN = "pub-lexical-validate-begin"
PHASE_PUB_LEXICAL_VALIDATED = "pub-lexical-validated"
PHASE_PUB_SET_STAGED = "pub-set-staged"
PHASE_PUB_VALIDATE_BEGIN = "pub-validate-begin"
PHASE_PUB_VALIDATED = "pub-validated"
PHASE_PUB_PRE_ACTIVATE = "pub-pre-activate"
PHASE_PUB_POST_ACTIVATE = "pub-post-activate"

PUBLICATION_FAULT_PHASES = frozenset(
    {
        PHASE_PUB_LEXICAL_BEGIN,
        PHASE_PUB_LEXICAL_SOURCE_READ,
        PHASE_PUB_LEXICAL_ROWS_MATERIALIZED,
        PHASE_PUB_LEXICAL_STAGED,
        PHASE_PUB_LEXICAL_VALIDATE_BEGIN,
        PHASE_PUB_LEXICAL_VALIDATED,
        PHASE_PUB_SET_STAGED,
        PHASE_PUB_VALIDATE_BEGIN,
        PHASE_PUB_VALIDATED,
        PHASE_PUB_PRE_ACTIVATE,
        PHASE_PUB_POST_ACTIVATE,
    }
)

_LEXICAL_BUILD_PHASES = frozenset(
    {
        PHASE_PUB_LEXICAL_BEGIN,
        PHASE_PUB_LEXICAL_SOURCE_READ,
        PHASE_PUB_LEXICAL_ROWS_MATERIALIZED,
        PHASE_PUB_LEXICAL_STAGED,
        PHASE_PUB_LEXICAL_VALIDATE_BEGIN,
    }
)


class _ConcurrentPointerMove(Exception):
    """Internal retry signal: the published-set pointer moved."""


def _is_busy(exc: OperationalError) -> bool:
    lowered = str(exc).lower()
    return any(marker in lowered for marker in _BUSY_MARKERS)


def _retry_delay(base: float, attempt: int) -> float:
    return min(base * (2**attempt), MAX_RETRY_DELAY)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_publication_profile(profile: str) -> str:
    if not isinstance(profile, str) or not PUBLICATION_PROFILE_PATTERN.match(profile):
        raise InvalidPublicationProfileError(
            f"invalid publication profile {profile!r}: must match "
            f"{PUBLICATION_PROFILE_PATTERN.pattern}"
        )
    return profile


# ---------------------------------------------------------------------------
# Deterministic identities
# ---------------------------------------------------------------------------


def compute_lexical_identity(
    *,
    workspace_id: str,
    kernel_commit_id: int,
    snapshot_id: str,
    source_generation_id: str,
    tokenizer: str = LEXICAL_TOKENIZER,
    tokenizer_config_json: str = "{}",
) -> str:
    """Deterministic lexical generation identity over declared inputs."""
    return record_identity_hash(
        record_type=LEXICAL_RECORD_TYPE,
        schema_version=LEXICAL_ID_SCHEMA_VERSION,
        payload={
            "workspace_id": workspace_id,
            "kernel_commit_id": kernel_commit_id,
            "snapshot_id": snapshot_id,
            "source_generation_id": source_generation_id,
            "index": {"id": LEXICAL_INDEX_ID, "version": LEXICAL_INDEX_VERSION},
            "tokenizer": tokenizer,
            "tokenizer_config": json.loads(tokenizer_config_json),
        },
    )


def compute_publication_set_identity(
    *,
    workspace_id: str,
    profile: str,
    kernel_commit_id: int,
    snapshot_id: str,
    materialized_generation_id: str,
    lexical_generation_id: str,
    vector_generation_id: str | None = None,
) -> str:
    """Deterministic publication set identity over its declared members.

    The identity covers exactly the compatibility dimensions a set must
    agree on: scope (workspace, profile), the pinned kernel cut/snapshot,
    and every member generation id — including the optional vector slot,
    so a set that omits the vector layer can never collide with (or
    borrow from) one that carries it.
    """
    return record_identity_hash(
        record_type=PUBLICATION_SET_RECORD_TYPE,
        schema_version=PUBLICATION_SET_ID_SCHEMA_VERSION,
        payload={
            "workspace_id": workspace_id,
            "profile": profile,
            "kernel_commit_id": kernel_commit_id,
            "snapshot_id": snapshot_id,
            "materialized_generation_id": materialized_generation_id,
            "lexical_generation_id": lexical_generation_id,
            "vector_generation_id": vector_generation_id,
        },
    )


def fts_table_name(lexical_generation_id: str) -> str:
    """Deterministic FTS5 virtual table name for one lexical generation."""
    digest = lexical_generation_id.removeprefix("sha256:")
    return f"{FTS_TABLE_PREFIX}{digest}"


# ---------------------------------------------------------------------------
# Corpus extraction (pure, deterministic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LexicalSourceRow:
    """One searchable text unit resolved from a materialized generation."""

    record_id: str
    view_id: str
    node_id: str
    revision_ref: str
    text: str


@dataclass(frozen=True)
class _CorpusRecord:
    """One view_document materialized record bound to its commit."""

    record_id: str
    kernel_commit_id: int
    identity_hash: str
    payload: dict[str, Any]


def extract_lexical_corpus(records: Sequence[_CorpusRecord]) -> list[LexicalSourceRow]:
    """Deterministically derive the lexical corpus from view documents.

    Selection rule, derived from generation content alone (never from
    live view heads): per view, the ``view_document`` revision with the
    highest ``(kernel_commit_id, identity_hash)`` at the cut wins —
    superseded revisions committed earlier are not indexed. One row is
    emitted per content node, ordered by view id then node id.
    """
    latest: dict[str, _CorpusRecord] = {}
    for record in records:
        view_id = record.payload.get("view_id") or "document"
        current = latest.get(view_id)
        if current is None or (record.kernel_commit_id, record.identity_hash) > (
            current.kernel_commit_id,
            current.identity_hash,
        ):
            latest[view_id] = record
    rows: list[LexicalSourceRow] = []
    for view_id in sorted(latest):
        record = latest[view_id]
        texts = record.payload.get("texts")
        if not isinstance(texts, Mapping):
            raise LexicalIntegrityError(
                f"view document {record.record_id!r}: texts is not a mapping"
            )
        for node_id in sorted(texts):
            node_text = texts[node_id]
            if not isinstance(node_text, str):
                raise LexicalIntegrityError(
                    f"view document {record.record_id!r}: text of {node_id!r} "
                    "is not str"
                )
            rows.append(
                LexicalSourceRow(
                    record_id=record.record_id,
                    view_id=view_id,
                    node_id=node_id,
                    revision_ref=record.identity_hash,
                    text=node_text,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LexicalGenerationRef:
    """Manifest-level view of one lexical generation."""

    lexical_generation_id: str
    workspace_id: str
    source_generation_id: str
    kernel_commit_id: int
    snapshot_id: str
    tokenizer: str
    tokenizer_config: dict
    schema_version: str
    fts_table: str
    row_count: int
    text_char_count: int
    content_digest: str
    state: str
    created_at: str | None
    validated_at: str | None


@dataclass(frozen=True)
class LexicalRowRef:
    """One source-resolvable lexical row locator."""

    row_index: int
    record_id: str
    view_id: str
    node_id: str
    revision_ref: str
    text_hash: str
    text_chars: int


@dataclass(frozen=True)
class LexicalVerification:
    """Deep verification result for one lexical generation."""

    lexical_generation_id: str
    ok: bool
    problems: tuple[str, ...]
    checked_rows: int


def _lexical_ref(row: KernelLexicalGeneration) -> LexicalGenerationRef:
    return LexicalGenerationRef(
        lexical_generation_id=row.lexical_generation_id,
        workspace_id=row.workspace_id,
        source_generation_id=row.source_generation_id,
        kernel_commit_id=row.kernel_commit_id,
        snapshot_id=row.snapshot_id,
        tokenizer=row.tokenizer,
        tokenizer_config=json.loads(row.tokenizer_config_json),
        schema_version=row.schema_version,
        fts_table=row.fts_table,
        row_count=row.row_count,
        text_char_count=row.text_char_count,
        content_digest=row.content_digest,
        state=row.state,
        created_at=row.created_at.isoformat() if row.created_at else None,
        validated_at=row.validated_at.isoformat() if row.validated_at else None,
    )


async def _load_lexical(
    session_factory: async_sessionmaker, lexical_generation_id: str
) -> LexicalGenerationRef | None:
    async with session_factory() as session:
        row = await session.get(KernelLexicalGeneration, lexical_generation_id)
    return _lexical_ref(row) if row is not None else None


# ---------------------------------------------------------------------------
# Deterministic content digest (shared by build/validate/verify)
# ---------------------------------------------------------------------------


def _lexical_row_entry(row: LexicalRowRef) -> dict[str, Any]:
    return {
        "row": row.row_index,
        "record_id": row.record_id,
        "view_id": row.view_id,
        "node_id": row.node_id,
        "revision_ref": row.revision_ref,
        "text_hash": row.text_hash,
        "text_chars": row.text_chars,
    }


def _lexical_content_digest(
    *,
    workspace_id: str,
    kernel_commit_id: int,
    source_generation_id: str,
    row_entries: Sequence[Mapping],
) -> tuple[str, int, int]:
    view = {
        "workspace_id": workspace_id,
        "kernel_commit_id": kernel_commit_id,
        "source_generation_id": source_generation_id,
        "row_count": len(row_entries),
        "rows": list(row_entries),
    }
    digest = payload_byte_hash(canonical_json_bytes(to_json_ready(view)))
    text_chars = sum(int(entry["text_chars"]) for entry in row_entries)
    return digest, len(row_entries), text_chars


def _corpus_rows(corpus: Sequence[LexicalSourceRow]) -> list[LexicalRowRef]:
    return [
        LexicalRowRef(
            row_index=index,
            record_id=row.record_id,
            view_id=row.view_id,
            node_id=row.node_id,
            revision_ref=row.revision_ref,
            text_hash=payload_byte_hash(row.text.encode("utf-8")),
            text_chars=len(row.text),
        )
        for index, row in enumerate(corpus)
    ]


_FTS_DDL = (
    'CREATE VIRTUAL TABLE "{table}" USING fts5('
    "record_id UNINDEXED, view_id UNINDEXED, node_id UNINDEXED, text, "
    "tokenize='{tokenizer}')"
)
_FTS_INSERT = (
    'INSERT INTO "{table}"(rowid, record_id, view_id, node_id, text) '
    "VALUES (:row_index, :record_id, :view_id, :node_id, :text)"
)


# ---------------------------------------------------------------------------
# Publication service (lexical construction in this section)
# ---------------------------------------------------------------------------


class PublicationService:
    """Build → validate → publish lifecycle for publication sets.

    One instance owns publication writes for one database; instantiate
    once per process (see :func:`default_publication_service`). The
    service never mutates kernel truth: it reads committed state and
    materialized generations, and writes only derived publication/index
    rows. With a ``readiness_check`` (normally ``verify_database_ready``)
    it fails closed on an unmigrated database.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        readiness_check: Any = None,
        busy_retry_attempts: int = DEFAULT_BUSY_RETRY_ATTEMPTS,
        busy_retry_base_delay: float = DEFAULT_BUSY_RETRY_BASE_DELAY,
    ) -> None:
        self._session_factory = session_factory
        self._readiness_check = readiness_check
        self._ready = readiness_check is None
        self._busy_retry_attempts = busy_retry_attempts
        self._busy_retry_base_delay = busy_retry_base_delay

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self._readiness_check()
        self._ready = True

    # -- lexical construction ---------------------------------------------

    async def build_lexical(
        self,
        source_generation_id: str,
        *,
        tokenizer: str = LEXICAL_TOKENIZER,
        tokenizer_config: Mapping[str, Any] | None = None,
        _inject_fault_at: str | None = None,
    ) -> LexicalGenerationRef:
        """Build one immutable lexical generation from a pinned source.

        Returns the generation in state ``validated`` (or its existing
        immutable row for an idempotent rebuild). The result is staging
        state only: nothing is queryable until a published set names it.
        """
        if _inject_fault_at is not None and _inject_fault_at not in _LEXICAL_BUILD_PHASES:
            raise KernelError(f"unknown fault phase {_inject_fault_at!r}")
        await self._ensure_ready()
        return await self._build_lexical_validated(
            source_generation_id,
            tokenizer=tokenizer,
            tokenizer_config=tokenizer_config,
            fault=_inject_fault_at,
        )

    async def _build_lexical_validated(
        self,
        source_generation_id: str,
        *,
        tokenizer: str,
        tokenizer_config: Mapping[str, Any] | None,
        fault: str | None,
    ) -> LexicalGenerationRef:
        def maybe_inject(phase: str) -> None:
            if fault == phase:
                raise InjectedFaultError(phase)

        maybe_inject(PHASE_PUB_LEXICAL_BEGIN)

        if tokenizer not in _SUPPORTED_TOKENIZERS:
            raise KernelError(
                f"unsupported tokenizer {tokenizer!r}; v1 supports only "
                f"{sorted(_SUPPORTED_TOKENIZERS)} — a tokenizer change must "
                "arrive as a new projection version, never a silent reindex"
            )
        config = dict(tokenizer_config or {})
        unknown_keys = sorted(set(config) - _SUPPORTED_TOKENIZER_CONFIG_KEYS)
        if unknown_keys:
            raise KernelError(
                f"unsupported tokenizer config keys {unknown_keys}; v1 "
                f"supports only {sorted(_SUPPORTED_TOKENIZER_CONFIG_KEYS)}"
            )
        try:
            tokenizer_config_json = canonical_json_str(to_json_ready(config))
        except CanonicalValueError as exc:
            raise KernelError(f"tokenizer config rejected: {exc}") from exc

        async with self._session_factory() as session:
            source = await session.get(KernelGeneration, source_generation_id)
        if source is None:
            raise UnknownGenerationError(
                f"generation={source_generation_id}: no such generation"
            )

        view_rows = await _read_view_documents(
            self._session_factory, source_generation_id
        )
        maybe_inject(PHASE_PUB_LEXICAL_SOURCE_READ)

        corpus = extract_lexical_corpus(view_rows)
        rows = _corpus_rows(corpus)
        digest, row_count, text_chars = _lexical_content_digest(
            workspace_id=source.workspace_id,
            kernel_commit_id=source.kernel_commit_id,
            source_generation_id=source_generation_id,
            row_entries=[_lexical_row_entry(row) for row in rows],
        )

        lexical_generation_id = compute_lexical_identity(
            workspace_id=source.workspace_id,
            kernel_commit_id=source.kernel_commit_id,
            snapshot_id=source.snapshot_id,
            source_generation_id=source_generation_id,
            tokenizer=tokenizer,
            tokenizer_config_json=tokenizer_config_json,
        )

        existing = await _load_lexical(self._session_factory, lexical_generation_id)
        if existing is not None and existing.state not in (
            LEXICAL_STATE_STAGED,
            LEXICAL_STATE_FAILED,
        ):
            if existing.content_digest != digest:
                raise LexicalIntegrityError(
                    f"lexical generation={lexical_generation_id}: same declared "
                    f"inputs produced different content (stored "
                    f"{existing.content_digest}, rebuilt {digest}); refusing to "
                    "rewrite an immutable lexical generation"
                )
            return existing  # idempotent rebuild: immutable rows untouched

        await self._retry(
            lambda: self._stage_lexical_transaction(
                lexical_generation_id,
                source=source,
                tokenizer=tokenizer,
                tokenizer_config_json=tokenizer_config_json,
                digest=digest,
                row_count=row_count,
                text_chars=text_chars,
                corpus=corpus,
                rows=rows,
                maybe_inject=maybe_inject,
            )
        )

        maybe_inject(PHASE_PUB_LEXICAL_STAGED)
        maybe_inject(PHASE_PUB_LEXICAL_VALIDATE_BEGIN)
        return await self._retry(
            lambda: self._validate_lexical_transaction(lexical_generation_id)
        )

    async def _stage_lexical_transaction(
        self,
        lexical_generation_id: str,
        *,
        source: KernelGeneration,
        tokenizer: str,
        tokenizer_config_json: str,
        digest: str,
        row_count: int,
        text_chars: int,
        corpus: Sequence[LexicalSourceRow],
        rows: Sequence[LexicalRowRef],
        maybe_inject: Any,
    ) -> None:
        fts_table = fts_table_name(lexical_generation_id)
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(
                    KernelLexicalGeneration, lexical_generation_id
                )
                if existing is not None and existing.state not in (
                    LEXICAL_STATE_STAGED,
                    LEXICAL_STATE_FAILED,
                ):
                    if existing.content_digest != digest:
                        raise LexicalIntegrityError(
                            f"lexical generation={lexical_generation_id}: same "
                            "declared inputs produced different content (stored "
                            f"{existing.content_digest}, rebuilt {digest})"
                        )
                    return  # durable; the validate step returns its ref

                # Purge only never-queryable residue for this identity. A
                # residue row can never be named by a published set head
                # (activation requires state validated), but the head is
                # still checked defensively, mirroring the generation
                # purge guard.
                await session.execute(
                    delete(KernelLexicalRow).where(
                        KernelLexicalRow.lexical_generation_id == lexical_generation_id
                    )
                )
                referenced = await session.scalar(
                    select(func.count())
                    .select_from(KernelGenerationHead)
                    .where(
                        KernelGenerationHead.current_generation_id
                        == lexical_generation_id
                    )
                )
                if referenced:
                    raise LexicalStateError(
                        f"lexical generation={lexical_generation_id}: refusing "
                        "to purge residue referenced as a current generation"
                    )
                await session.execute(
                    delete(KernelLexicalGeneration).where(
                        KernelLexicalGeneration.lexical_generation_id
                        == lexical_generation_id,
                        KernelLexicalGeneration.state.in_(
                            [LEXICAL_STATE_STAGED, LEXICAL_STATE_FAILED]
                        ),
                    )
                )
                await session.execute(
                    text(f'DROP TABLE IF EXISTS "{fts_table}"')
                )

                await session.execute(
                    text(
                        _FTS_DDL.format(table=fts_table, tokenizer=tokenizer)
                    )
                )
                if rows:
                    await session.execute(
                        text(_FTS_INSERT.format(table=fts_table)),
                        [
                            {
                                "row_index": row.row_index,
                                "record_id": row.record_id,
                                "view_id": row.view_id,
                                "node_id": row.node_id,
                                "text": corpus[row.row_index].text,
                            }
                            for row in rows
                        ],
                    )
                maybe_inject(PHASE_PUB_LEXICAL_ROWS_MATERIALIZED)
                session.add(
                    KernelLexicalGeneration(
                        lexical_generation_id=lexical_generation_id,
                        workspace_id=source.workspace_id,
                        source_generation_id=source.generation_id,
                        kernel_commit_id=source.kernel_commit_id,
                        snapshot_id=source.snapshot_id,
                        tokenizer=tokenizer,
                        tokenizer_config_json=tokenizer_config_json,
                        schema_version=LEXICAL_SCHEMA_VERSION,
                        fts_table=fts_table,
                        row_count=row_count,
                        text_char_count=text_chars,
                        content_digest=digest,
                        state=LEXICAL_STATE_STAGED,
                    )
                )
                session.add_all(
                    KernelLexicalRow(
                        lexical_generation_id=lexical_generation_id,
                        row_index=row.row_index,
                        record_id=row.record_id,
                        view_id=row.view_id,
                        node_id=row.node_id,
                        revision_ref=row.revision_ref,
                        text_hash=row.text_hash,
                        text_chars=row.text_chars,
                    )
                    for row in rows
                )

    async def _validate_lexical_transaction(
        self, lexical_generation_id: str
    ) -> LexicalGenerationRef:
        """Recompute the digest from stored rows AND the FTS read-back.

        The staged manifest's ``content_digest`` (computed from the
        pinned source generation at staging time) is the expectation;
        recomputation runs over the stored locator rows and over the
        FTS5 table's own content, so divergence between any two of
        source, locators, and index marks the generation ``failed``.
        """
        problems = await _lexical_integrity_problems(
            self._session_factory, lexical_generation_id
        )
        new_state = LEXICAL_STATE_VALIDATED if not problems else LEXICAL_STATE_FAILED
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelLexicalGeneration)
                    .where(
                        KernelLexicalGeneration.lexical_generation_id
                        == lexical_generation_id,
                        KernelLexicalGeneration.state == LEXICAL_STATE_STAGED,
                    )
                    .values(
                        state=new_state,
                        validated_at=_utcnow()
                        if new_state == LEXICAL_STATE_VALIDATED
                        else None,
                    )
                )
                winner = None
                if result.rowcount != 1:
                    moved = await session.get(
                        KernelLexicalGeneration, lexical_generation_id
                    )
                    advanced = (
                        new_state == LEXICAL_STATE_VALIDATED
                        and moved is not None
                        and moved.state == LEXICAL_STATE_VALIDATED
                    )
                    if moved is not None and (moved.state == new_state or advanced):
                        winner = moved
                    else:
                        raise LexicalStateError(
                            f"lexical generation={lexical_generation_id}: state "
                            f"changed to {None if moved is None else moved.state!r} "
                            "concurrently during validation"
                        )
                else:
                    winner = await session.get(
                        KernelLexicalGeneration, lexical_generation_id
                    )
        if problems:
            raise LexicalIntegrityError(
                f"lexical generation={lexical_generation_id}: validation "
                "rejected: " + "; ".join(problems)
            )
        assert winner is not None
        return _lexical_ref(winner)

    # -- lexical resume step -----------------------------------------------

    async def validate_lexical(
        self, lexical_generation_id: str, *, _inject_fault_at: str | None = None
    ) -> LexicalGenerationRef:
        """Validate one staged lexical generation (resume after a crash).

        ``build_lexical`` already validates before returning; this
        explicit step exists so a generation left ``staged`` by a crash
        between staging and validation can be validated — or honestly
        rejected — without rebuilding.
        """
        if _inject_fault_at is not None and (
            _inject_fault_at != PHASE_PUB_LEXICAL_VALIDATE_BEGIN
        ):
            raise KernelError(
                f"fault phase {_inject_fault_at!r} does not apply to "
                "validate_lexical"
            )
        await self._ensure_ready()
        if _inject_fault_at == PHASE_PUB_LEXICAL_VALIDATE_BEGIN:
            raise InjectedFaultError(PHASE_PUB_LEXICAL_VALIDATE_BEGIN)
        return await self._retry(
            lambda: self._validate_lexical_transaction(lexical_generation_id)
        )

    async def get_lexical_generation(
        self, lexical_generation_id: str
    ) -> LexicalGenerationRef:
        ref = await _load_lexical(self._session_factory, lexical_generation_id)
        if ref is None:
            raise UnknownLexicalGenerationError(
                f"lexical generation={lexical_generation_id}: no such "
                "lexical generation"
            )
        return ref

    async def list_lexical_generations(
        self,
        *,
        workspace_id: str | None = None,
        state: str | None = None,
    ) -> list[LexicalGenerationRef]:
        stmt = select(KernelLexicalGeneration).order_by(
            KernelLexicalGeneration.created_at.asc()
        )
        if workspace_id is not None:
            stmt = stmt.where(KernelLexicalGeneration.workspace_id == workspace_id)
        if state is not None:
            stmt = stmt.where(KernelLexicalGeneration.state == state)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_lexical_ref(row) for row in rows]

    # -- retry plumbing ----------------------------------------------------

    async def _retry(self, operation: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._busy_retry_attempts):
            try:
                return await operation()
            except _ConcurrentPointerMove:
                last_error = None
            except OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_error = exc
            except IntegrityError as exc:
                text_ = str(exc).lower()
                if not any(
                    marker in text_
                    for marker in ("kernel_lexical", "kernel_publication")
                ):
                    raise
                last_error = exc
            await asyncio.sleep(_retry_delay(self._busy_retry_base_delay, attempt))
        raise KernelError(
            f"publication operation did not converge after "
            f"{self._busy_retry_attempts} attempts: {last_error or 'pointer moved'}"
        )


# ---------------------------------------------------------------------------
# Shared integrity checking (validate + verify + reader post-checks)
# ---------------------------------------------------------------------------


async def _read_view_documents(
    session_factory: async_sessionmaker, source_generation_id: str
) -> list[_CorpusRecord]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        KernelGenerationRecord.record_id,
                        KernelGenerationRecord.kernel_commit_id,
                        KernelGenerationRecord.identity_hash,
                        KernelGenerationRecord.payload_json,
                    )
                    .where(
                        KernelGenerationRecord.generation_id == source_generation_id,
                        KernelGenerationRecord.record_class == "view_document",
                    )
                    .order_by(
                        KernelGenerationRecord.kernel_commit_id.asc(),
                        KernelGenerationRecord.identity_hash.asc(),
                        KernelGenerationRecord.record_id.asc(),
                    )
                )
            )
            .all()
        )
    records: list[_CorpusRecord] = []
    for record_id, kernel_commit_id, identity_hash, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except Exception as exc:
            raise LexicalIntegrityError(
                f"source generation={source_generation_id} record={record_id!r}: "
                f"view document payload unreadable: {exc}"
            ) from exc
        records.append(
            _CorpusRecord(
                record_id=record_id,
                kernel_commit_id=kernel_commit_id,
                identity_hash=identity_hash,
                payload=payload,
            )
        )
    return records


async def _lexical_integrity_problems(
    session_factory: async_sessionmaker, lexical_generation_id: str
) -> list[str]:
    """Cross-check manifest, locator rows, and the FTS table itself."""
    async with session_factory() as session:
        manifest = await session.get(KernelLexicalGeneration, lexical_generation_id)
        if manifest is None:
            raise UnknownLexicalGenerationError(
                f"lexical generation={lexical_generation_id}: no such lexical "
                "generation"
            )
        if manifest.state == LEXICAL_STATE_STAGED:
            pass  # the state under validation
        stored_rows = (
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
        fts_rows = (
            (
                await session.execute(
                    text(
                        'SELECT rowid, record_id, view_id, node_id, text FROM '
                        f'"{manifest.fts_table}" ORDER BY rowid'
                    )
                )
            )
            .all()
        )

    problems: list[str] = []
    row_entries = [
        _lexical_row_entry(
            LexicalRowRef(
                row_index=row.row_index,
                record_id=row.record_id,
                view_id=row.view_id,
                node_id=row.node_id,
                revision_ref=row.revision_ref,
                text_hash=row.text_hash,
                text_chars=row.text_chars,
            )
        )
        for row in stored_rows
    ]
    recomputed, row_count, text_chars = _lexical_content_digest(
        workspace_id=manifest.workspace_id,
        kernel_commit_id=manifest.kernel_commit_id,
        source_generation_id=manifest.source_generation_id,
        row_entries=row_entries,
    )
    if recomputed != manifest.content_digest:
        problems.append(
            f"content digest mismatch: manifest {manifest.content_digest}, "
            f"recomputed {recomputed}"
        )
    if row_count != manifest.row_count:
        problems.append(
            f"row count mismatch: manifest {manifest.row_count}, "
            f"stored {row_count}"
        )
    if text_chars != manifest.text_char_count:
        problems.append(
            f"text char count mismatch: manifest {manifest.text_char_count}, "
            f"stored {text_chars}"
        )

    if len(fts_rows) != row_count:
        problems.append(
            f"FTS row count mismatch: locators {row_count}, index "
            f"{len(fts_rows)}"
        )
    for locator, fts_row in zip(stored_rows, fts_rows, strict=False):
        rowid, record_id, view_id, node_id, node_text = fts_row
        if rowid != locator.row_index:
            problems.append(
                f"FTS rowid {rowid} misaligned with locator row "
                f"{locator.row_index}"
            )
            continue
        if (record_id, view_id, node_id) != (
            locator.record_id,
            locator.view_id,
            locator.node_id,
        ):
            problems.append(
                f"FTS row {rowid} locator mismatch: index "
                f"({record_id!r}, {view_id!r}, {node_id!r}) vs stored "
                f"({locator.record_id!r}, {locator.view_id!r}, "
                f"{locator.node_id!r})"
            )
        text_hash = payload_byte_hash((node_text or "").encode("utf-8"))
        if text_hash != locator.text_hash or len(node_text or "") != locator.text_chars:
            problems.append(
                f"FTS row {rowid} text diverges from stored hash for "
                f"record {locator.record_id!r}"
            )

    # FTS5's own structural integrity check (self-contained table: the
    # index is verified against its stored content).
    async with session_factory() as session:
        try:
            await session.execute(
                text(f'INSERT INTO "{manifest.fts_table}"("{manifest.fts_table}") '
                     "VALUES('integrity-check')")
            )
        except Exception as exc:
            problems.append(f"FTS integrity-check rejected: {exc}")
    return problems


async def verify_lexical_generation(
    session_factory: async_sessionmaker, lexical_generation_id: str
) -> LexicalVerification:
    """Explicit deep verification of one lexical generation.

    Catches post-validation tampering of the manifest, locator rows, or
    the FTS table itself. Read-only; reports problems instead of
    mutating state.
    """
    async with session_factory() as session:
        manifest = await session.get(KernelLexicalGeneration, lexical_generation_id)
        if manifest is None:
            raise UnknownLexicalGenerationError(
                f"lexical generation={lexical_generation_id}: no such lexical "
                "generation"
            )
        row_count = manifest.row_count
    problems = await _lexical_integrity_problems(session_factory, lexical_generation_id)
    return LexicalVerification(
        lexical_generation_id=lexical_generation_id,
        ok=not problems,
        problems=tuple(problems),
        checked_rows=row_count,
    )


_default_service: PublicationService | None = None


def default_publication_service() -> PublicationService:
    """Process-wide service bound to the production engine.

    Fails closed until ``verify_database_ready`` passes.
    """
    global _default_service
    if _default_service is None:
        from app.database import async_session_factory
        from app.db_migration import verify_database_ready

        _default_service = PublicationService(
            async_session_factory,
            readiness_check=verify_database_ready,
        )
    return _default_service
