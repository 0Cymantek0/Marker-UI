"""add Truth Kernel commit spine tables (PR63A)

Revision ID: 20260815_0004
Revises: 20260709_0003
Create Date: 2026-08-15

Creates the local Truth Kernel persistence spine:

* kernel_commit_heads — per-workspace/shard commit head (serialization point);
* kernel_commit_manifests — one immutable manifest per accepted commit;
* kernel_records — append-only committed logical record metadata with
  canonical semantic identity hashes and separate payload byte hashes;
* kernel_record_edges — dependency/reference edges between records.

These tables are new; no existing table is altered, so the upgrade is
convergent by construction (inspect-and-skip guards kept for symmetry
with the guarded chain). Downgrade drops the spine tables; it discards
kernel commit history irreversibly and exists only for schema symmetry —
production truth history is append-only and must not be downgraded away.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260815_0004"
down_revision = "20260709_0003"
branch_labels = None
depends_on = None

_KERNEL_TABLES = (
    "kernel_record_edges",
    "kernel_records",
    "kernel_commit_manifests",
    "kernel_commit_heads",
)

_KERNEL_INDEXES = (
    ("ix_kernel_records_workspace_id", "kernel_records", "workspace_id"),
    ("ix_kernel_records_kernel_commit_id", "kernel_records", "kernel_commit_id"),
    ("ix_kernel_records_record_class", "kernel_records", "record_class"),
    ("ix_kernel_records_identity_hash", "kernel_records", "identity_hash"),
    ("ix_kernel_record_edges_workspace_id", "kernel_record_edges", "workspace_id"),
    ("ix_kernel_record_edges_kernel_commit_id", "kernel_record_edges", "kernel_commit_id"),
    ("ix_kernel_record_edges_source_record_id", "kernel_record_edges", "source_record_id"),
    ("ix_kernel_record_edges_target_record_id", "kernel_record_edges", "target_record_id"),
)


def _existing_indexes(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "kernel_commit_heads" not in existing:
        op.create_table(
            "kernel_commit_heads",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("head_kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id"),
        )

    if "kernel_commit_manifests" not in existing:
        op.create_table(
            "kernel_commit_manifests",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("parent_kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("edge_count", sa.Integer(), nullable=False),
            sa.Column("record_class_counts_json", sa.Text(), nullable=False),
            sa.Column("record_identity_root", sa.String(length=80), nullable=False),
            sa.Column("edge_identity_root", sa.String(length=80), nullable=False),
            sa.Column("manifest_identity_hash", sa.String(length=80), nullable=False),
            sa.Column("kernel_schema_version", sa.String(length=32), nullable=False),
            sa.Column("canonicalization_profile", sa.String(length=32), nullable=False),
            sa.Column("producer_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id", "kernel_commit_id"),
        )

    if "kernel_records" not in existing:
        op.create_table(
            "kernel_records",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("record_class", sa.String(length=50), nullable=False),
            sa.Column("record_type", sa.String(length=100), nullable=False),
            sa.Column("schema_version", sa.String(length=32), nullable=False),
            sa.Column("identity_hash", sa.String(length=80), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("payload_byte_hash", sa.String(length=80), nullable=True),
            sa.Column("payload_length", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "workspace_id", "identity_hash", name="uq_kernel_records_workspace_identity"
            ),
        )

    if "kernel_record_edges" not in existing:
        op.create_table(
            "kernel_record_edges",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("edge_kind", sa.String(length=64), nullable=False),
            sa.Column("source_record_id", sa.String(length=36), nullable=False),
            sa.Column("target_record_id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["source_record_id"], ["kernel_records.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["target_record_id"], ["kernel_records.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    for name, table, column in _KERNEL_INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    # WARNING: drops the append-only kernel commit history. Schema-symmetry
    # only; never use on a database whose truth history must survive.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _KERNEL_TABLES:
        if table in existing:
            op.drop_table(table)
