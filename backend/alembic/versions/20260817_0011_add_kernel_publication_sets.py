"""add kernel publication set and lexical generation tables (PR76)

Revision ID: 20260817_0011
Revises: 20260817_0010
Create Date: 2026-08-17

Creates the PR76 atomic-publication serving state:

* kernel_lexical_generations — one immutable manifest row per lexical
  (FTS5) index generation: the pinned source materialized generation,
  tokenizer/config identity, deterministic content digest, row counts,
  and lifecycle state (staged/validated/active/superseded/failed, where
  "active" only ever means "referenced by the published set").
* kernel_lexical_rows — per-row lexical locators (record id, view id,
  node id, view revision ref, text hash) mapping every searchable FTS
  row back to the materialized record it came from. The FTS5 virtual
  tables themselves are runtime-managed (created at build time, one per
  lexical generation, named ``kernel_fts_<hex>``) and are deliberately
  NOT migration-managed: they are rebuildable derived state that exists
  only after a build, never schema authority.
* kernel_publication_sets — one immutable manifest row per publication
  set: the exact member generation identities (materialized + lexical,
  plus an optional nullable vector slot whose NULL is an explicit
  "absent", never a fallback), the pinned kernel cut, and a lifecycle
  state (staged/validated/published/superseded/failed).
* kernel_publication_heads — one row per (workspace, profile) naming the
  current published set. The single atomic publication switch is the
  transactional update of this row; there is exactly one authoritative
  "published" resolution, never independent per-layer current pointers.
* kernel_publication_pins — bounded wall-clock read leases over one
  publication set (the publication twin of kernel_reader_pins): an
  unexpired pin protects the set and every member generation from
  collection until released or lapsed.

All five tables are derived, rebuildable serving state over kernel
truth — never a second truth authority. The upgrade is convergent by
construction (inspect-and-skip guards); new rows arrive empty: no
publication or index truth is fabricated for pre-PR76 state. Downgrade
drops publication/index serving state and any runtime-managed
``kernel_fts_*`` virtual tables — schema symmetry only, never use on a
live database whose published lineage must stay queryable.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260817_0011"
down_revision = "20260817_0010"
branch_labels = None
depends_on = None

_PR76_TABLES = (
    # drop order: children before manifests/heads
    "kernel_lexical_rows",
    "kernel_publication_pins",
    "kernel_publication_heads",
    "kernel_publication_sets",
    "kernel_lexical_generations",
)

_PR76_INDEXES = (
    ("ix_kernel_lexical_generations_workspace", "kernel_lexical_generations", ("workspace_id",)),
    ("ix_kernel_lexical_generations_source", "kernel_lexical_generations", ("source_generation_id",)),
    ("ix_kernel_lexical_generations_state", "kernel_lexical_generations", ("state",)),
    ("ix_kernel_lexical_rows_record", "kernel_lexical_rows", ("lexical_generation_id", "record_id")),
    ("ix_kernel_publication_sets_workspace", "kernel_publication_sets", ("workspace_id",)),
    ("ix_kernel_publication_sets_state", "kernel_publication_sets", ("state",)),
    ("ix_kernel_publication_pins_expires", "kernel_publication_pins", ("expires_at",)),
)

# Runtime-managed FTS5 virtual tables (and their shadow tables) share
# this prefix; the downgrade drops any that linger so a downgraded
# database does not keep unreachable index state.
_FTS_TABLE_PREFIX = "kernel_fts_"


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _existing_indexes(bind, table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)

    if "kernel_lexical_generations" not in existing:
        op.create_table(
            "kernel_lexical_generations",
            sa.Column("lexical_generation_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("source_generation_id", sa.String(length=80), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("snapshot_id", sa.String(length=80), nullable=False),
            sa.Column("tokenizer", sa.String(length=64), nullable=False),
            sa.Column("tokenizer_config_json", sa.Text(), nullable=False),
            sa.Column("schema_version", sa.String(length=32), nullable=False),
            sa.Column("fts_table", sa.String(length=96), nullable=False),
            sa.Column("row_count", sa.Integer(), nullable=False),
            sa.Column("text_char_count", sa.Integer(), nullable=False),
            sa.Column("content_digest", sa.String(length=80), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("validated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("lexical_generation_id"),
        )
    if "kernel_lexical_rows" not in existing:
        op.create_table(
            "kernel_lexical_rows",
            sa.Column("lexical_generation_id", sa.String(length=80), nullable=False),
            sa.Column("row_index", sa.Integer(), nullable=False),
            sa.Column("record_id", sa.String(length=36), nullable=False),
            sa.Column("view_id", sa.String(length=64), nullable=False),
            sa.Column("node_id", sa.String(length=128), nullable=False),
            sa.Column("revision_ref", sa.String(length=80), nullable=False),
            sa.Column("text_hash", sa.String(length=80), nullable=False),
            sa.Column("text_chars", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("lexical_generation_id", "row_index"),
        )
    if "kernel_publication_sets" not in existing:
        op.create_table(
            "kernel_publication_sets",
            sa.Column("publication_set_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("profile", sa.String(length=64), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("snapshot_id", sa.String(length=80), nullable=False),
            sa.Column("materialized_generation_id", sa.String(length=80), nullable=False),
            sa.Column("lexical_generation_id", sa.String(length=80), nullable=False),
            sa.Column("vector_generation_id", sa.String(length=80), nullable=True),
            sa.Column("content_digest", sa.String(length=80), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("validated_at", sa.DateTime(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("publication_set_id"),
        )
    if "kernel_publication_heads" not in existing:
        op.create_table(
            "kernel_publication_heads",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("profile", sa.String(length=64), nullable=False),
            sa.Column("current_publication_set_id", sa.String(length=80), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id", "profile"),
        )
    if "kernel_publication_pins" not in existing:
        op.create_table(
            "kernel_publication_pins",
            sa.Column("pin_id", sa.String(length=36), nullable=False),
            sa.Column("publication_set_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("pin_id"),
        )

    for name, table, columns in _PR76_INDEXES:
        if table in existing or table in _existing_tables(bind):
            if name not in _existing_indexes(bind, table):
                op.create_index(name, table, columns)


def downgrade() -> None:
    # WARNING: discards publication/index serving state. Schema symmetry
    # only; never use on a live database whose published lineage must
    # stay queryable. Kernel truth (records/commits/manifests) survives.
    bind = op.get_bind()
    for table in _PR76_TABLES:
        if table in _existing_tables(bind):
            op.drop_table(table)
    # Runtime-managed FTS5 virtual tables are dropped explicitly: they
    # are unreachable serving state once the lexical manifests are gone.
    fts_tables = sorted(
        name
        for (name,) in bind.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if name.startswith(_FTS_TABLE_PREFIX)
    )
    for table in fts_tables:
        op.drop_table(table)
