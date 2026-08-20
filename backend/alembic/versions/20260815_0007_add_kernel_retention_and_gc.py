"""add kernel retention roots, reader pins, and GC tombstones (PR65B)

Revision ID: 20260815_0007
Revises: 20260815_0006
Create Date: 2026-08-16

Creates the PR65B retention contract tables:

* kernel_retention_roots — declared durable retention holds. A root
  names a workspace cut plus a required payload class (and optionally a
  target generation); collection must treat every active root's closure
  as live. The intrinsic current-generation roots are read from
  kernel_generation_heads and are not duplicated here.
* kernel_reader_pins — bounded wall-clock read leases over one
  generation. Unexpired pins are active roots; crashed readers' pins
  lapse when the lease expires, so restart safety never depends on
  process memory.
* kernel_payload_retirements — GC tombstones. A row authorizes (or
  records the outcome of) physical retirement of one payload object:
  pending / deleted / failed. The kernel_payload_objects registry row is
  deliberately kept so retired bytes stay an honest availability fact.

The tables are new; no existing table is altered, so the upgrade is
convergent by construction (inspect-and-skip guards kept for symmetry
with the guarded chain). Upgrade preserves all committed history,
payloads, and generations, and the new tables arrive empty — no data is
ever retired by a migration.

Downgrade drops the retention contract. Any payload bytes already
retired while these tables existed are NOT restored by the downgrade —
deleted bytes cannot be resurrected by schema manipulation; re-supplying
the exact bytes through staging is the only heal path. Downgrade also
forgets live holds and pins, so a subsequent upgrade on a database whose
serving state must survive is unsafe. Schema symmetry only.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260815_0007"
down_revision = "20260815_0006"
branch_labels = None
depends_on = None

_PR65B_TABLES = (
    "kernel_reader_pins",
    "kernel_payload_retirements",
    "kernel_retention_roots",
)

_PR65B_INDEXES = (
    ("ix_kernel_retention_roots_workspace_id", "kernel_retention_roots", ["workspace_id"]),
    ("ix_kernel_retention_roots_state", "kernel_retention_roots", ["state"]),
    ("ix_kernel_reader_pins_generation", "kernel_reader_pins", ["generation_id"]),
    ("ix_kernel_reader_pins_expires", "kernel_reader_pins", ["expires_at"]),
    ("ix_kernel_payload_retirements_state", "kernel_payload_retirements", ["state"]),
)


def _existing_indexes(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "kernel_retention_roots" not in existing:
        op.create_table(
            "kernel_retention_roots",
            sa.Column("root_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("root_kind", sa.String(length=50), nullable=False),
            sa.Column("target_generation_id", sa.String(length=80), nullable=True),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("required_payload_state", sa.String(length=24), nullable=False),
            sa.Column("producer_json", sa.Text(), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("root_id"),
        )

    if "kernel_reader_pins" not in existing:
        op.create_table(
            "kernel_reader_pins",
            sa.Column("pin_id", sa.String(length=36), nullable=False),
            sa.Column("generation_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("pin_id"),
        )

    if "kernel_payload_retirements" not in existing:
        op.create_table(
            "kernel_payload_retirements",
            sa.Column("blob_key", sa.String(length=80), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("reason", sa.String(length=64), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("swept_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("blob_key"),
        )

    for name, table, columns in _PR65B_INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    # WARNING: forgets live holds, pins, and tombstone history. Bytes
    # already retired are NOT restored — re-supply through staging is
    # the only heal. Schema symmetry only; never use on a live serving
    # database whose retention policy must keep operating.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _PR65B_TABLES:
        if table in existing:
            op.drop_table(table)
