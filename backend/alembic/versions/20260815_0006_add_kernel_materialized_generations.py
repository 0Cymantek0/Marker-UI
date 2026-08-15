"""add kernel materialized generation tables (PR65A)

Revision ID: 20260815_0006
Revises: 20260815_0005
Create Date: 2026-08-16

Creates the PR65A snapshot/materialization read-model tables:

* kernel_generations — one immutable manifest row per materialized
  generation: pinned snapshot identity, materializer/schema/config
  identity, lifecycle state (staged/validated/active/superseded/failed),
  and the deterministic content digest of the materialized view;
* kernel_generation_records — committed record metadata materialized
  into a generation, bounded to the generation's pinned cut;
* kernel_generation_edges — dependency edges materialized into a
  generation, bounded to the pinned cut;
* kernel_generation_heads — per-workspace current accepted read
  generation; the atomic pointer switch happens on this row.

All four tables are derived, rebuildable state — never a second truth
authority. They arrive empty on upgrade; committed kernel history and
payload objects are untouched. The tables are new; no existing table is
altered, so the upgrade is convergent by construction (inspect-and-skip
guards kept for symmetry with the guarded chain). Downgrade drops
materialized generations: it discards derived read state irreversibly
(rebuild restores it, but activation history is lost) and exists only
for schema symmetry — never use on a database whose serving state must
survive.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260815_0006"
down_revision = "20260815_0005"
branch_labels = None
depends_on = None

_PR65A_TABLES = (
    "kernel_generation_edges",
    "kernel_generation_records",
    "kernel_generation_heads",
    "kernel_generations",
)

_PR65A_INDEXES = (
    ("ix_kernel_generations_workspace_id", "kernel_generations", ["workspace_id"]),
    ("ix_kernel_generations_state", "kernel_generations", ["state"]),
    (
        "ix_kernel_generation_records_class",
        "kernel_generation_records",
        ["generation_id", "record_class"],
    ),
    ("ix_kernel_generation_edges_generation", "kernel_generation_edges", ["generation_id"]),
)


def _existing_indexes(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "kernel_generations" not in existing:
        op.create_table(
            "kernel_generations",
            sa.Column("generation_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("snapshot_id", sa.String(length=80), nullable=False),
            sa.Column("materializer_id", sa.String(length=100), nullable=False),
            sa.Column("materializer_version", sa.String(length=32), nullable=False),
            sa.Column("schema_version", sa.String(length=32), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("content_digest", sa.String(length=80), nullable=False),
            sa.Column("commit_count", sa.Integer(), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("edge_count", sa.Integer(), nullable=False),
            sa.Column("record_class_counts_json", sa.Text(), nullable=False),
            sa.Column("required_payload_state", sa.String(length=24), nullable=False),
            sa.Column("completeness", sa.String(length=16), nullable=False),
            sa.Column("payload_state_counts_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("validated_at", sa.DateTime(), nullable=True),
            sa.Column("activated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("generation_id"),
        )

    if "kernel_generation_records" not in existing:
        op.create_table(
            "kernel_generation_records",
            sa.Column("generation_id", sa.String(length=80), nullable=False),
            sa.Column("record_id", sa.String(length=36), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("record_class", sa.String(length=50), nullable=False),
            sa.Column("record_type", sa.String(length=100), nullable=False),
            sa.Column("schema_version", sa.String(length=32), nullable=False),
            sa.Column("identity_hash", sa.String(length=80), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("payload_byte_hash", sa.String(length=80), nullable=True),
            sa.Column("payload_length", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("generation_id", "record_id"),
        )

    if "kernel_generation_edges" not in existing:
        op.create_table(
            "kernel_generation_edges",
            sa.Column("generation_id", sa.String(length=80), nullable=False),
            sa.Column("edge_id", sa.String(length=36), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("edge_kind", sa.String(length=64), nullable=False),
            sa.Column("source_record_id", sa.String(length=36), nullable=False),
            sa.Column("target_record_id", sa.String(length=36), nullable=False),
            sa.PrimaryKeyConstraint("generation_id", "edge_id"),
        )

    if "kernel_generation_heads" not in existing:
        op.create_table(
            "kernel_generation_heads",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("current_generation_id", sa.String(length=80), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id"),
        )

    for name, table, columns in _PR65A_INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    # WARNING: discards materialized generation state (derived read model
    # and activation history). Schema symmetry only; rebuilds restore the
    # derived content but never the activation trail. Never use on a live
    # serving database.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _PR65A_TABLES:
        if table in existing:
            op.drop_table(table)
