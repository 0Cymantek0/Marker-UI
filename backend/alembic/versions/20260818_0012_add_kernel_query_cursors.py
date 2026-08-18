"""add durable server-side query cursor state (PR79A)

Revision ID: 20260818_0012
Revises: 20260817_0011
Create Date: 2026-08-18

The cursor token is only an HMAC-authenticated reference.  This migration
stores query, publication/snapshot, authorization, keyset, budget, pin, and
replay state locally so none of those dimensions need to enter a client token.
The table starts empty: no historical continuation state can be fabricated.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260818_0012"
down_revision = "20260817_0011"
branch_labels = None
depends_on = None

_TABLE = "kernel_query_cursors"
_INDEXES = (
    ("ix_kernel_query_cursors_workspace_id", _TABLE, ("workspace_id",)),
    ("ix_kernel_query_cursors_expires_at", _TABLE, ("expires_at",)),
    ("ix_kernel_query_cursors_status", _TABLE, ("status",)),
    (
        "ix_kernel_query_cursors_workspace_status",
        _TABLE,
        ("workspace_id", "status"),
    ),
)


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _existing_indexes(bind, table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)
    if _TABLE not in existing:
        op.create_table(
            _TABLE,
            sa.Column("handle", sa.String(length=128), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("query_json", sa.Text(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=True),
            sa.Column("publication_json", sa.Text(), nullable=True),
            sa.Column("authorization_json", sa.Text(), nullable=True),
            sa.Column("keyset_json", sa.Text(), nullable=False),
            sa.Column("cumulative_budget_json", sa.Text(), nullable=False),
            sa.Column("page_count", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("pin_id", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("nonce", sa.String(length=128), nullable=False),
            sa.Column("replay_state", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("handle"),
        )

    for name, table, columns in _INDEXES:
        if table in _existing_tables(bind) and name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in _existing_tables(bind):
        op.drop_table(_TABLE)
