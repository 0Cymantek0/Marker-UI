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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    InjectedFaultError,
    InvalidPublicationProfileError,
    KernelError,
    LexicalIntegrityError,
    LexicalQueryError,
    LexicalStateError,
    PublicationIntegrityError,
    PublicationStateError,
    RetentionContractError,
    UnknownGenerationError,
    UnknownLexicalGenerationError,
    UnknownPublicationSetError,
    UnknownReaderPinError,
)
from app.kernel.models import (
    KernelGeneration,
    KernelGenerationHead,
    KernelGenerationRecord,
    KernelLexicalGeneration,
    KernelLexicalRow,
    KernelPublicationHead,
    KernelPublicationPin,
    KernelPublicationSet,
)
from app.kernel.retention import DEFAULT_PIN_LEASE_SECONDS
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
    "LexicalHit",
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
    "PublicationPinView",
    "PublicationReader",
    "PublicationService",
    "PublicationSetRef",
    "PublicationSetVerification",
    "acquire_publication_pin",
    "active_publication_pins",
    "compute_lexical_identity",
    "compute_publication_set_identity",
    "default_publication_service",
    "extract_lexical_corpus",
    "fts_table_name",
    "open_pinned_publication",
    "open_published_reader",
    "purge_expired_publication_pins",
    "release_publication_pin",
    "renew_publication_pin",
    "resolve_published_set",
    "validate_publication_profile",
    "verify_lexical_generation",
    "verify_publication_set",
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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


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


@dataclass(frozen=True)
class PublicationSetRef:
    """Manifest-level view of one publication set."""

    publication_set_id: str
    workspace_id: str
    profile: str
    kernel_commit_id: int
    snapshot_id: str
    materialized_generation_id: str
    lexical_generation_id: str
    vector_generation_id: str | None
    content_digest: str
    state: str
    created_at: str | None
    validated_at: str | None
    published_at: str | None


@dataclass(frozen=True)
class PublicationSetVerification:
    """Deep verification result for one publication set."""

    publication_set_id: str
    ok: bool
    problems: tuple[str, ...]


@dataclass(frozen=True)
class PublicationPinView:
    """View of one durable publication reader pin."""

    pin_id: str
    publication_set_id: str
    workspace_id: str
    created_at: datetime
    expires_at: datetime

    @property
    def active(self) -> bool:
        return self.expires_at > _utcnow()


def _pin_view(row: KernelPublicationPin) -> PublicationPinView:
    return PublicationPinView(
        pin_id=row.pin_id,
        publication_set_id=row.publication_set_id,
        workspace_id=row.workspace_id,
        created_at=_as_utc(row.created_at),
        expires_at=_as_utc(row.expires_at) or _utcnow(),
    )


@dataclass(frozen=True)
class LexicalHit:
    """One source-resolvable lexical query hit.

    FTS text alone is never sufficient provenance: every hit carries
    the locator of the materialized record/node it resolves through and
    the re-verified text hash, plus the identity of the publication set
    and lexical generation that produced it (I12: reads are
    attributable).
    """

    publication_set_id: str
    lexical_generation_id: str
    row_index: int
    record_id: str
    view_id: str
    node_id: str
    revision_ref: str
    text_hash: str
    rank: float
    text: str


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


def _set_ref(row: KernelPublicationSet) -> PublicationSetRef:
    return PublicationSetRef(
        publication_set_id=row.publication_set_id,
        workspace_id=row.workspace_id,
        profile=row.profile,
        kernel_commit_id=row.kernel_commit_id,
        snapshot_id=row.snapshot_id,
        materialized_generation_id=row.materialized_generation_id,
        lexical_generation_id=row.lexical_generation_id,
        vector_generation_id=row.vector_generation_id,
        content_digest=row.content_digest,
        state=row.state,
        created_at=row.created_at.isoformat() if row.created_at else None,
        validated_at=row.validated_at.isoformat() if row.validated_at else None,
        published_at=row.published_at.isoformat() if row.published_at else None,
    )


async def _load_set(
    session_factory: async_sessionmaker, publication_set_id: str
) -> PublicationSetRef | None:
    async with session_factory() as session:
        row = await session.get(KernelPublicationSet, publication_set_id)
    return _set_ref(row) if row is not None else None


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


def _publication_set_digest(
    *,
    workspace_id: str,
    profile: str,
    kernel_commit_id: int,
    snapshot_id: str,
    materialized_generation_id: str,
    lexical_generation_id: str,
    vector_generation_id: str | None,
    lexical_content_digest: str,
) -> str:
    """Deterministic content digest of one publication set's manifest.

    Covers every compatibility dimension plus the lexical member's own
    content digest, so a set row whose members were tampered with after
    staging can never validate.
    """
    view = {
        "workspace_id": workspace_id,
        "profile": profile,
        "kernel_commit_id": kernel_commit_id,
        "snapshot_id": snapshot_id,
        "materialized_generation_id": materialized_generation_id,
        "lexical_generation_id": lexical_generation_id,
        "vector_generation_id": vector_generation_id,
        "lexical_content_digest": lexical_content_digest,
    }
    return payload_byte_hash(canonical_json_bytes(to_json_ready(view)))


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

    # -- publication set lifecycle ------------------------------------------

    async def stage_publication_set(
        self,
        *,
        materialized_generation_id: str,
        lexical_generation_id: str | None = None,
        vector_generation_id: str | None = None,
        profile: str = DEFAULT_PUBLICATION_PROFILE,
        _inject_fault_at: str | None = None,
    ) -> PublicationSetRef:
        """Stage one publication-set manifest naming its exact members.

        The lexical member is built from the materialized generation
        when not supplied. Staging is invisible: nothing is queryable
        until a set is validated AND activated.
        """
        if _inject_fault_at is not None and (
            _inject_fault_at
            not in _LEXICAL_BUILD_PHASES
            | {PHASE_PUB_SET_STAGED, PHASE_PUB_LEXICAL_VALIDATED}
        ):
            raise KernelError(f"unknown fault phase {_inject_fault_at!r}")
        await self._ensure_ready()
        profile = validate_publication_profile(profile)

        async with self._session_factory() as session:
            source = await session.get(KernelGeneration, materialized_generation_id)
        if source is None:
            raise UnknownGenerationError(
                f"generation={materialized_generation_id}: no such generation"
            )

        if lexical_generation_id is None:
            lexical = await self._build_lexical_validated(
                materialized_generation_id,
                tokenizer=LEXICAL_TOKENIZER,
                tokenizer_config=None,
                fault=_inject_fault_at
                if _inject_fault_at in _LEXICAL_BUILD_PHASES
                else None,
            )
        else:
            lexical = await self.get_lexical_generation(lexical_generation_id)
        if lexical.state != LEXICAL_STATE_VALIDATED:
            raise LexicalStateError(
                f"lexical generation={lexical.lexical_generation_id}: cannot "
                f"stage a set from lexical state {lexical.state!r}"
            )
        if _inject_fault_at == PHASE_PUB_LEXICAL_VALIDATED:
            raise InjectedFaultError(PHASE_PUB_LEXICAL_VALIDATED)
        self._check_member_compatibility(source, lexical)

        if vector_generation_id is not None and (
            not isinstance(vector_generation_id, str) or not vector_generation_id
        ):
            raise KernelError(
                "vector_generation_id must be a non-empty string or None "
                "(None is the explicit absent vector layer)"
            )

        publication_set_id = compute_publication_set_identity(
            workspace_id=source.workspace_id,
            profile=profile,
            kernel_commit_id=source.kernel_commit_id,
            snapshot_id=source.snapshot_id,
            materialized_generation_id=materialized_generation_id,
            lexical_generation_id=lexical.lexical_generation_id,
            vector_generation_id=vector_generation_id,
        )
        digest = _publication_set_digest(
            workspace_id=source.workspace_id,
            profile=profile,
            kernel_commit_id=source.kernel_commit_id,
            snapshot_id=source.snapshot_id,
            materialized_generation_id=materialized_generation_id,
            lexical_generation_id=lexical.lexical_generation_id,
            vector_generation_id=vector_generation_id,
            lexical_content_digest=lexical.content_digest,
        )

        existing = await _load_set(self._session_factory, publication_set_id)
        if existing is not None and existing.state not in (
            PUBLICATION_STATE_STAGED,
            PUBLICATION_STATE_FAILED,
        ):
            if existing.content_digest != digest:
                raise PublicationIntegrityError(
                    f"publication set={publication_set_id}: same declared "
                    f"members produced different content (stored "
                    f"{existing.content_digest}, rebuilt {digest}); refusing "
                    "to rewrite an immutable publication set"
                )
            return existing  # idempotent restage

        await self._retry(
            lambda: self._stage_set_transaction(
                publication_set_id,
                workspace_id=source.workspace_id,
                profile=profile,
                kernel_commit_id=source.kernel_commit_id,
                snapshot_id=source.snapshot_id,
                materialized_generation_id=materialized_generation_id,
                lexical_generation_id=lexical.lexical_generation_id,
                vector_generation_id=vector_generation_id,
                digest=digest,
            )
        )
        if _inject_fault_at == PHASE_PUB_SET_STAGED:
            raise InjectedFaultError(PHASE_PUB_SET_STAGED)
        ref = await _load_set(self._session_factory, publication_set_id)
        assert ref is not None
        return ref

    @staticmethod
    def _check_member_compatibility(
        source: KernelGeneration, lexical: LexicalGenerationRef
    ) -> None:
        """The compatibility key every staged set must already satisfy.

        Workspace, kernel cut, snapshot, and source lineage must agree
        between the required members; the lexical layer must have been
        built from exactly the materialized generation it names.
        """
        problems: list[str] = []
        if lexical.workspace_id != source.workspace_id:
            problems.append(
                f"lexical member workspace {lexical.workspace_id!r} != "
                f"materialized {source.workspace_id!r}"
            )
        if lexical.kernel_commit_id != source.kernel_commit_id:
            problems.append(
                f"lexical member cut {lexical.kernel_commit_id} != "
                f"materialized {source.kernel_commit_id}"
            )
        if lexical.snapshot_id != source.snapshot_id:
            problems.append(
                f"lexical member snapshot {lexical.snapshot_id!r} != "
                f"materialized {source.snapshot_id!r}"
            )
        if lexical.source_generation_id != source.generation_id:
            problems.append(
                f"lexical member was built from source generation "
                f"{lexical.source_generation_id!r}, not "
                f"{source.generation_id!r}"
            )
        if problems:
            raise PublicationIntegrityError(
                "publication set members are incompatible: " + "; ".join(problems)
            )

    async def _stage_set_transaction(
        self,
        publication_set_id: str,
        *,
        workspace_id: str,
        profile: str,
        kernel_commit_id: int,
        snapshot_id: str,
        materialized_generation_id: str,
        lexical_generation_id: str,
        vector_generation_id: str | None,
        digest: str,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                existing = await session.get(KernelPublicationSet, publication_set_id)
                if existing is not None and existing.state not in (
                    PUBLICATION_STATE_STAGED,
                    PUBLICATION_STATE_FAILED,
                ):
                    if existing.content_digest != digest:
                        raise PublicationIntegrityError(
                            f"publication set={publication_set_id}: same "
                            "declared members produced different content "
                            f"(stored {existing.content_digest}, rebuilt {digest})"
                        )
                    return  # durable; the caller re-reads the ref
                await session.execute(
                    delete(KernelPublicationSet).where(
                        KernelPublicationSet.publication_set_id == publication_set_id,
                        KernelPublicationSet.state.in_(
                            [PUBLICATION_STATE_STAGED, PUBLICATION_STATE_FAILED]
                        ),
                    )
                )
                session.add(
                    KernelPublicationSet(
                        publication_set_id=publication_set_id,
                        workspace_id=workspace_id,
                        profile=profile,
                        kernel_commit_id=kernel_commit_id,
                        snapshot_id=snapshot_id,
                        materialized_generation_id=materialized_generation_id,
                        lexical_generation_id=lexical_generation_id,
                        vector_generation_id=vector_generation_id,
                        content_digest=digest,
                        state=PUBLICATION_STATE_STAGED,
                    )
                )

    async def validate_publication_set(
        self, publication_set_id: str, *, _inject_fault_at: str | None = None
    ) -> PublicationSetRef:
        """Enforce the full compatibility key before activation.

        A failed validation marks the set ``failed`` and raises; the
        previously published set (if any) is untouched.
        """
        if _inject_fault_at is not None and (
            _inject_fault_at != PHASE_PUB_VALIDATE_BEGIN
        ):
            raise KernelError(
                f"fault phase {_inject_fault_at!r} does not apply to "
                "validate_publication_set"
            )
        await self._ensure_ready()
        if _inject_fault_at == PHASE_PUB_VALIDATE_BEGIN:
            raise InjectedFaultError(PHASE_PUB_VALIDATE_BEGIN)
        return await self._retry(
            lambda: self._validate_set_transaction(publication_set_id)
        )

    async def _validate_set_transaction(
        self, publication_set_id: str
    ) -> PublicationSetRef:
        async with self._session_factory() as session:
            row = await session.get(KernelPublicationSet, publication_set_id)
            if row is None:
                raise UnknownPublicationSetError(
                    f"publication set={publication_set_id}: no such set"
                )
            if row.state in (PUBLICATION_STATE_VALIDATED, PUBLICATION_STATE_PUBLISHED):
                return _set_ref(row)  # concurrent validator won the race
            if row.state != PUBLICATION_STATE_STAGED:
                raise PublicationStateError(
                    f"publication set={publication_set_id}: cannot validate "
                    f"from state {row.state!r}"
                )

        problems = await _set_integrity_problems(
            self._session_factory, publication_set_id
        )
        new_state = (
            PUBLICATION_STATE_VALIDATED if not problems else PUBLICATION_STATE_FAILED
        )
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelPublicationSet)
                    .where(
                        KernelPublicationSet.publication_set_id == publication_set_id,
                        KernelPublicationSet.state == PUBLICATION_STATE_STAGED,
                    )
                    .values(
                        state=new_state,
                        validated_at=_utcnow()
                        if new_state == PUBLICATION_STATE_VALIDATED
                        else None,
                    )
                )
                winner = None
                if result.rowcount != 1:
                    moved = await session.get(KernelPublicationSet, publication_set_id)
                    advanced = (
                        new_state == PUBLICATION_STATE_VALIDATED
                        and moved is not None
                        and moved.state
                        in (PUBLICATION_STATE_VALIDATED, PUBLICATION_STATE_PUBLISHED)
                    )
                    if moved is not None and (moved.state == new_state or advanced):
                        winner = moved
                    else:
                        raise PublicationStateError(
                            f"publication set={publication_set_id}: state changed "
                            f"to {None if moved is None else moved.state!r} "
                            "concurrently during validation"
                        )
                else:
                    winner = await session.get(KernelPublicationSet, publication_set_id)
        if problems:
            raise PublicationIntegrityError(
                f"publication set={publication_set_id}: validation rejected: "
                + "; ".join(problems)
            )
        assert winner is not None
        return _set_ref(winner)

    async def activate_publication_set(
        self, publication_set_id: str, *, _inject_fault_at: str | None = None
    ) -> PublicationSetRef:
        """Atomically make one validated set the published state."""
        if _inject_fault_at is not None and (
            _inject_fault_at
            not in {
                PHASE_PUB_VALIDATED,
                PHASE_PUB_PRE_ACTIVATE,
                PHASE_PUB_POST_ACTIVATE,
            }
        ):
            raise KernelError(
                f"fault phase {_inject_fault_at!r} does not apply to "
                "activate_publication_set"
            )
        await self._ensure_ready()
        return await self._retry(
            lambda: self._activate_set(publication_set_id, fault=_inject_fault_at)
        )

    async def _activate_set(
        self, publication_set_id: str, *, fault: str | None
    ) -> PublicationSetRef:
        def maybe_inject(phase: str) -> None:
            if fault == phase:
                raise InjectedFaultError(phase)

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(KernelPublicationSet, publication_set_id)
                if row is None:
                    raise UnknownPublicationSetError(
                        f"publication set={publication_set_id}: no such set"
                    )
                if row.state == PUBLICATION_STATE_PUBLISHED:
                    current = await session.scalar(
                        select(
                            KernelPublicationHead.current_publication_set_id
                        ).where(
                            KernelPublicationHead.workspace_id == row.workspace_id,
                            KernelPublicationHead.profile == row.profile,
                        )
                    )
                    if current == publication_set_id:
                        return _set_ref(row)  # idempotent activation
                    raise PublicationStateError(
                        f"publication set={publication_set_id}: published but "
                        "not current; the pointer moved concurrently"
                    )
                if row.state != PUBLICATION_STATE_VALIDATED:
                    raise PublicationStateError(
                        f"publication set={publication_set_id}: cannot activate "
                        f"from state {row.state!r}"
                    )
                maybe_inject(PHASE_PUB_PRE_ACTIVATE)

                observed = await session.scalar(
                    select(KernelPublicationHead.current_publication_set_id).where(
                        KernelPublicationHead.workspace_id == row.workspace_id,
                        KernelPublicationHead.profile == row.profile,
                    )
                )
                if observed is not None and observed != publication_set_id:
                    result = await session.execute(
                        update(KernelPublicationSet)
                        .where(
                            KernelPublicationSet.publication_set_id == observed,
                            KernelPublicationSet.state
                            == PUBLICATION_STATE_PUBLISHED,
                        )
                        .values(state=PUBLICATION_STATE_SUPERSEDED)
                    )
                    if result.rowcount != 1:
                        raise _ConcurrentPointerMove(
                            f"previous publication set {observed} was not "
                            "published; retrying with a fresh pointer observation"
                        )
                if observed is None:
                    await session.execute(
                        sqlite_insert(KernelPublicationHead)
                        .values(
                            workspace_id=row.workspace_id,
                            profile=row.profile,
                            current_publication_set_id=publication_set_id,
                            updated_at=_utcnow(),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                KernelPublicationHead.workspace_id,
                                KernelPublicationHead.profile,
                            ]
                        )
                    )
                    written = await session.scalar(
                        select(
                            KernelPublicationHead.current_publication_set_id
                        ).where(
                            KernelPublicationHead.workspace_id == row.workspace_id,
                            KernelPublicationHead.profile == row.profile,
                        )
                    )
                    if written != publication_set_id:
                        raise _ConcurrentPointerMove(
                            "publication head appeared concurrently; retrying"
                        )
                else:
                    result = await session.execute(
                        update(KernelPublicationHead)
                        .where(
                            KernelPublicationHead.workspace_id == row.workspace_id,
                            KernelPublicationHead.profile == row.profile,
                            KernelPublicationHead.current_publication_set_id.is_(
                                observed
                            ),
                        )
                        .values(
                            current_publication_set_id=publication_set_id,
                            updated_at=_utcnow(),
                        )
                    )
                    if result.rowcount != 1:
                        raise _ConcurrentPointerMove(
                            "publication pointer moved concurrently; retrying"
                        )
                result = await session.execute(
                    update(KernelPublicationSet)
                    .where(
                        KernelPublicationSet.publication_set_id == publication_set_id,
                        KernelPublicationSet.state == PUBLICATION_STATE_VALIDATED,
                    )
                    .values(
                        state=PUBLICATION_STATE_PUBLISHED,
                        published_at=_utcnow(),
                    )
                )
                if result.rowcount != 1:
                    raise _ConcurrentPointerMove(
                        "publication set state moved concurrently; retrying"
                    )
            # session.begin() exit == COMMIT == the publication
            # linearization point.

        maybe_inject(PHASE_PUB_POST_ACTIVATE)
        ref = await _load_set(self._session_factory, publication_set_id)
        assert ref is not None
        return ref

    async def publish(
        self,
        *,
        materialized_generation_id: str,
        lexical_generation_id: str | None = None,
        vector_generation_id: str | None = None,
        profile: str = DEFAULT_PUBLICATION_PROFILE,
        _inject_fault_at: str | None = None,
    ) -> PublicationSetRef:
        """Convenience lifecycle: stage, validate, then atomically publish."""
        if _inject_fault_at is not None and (
            _inject_fault_at not in PUBLICATION_FAULT_PHASES
        ):
            raise KernelError(f"unknown fault phase {_inject_fault_at!r}")
        await self._ensure_ready()

        staged = await self.stage_publication_set(
            materialized_generation_id=materialized_generation_id,
            lexical_generation_id=lexical_generation_id,
            vector_generation_id=vector_generation_id,
            profile=profile,
            _inject_fault_at=_inject_fault_at
            if _inject_fault_at
            in _LEXICAL_BUILD_PHASES
            | {PHASE_PUB_SET_STAGED, PHASE_PUB_LEXICAL_VALIDATED}
            else None,
        )
        if staged.state == PUBLICATION_STATE_PUBLISHED:
            return staged  # idempotent republish of the live set

        if _inject_fault_at == PHASE_PUB_VALIDATE_BEGIN:
            raise InjectedFaultError(PHASE_PUB_VALIDATE_BEGIN)
        validated = await self.validate_publication_set(staged.publication_set_id)

        if _inject_fault_at == PHASE_PUB_VALIDATED:
            raise InjectedFaultError(PHASE_PUB_VALIDATED)
        return await self.activate_publication_set(
            validated.publication_set_id,
            _inject_fault_at=_inject_fault_at
            if _inject_fault_at
            in {PHASE_PUB_PRE_ACTIVATE, PHASE_PUB_POST_ACTIVATE}
            else None,
        )

    async def get_publication_set(
        self, publication_set_id: str
    ) -> PublicationSetRef:
        ref = await _load_set(self._session_factory, publication_set_id)
        if ref is None:
            raise UnknownPublicationSetError(
                f"publication set={publication_set_id}: no such set"
            )
        return ref

    async def list_publication_sets(
        self,
        *,
        workspace_id: str | None = None,
        state: str | None = None,
    ) -> list[PublicationSetRef]:
        stmt = select(KernelPublicationSet).order_by(
            KernelPublicationSet.created_at.asc()
        )
        if workspace_id is not None:
            stmt = stmt.where(KernelPublicationSet.workspace_id == workspace_id)
        if state is not None:
            stmt = stmt.where(KernelPublicationSet.state == state)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_set_ref(row) for row in rows]

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


async def _set_integrity_problems(
    session_factory: async_sessionmaker, publication_set_id: str
) -> list[str]:
    """The full compatibility key, evaluated against durable state.

    Enforced dimensions (derived from real branch semantics, not future
    PR77/78 fields): scope and cut agreement of every required member,
    source-generation lineage of the lexical layer, materialized member
    lifecycle fitness, lexical deep integrity, per-row locator
    membership in the materialized generation, and manifest digest
    agreement. A NULL vector member is the explicit absent layer —
    nothing is borrowed and nothing is checked beyond its grammar.
    """
    async with session_factory() as session:
        row = await session.get(KernelPublicationSet, publication_set_id)
        assert row is not None  # caller checked existence
        materialized = await session.get(
            KernelGeneration, row.materialized_generation_id
        )
        lexical = await session.get(KernelLexicalGeneration, row.lexical_generation_id)
        if lexical is not None:
            stored_rows = (
                await session.execute(
                    select(KernelLexicalRow.record_id)
                    .where(
                        KernelLexicalRow.lexical_generation_id
                        == row.lexical_generation_id
                    )
                    .distinct()
                )
            ).all()
            member_record_ids = {
                value
                for (value,) in (
                    await session.execute(
                        select(KernelGenerationRecord.record_id).where(
                            KernelGenerationRecord.generation_id
                            == row.materialized_generation_id
                        )
                    )
                ).all()
            }

    problems: list[str] = []
    if materialized is None:
        problems.append(
            f"materialized member {row.materialized_generation_id!r} is missing"
        )
    else:
        if materialized.workspace_id != row.workspace_id:
            problems.append(
                f"materialized member workspace {materialized.workspace_id!r} != "
                f"set {row.workspace_id!r}"
            )
        if materialized.kernel_commit_id != row.kernel_commit_id:
            problems.append(
                f"materialized member cut {materialized.kernel_commit_id} != "
                f"set {row.kernel_commit_id}"
            )
        if materialized.snapshot_id != row.snapshot_id:
            problems.append(
                f"materialized member snapshot {materialized.snapshot_id!r} != "
                f"set {row.snapshot_id!r}"
            )
        if materialized.state not in ("validated", "active", "superseded"):
            problems.append(
                "materialized member state "
                f"{materialized.state!r} is not publishable"
            )
    if lexical is None:
        problems.append(f"lexical member {row.lexical_generation_id!r} is missing")
    else:
        if lexical.workspace_id != row.workspace_id:
            problems.append(
                f"lexical member workspace {lexical.workspace_id!r} != "
                f"set {row.workspace_id!r}"
            )
        if lexical.kernel_commit_id != row.kernel_commit_id:
            problems.append(
                f"lexical member cut {lexical.kernel_commit_id} != set "
                f"{row.kernel_commit_id}"
            )
        if lexical.snapshot_id != row.snapshot_id:
            problems.append(
                f"lexical member snapshot {lexical.snapshot_id!r} != set "
                f"{row.snapshot_id!r}"
            )
        if lexical.source_generation_id != row.materialized_generation_id:
            problems.append(
                "lexical member was built from source generation "
                f"{lexical.source_generation_id!r}, not the set's "
                f"{row.materialized_generation_id!r}"
            )
        if lexical.state != LEXICAL_STATE_VALIDATED:
            problems.append(
                f"lexical member state {lexical.state!r} is not validated"
            )
        problems.extend(
            f"lexical integrity: {problem}"
            for problem in await _lexical_integrity_problems(
                session_factory, row.lexical_generation_id
            )
        )
        orphan_record_ids = sorted(
            {value for (value,) in stored_rows} - member_record_ids
        )
        if orphan_record_ids:
            problems.append(
                "lexical rows name records outside the materialized "
                f"member: {orphan_record_ids[:5]}"
                + (
                    f" (+{len(orphan_record_ids) - 5} more)"
                    if len(orphan_record_ids) > 5
                    else ""
                )
            )
    if row.vector_generation_id is not None and not row.vector_generation_id:
        problems.append("vector member id is present but empty")

    expected_digest = _publication_set_digest(
        workspace_id=row.workspace_id,
        profile=row.profile,
        kernel_commit_id=row.kernel_commit_id,
        snapshot_id=row.snapshot_id,
        materialized_generation_id=row.materialized_generation_id,
        lexical_generation_id=row.lexical_generation_id,
        vector_generation_id=row.vector_generation_id,
        lexical_content_digest=lexical.content_digest if lexical else "",
    )
    if expected_digest != row.content_digest:
        problems.append(
            f"set digest mismatch: manifest {row.content_digest}, "
            f"recomputed {expected_digest}"
        )
    return problems


async def resolve_published_set(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    profile: str = DEFAULT_PUBLICATION_PROFILE,
) -> PublicationSetRef | None:
    """The one authoritative published set, from durable state alone.

    A fresh process recovers the published state here; ``None`` means
    the scope has never published. A head naming a missing set fails
    closed as an integrity fault — it is never silently skipped.
    """
    from app.kernel.commit import validate_workspace_id

    validate_workspace_id(workspace_id)
    validate_publication_profile(profile)
    async with session_factory() as session:
        current_id = await session.scalar(
            select(KernelPublicationHead.current_publication_set_id).where(
                KernelPublicationHead.workspace_id == workspace_id,
                KernelPublicationHead.profile == profile,
            )
        )
        if current_id is None:
            return None
        row = await session.get(KernelPublicationSet, current_id)
    if row is None:
        raise PublicationIntegrityError(
            f"workspace={workspace_id!r} profile={profile!r}: published pointer "
            f"names {current_id!r} but no such publication set row exists"
        )
    return _set_ref(row)


async def verify_publication_set(
    session_factory: async_sessionmaker, publication_set_id: str
) -> PublicationSetVerification:
    """Explicit deep verification of one publication set (read-only)."""
    async with session_factory() as session:
        row = await session.get(KernelPublicationSet, publication_set_id)
        if row is None:
            raise UnknownPublicationSetError(
                f"publication set={publication_set_id}: no such set"
            )
    problems = await _set_integrity_problems(session_factory, publication_set_id)
    if row.state != PUBLICATION_STATE_PUBLISHED:
        problems.append(f"set state is {row.state!r}, not published")
    return PublicationSetVerification(
        publication_set_id=publication_set_id,
        ok=not problems,
        problems=tuple(problems),
    )


# ---------------------------------------------------------------------------
# Publication reader pins (bounded leases over published sets)
# ---------------------------------------------------------------------------


async def acquire_publication_pin(
    session_factory: async_sessionmaker,
    publication_set_id: str,
    *,
    lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS,
) -> PublicationPinView:
    """Acquire a durable read lease over one publication set.

    The pin protects the set and every member generation from
    collection until it expires, is released, or is renewed. A crashed
    reader's pin lapses when the lease expires — safety never depends
    on process memory.
    """
    if lease_seconds <= 0:
        raise RetentionContractError("lease_seconds must be positive")
    now = _utcnow()
    expires = now + timedelta(seconds=lease_seconds)
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(KernelPublicationSet, publication_set_id)
            if row is None:
                raise UnknownPublicationSetError(
                    f"publication set={publication_set_id}: no such set"
                )
            pin = KernelPublicationPin(
                pin_id=str(uuid.uuid4()),
                publication_set_id=publication_set_id,
                workspace_id=row.workspace_id,
                created_at=now,
                expires_at=expires,
            )
            session.add(pin)
    return PublicationPinView(
        pin_id=pin.pin_id,
        publication_set_id=publication_set_id,
        workspace_id=row.workspace_id,
        created_at=now,
        expires_at=expires,
    )


async def renew_publication_pin(
    session_factory: async_sessionmaker,
    pin_id: str,
    *,
    lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS,
) -> PublicationPinView:
    """Extend one publication pin's lease from now; expired pins cannot
    be revived."""
    if lease_seconds <= 0:
        raise RetentionContractError("lease_seconds must be positive")
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(KernelPublicationPin, pin_id)
            view = _pin_view(row) if row is not None else None
            if view is None or not view.active:
                raise UnknownReaderPinError(
                    f"publication pin {pin_id!r}: no such active pin "
                    "(released, purged, or the lease expired)"
                )
            row.expires_at = _utcnow() + timedelta(seconds=lease_seconds)
            session.add(row)
        refreshed = await session.get(KernelPublicationPin, pin_id)
    assert refreshed is not None
    return _pin_view(refreshed)


async def release_publication_pin(
    session_factory: async_sessionmaker, pin_id: str
) -> bool:
    """Release one publication pin (row deleted); False when gone."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(KernelPublicationPin).where(
                    KernelPublicationPin.pin_id == pin_id
                )
            )
            released = result.rowcount == 1
    return released


async def active_publication_pins(
    session_factory: async_sessionmaker,
    *,
    publication_set_id: str | None = None,
) -> tuple[PublicationPinView, ...]:
    """All currently active (unexpired) publication pins."""
    stmt = select(KernelPublicationPin).where(
        KernelPublicationPin.expires_at > _utcnow()
    )
    if publication_set_id is not None:
        stmt = stmt.where(
            KernelPublicationPin.publication_set_id == publication_set_id
        )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return tuple(_pin_view(row) for row in rows)


async def purge_expired_publication_pins(
    session_factory: async_sessionmaker,
) -> int:
    """Delete lapsed publication pin rows (called by collection)."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(KernelPublicationPin).where(
                    KernelPublicationPin.expires_at <= _utcnow()
                )
            )
            purged = result.rowcount
    return purged


# ---------------------------------------------------------------------------
# Published-set-pinned reader with lexical search
# ---------------------------------------------------------------------------


class PublicationReader:
    """Bounded, publication-set-pinned read surface (internal probe).

    The reader resolves the published set ONCE at construction and uses
    that identity for its whole lifetime: a publication switch mid-read
    cannot change any layer underneath it. Lexical queries run only
    against the lexical generation named by the pinned set, and every
    hit is re-verified against its stored locator (text hash, row
    alignment) before it is returned as valid — an orphan or tampered
    hit fails closed instead of surfacing.

    GC protection: a reader constructed with ``pin_id`` holds a durable
    lease that keeps the set and its members alive until :meth:`close`
    releases it (or the lease lapses).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        set_ref: PublicationSetRef,
        lexical: LexicalGenerationRef,
        *,
        pin_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._set = set_ref
        self._lexical = lexical
        self.pin_id = pin_id

    async def __aenter__(self) -> "PublicationReader":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    @property
    def publication_set_id(self) -> str:
        return self._set.publication_set_id

    @property
    def lexical_generation_id(self) -> str:
        return self._lexical.lexical_generation_id

    @property
    def pinned(self) -> bool:
        return self.pin_id is not None

    def explain(self) -> dict[str, Any]:
        """Attribution metadata for this reader's reads (I12)."""
        return {
            "publication_set_id": self._set.publication_set_id,
            "workspace_id": self._set.workspace_id,
            "profile": self._set.profile,
            "kernel_commit_id": self._set.kernel_commit_id,
            "snapshot_id": self._set.snapshot_id,
            "materialized_generation_id": self._set.materialized_generation_id,
            "lexical_generation_id": self._lexical.lexical_generation_id,
            "tokenizer": self._lexical.tokenizer,
            "vector_generation_id": self._set.vector_generation_id,
            "lexical_row_count": self._lexical.row_count,
        }

    async def renew(self, *, lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS) -> None:
        """Extend this reader's GC lease from now (long reads)."""
        if self.pin_id is None:
            raise KernelError(
                "reader holds no pin; open with open_published_reader to renew"
            )
        await renew_publication_pin(
            self._session_factory, self.pin_id, lease_seconds=lease_seconds
        )

    async def close(self) -> None:
        """Release this reader's pin (unpinned readers: no-op)."""
        if self.pin_id is not None:
            await release_publication_pin(self._session_factory, self.pin_id)
            self.pin_id = None

    async def summary(self) -> PublicationSetRef:
        ref = await _load_set(self._session_factory, self._set.publication_set_id)
        if ref is None:
            raise UnknownPublicationSetError(
                f"publication set={self._set.publication_set_id}: no such set"
            )
        return ref

    async def verify(self) -> PublicationSetVerification:
        return await verify_publication_set(
            self._session_factory, self._set.publication_set_id
        )

    async def search(
        self, query: str, *, limit: int = 100
    ) -> tuple[LexicalHit, ...]:
        """Query the pinned lexical generation only.

        The query must be a valid FTS5 MATCH expression; malformed
        syntax raises :class:`LexicalQueryError` rather than being
        guessed at. Results are ordered by bm25 rank with row-index
        tie-breaking for determinism.
        """
        if not isinstance(query, str) or not query.strip():
            raise LexicalQueryError("lexical query must be a non-empty string")
        if limit <= 0:
            raise KernelError("limit must be positive")
        fts = self._lexical.fts_table
        try:
            async with self._session_factory() as session:
                fts_rows = (
                    (
                        await session.execute(
                            text(
                                f'SELECT rowid, record_id, view_id, node_id, text, '
                                f'bm25("{fts}") AS bm25_rank FROM "{fts}" '
                                f'WHERE "{fts}" MATCH :query '
                                "ORDER BY bm25_rank, rowid LIMIT :limit"
                            ),
                            {"query": query, "limit": limit},
                        )
                    )
                    .mappings()
                    .all()
                )
                if fts_rows:
                    locators = {
                        row.row_index: row
                        for row in (
                            (
                                await session.execute(
                                    select(KernelLexicalRow).where(
                                        KernelLexicalRow.lexical_generation_id
                                        == self._lexical.lexical_generation_id,
                                        KernelLexicalRow.row_index.in_(
                                            [int(r["rowid"]) for r in fts_rows]
                                        ),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                    }
                else:
                    locators = {}
        except OperationalError as exc:
            # The statement is parameterized and the table provably exists
            # (the reader was built from a validated manifest), so a
            # non-busy operational failure here is the FTS5 engine
            # rejecting the MATCH expression itself ("syntax error",
            # "unterminated string", "malformed MATCH expression", ...).
            # Surface that as a typed query rejection, never as a raw
            # database error and never as a partial result.
            if not _is_busy(exc):
                raise LexicalQueryError(f"lexical query rejected: {exc}") from exc
            raise

        hits: list[LexicalHit] = []
        for fts_row in fts_rows:
            row_index = int(fts_row["rowid"])
            node_text = fts_row["text"] or ""
            text_hash = payload_byte_hash(node_text.encode("utf-8"))
            locator = locators.get(row_index)
            if locator is None:
                raise PublicationIntegrityError(
                    f"lexical hit row {row_index} has no locator in generation "
                    f"{self._lexical.lexical_generation_id}; refusing to serve "
                    "an orphan hit"
                )
            if (
                locator.record_id != fts_row["record_id"]
                or locator.view_id != fts_row["view_id"]
                or locator.node_id != fts_row["node_id"]
            ):
                raise PublicationIntegrityError(
                    f"lexical hit row {row_index} locator mismatch between "
                    "index and stored rows"
                )
            if text_hash != locator.text_hash or len(node_text) != locator.text_chars:
                raise PublicationIntegrityError(
                    f"lexical hit row {row_index} text hash mismatch "
                    f"(stored {locator.text_hash}, indexed {text_hash}); the "
                    "pinned lexical generation was tampered with"
                )
            hits.append(
                LexicalHit(
                    publication_set_id=self._set.publication_set_id,
                    lexical_generation_id=self._lexical.lexical_generation_id,
                    row_index=row_index,
                    record_id=locator.record_id,
                    view_id=locator.view_id,
                    node_id=locator.node_id,
                    revision_ref=locator.revision_ref,
                    text_hash=locator.text_hash,
                    rank=float(fts_row["bm25_rank"]),
                    text=node_text,
                )
            )
        return tuple(hits)


async def open_published_reader(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    profile: str = DEFAULT_PUBLICATION_PROFILE,
    pin_lease_seconds: float | None = DEFAULT_PIN_LEASE_SECONDS,
) -> PublicationReader | None:
    """Resolve the published set once, then pin it for this reader.

    ``None`` means the scope has never published. The reader's identity
    is frozen at construction: later publication switches do not affect
    an open reader (I9). ``pin_lease_seconds=None`` opens an unpinned
    reader — appropriate only while the set is expected to stay current.
    """
    resolved = await resolve_published_set(
        session_factory, workspace_id, profile=profile
    )
    if resolved is None:
        return None
    async with session_factory() as session:
        lexical_row = await session.get(
            KernelLexicalGeneration, resolved.lexical_generation_id
        )
    if lexical_row is None:
        raise PublicationIntegrityError(
            f"publication set={resolved.publication_set_id}: lexical member "
            f"{resolved.lexical_generation_id!r} is missing"
        )
    pin_id = None
    if pin_lease_seconds is not None:
        pin = await acquire_publication_pin(
            session_factory,
            resolved.publication_set_id,
            lease_seconds=pin_lease_seconds,
        )
        pin_id = pin.pin_id
    return PublicationReader(
        session_factory,
        resolved,
        _lexical_ref(lexical_row),
        pin_id=pin_id,
    )


async def open_pinned_publication(
    session_factory: async_sessionmaker,
    publication_set_id: str,
    *,
    lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS,
) -> PublicationReader:
    """Open one named publication set under a durable pin.

    This is the long-reader entry point for sets that may not stay
    current (a superseded set being read across a later publication):
    the pin protects the named set and every member until released or
    lapsed, independent of what the head currently names.
    """
    async with session_factory() as session:
        row = await session.get(KernelPublicationSet, publication_set_id)
        if row is None:
            raise UnknownPublicationSetError(
                f"publication set={publication_set_id}: no such set"
            )
        lexical_row = await session.get(
            KernelLexicalGeneration, row.lexical_generation_id
        )
    if lexical_row is None:
        raise PublicationIntegrityError(
            f"publication set={publication_set_id}: lexical member "
            f"{row.lexical_generation_id!r} is missing"
        )
    pin = await acquire_publication_pin(
        session_factory, publication_set_id, lease_seconds=lease_seconds
    )
    return PublicationReader(
        session_factory,
        _set_ref(row),
        _lexical_ref(lexical_row),
        pin_id=pin.pin_id,
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
