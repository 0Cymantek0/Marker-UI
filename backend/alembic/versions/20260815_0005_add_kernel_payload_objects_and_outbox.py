"""add kernel payload objects and outbox tables (PR64)

Revision ID: 20260815_0005
Revises: 20260815_0004
Create Date: 2026-08-16

Creates the PR64 durability tables:

* kernel_payload_objects — registry of durably published content-addressed
  payload objects (blob key, length, store profile, locator). Registry
  rows are inserted inside the commit transaction that references them,
  so a visible row implies the object was staged and verified before
  database acceptance;
* kernel_outbox — durable at-least-once successor-work intent, enqueued
  atomically with its authorizing commit and identified by a
  deterministic dedupe key.

Both tables are new; no existing table is altered, so the upgrade is
convergent by construction (inspect-and-skip guards kept for symmetry
with the guarded chain). Downgrade drops durability truth: payload
registry rows (availability state) and outbox intent (scheduled work)
are discarded irreversibly and exist only for schema symmetry — never
use on a database whose truth history must survive.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260815_0005"
down_revision = "20260815_0004"
branch_labels = None
depends_on = None

_PR64_TABLES = (
    "kernel_outbox",
    "kernel_payload_objects",
)

_PR64_INDEXES = (
    ("ix_kernel_outbox_workspace_id", "kernel_outbox", "workspace_id"),
    ("ix_kernel_outbox_state", "kernel_outbox", "state"),
)


def _existing_indexes(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "kernel_payload_objects" not in existing:
        op.create_table(
            "kernel_payload_objects",
            sa.Column("blob_key", sa.String(length=80), nullable=False),
            sa.Column("payload_length", sa.Integer(), nullable=False),
            sa.Column("store_profile", sa.String(length=64), nullable=False),
            sa.Column("storage_locator", sa.String(length=256), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("blob_key"),
        )

    if "kernel_outbox" not in existing:
        op.create_table(
            "kernel_outbox",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("work_kind", sa.String(length=64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("dedupe_key", sa.String(length=80), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key", name="uq_kernel_outbox_dedupe_key"),
        )

    for name, table, column in _PR64_INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    # WARNING: discards payload availability truth and pending successor
    # work. Schema symmetry only; never use on a live truth database.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _PR64_TABLES:
        if table in existing:
            op.drop_table(table)
