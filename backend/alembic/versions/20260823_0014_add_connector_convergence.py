"""add connector convergence stream + inbox state (PR71B)

Revision ID: 20260823_0014
Revises: 20260819_0013
Create Date: 2026-08-23

Amendment 16B.7 durable state: one checkpoint/lifecycle row per connector
stream, and the append-only provider-event inbox whose unique
(stream, provider_event_id) key is the durable redelivery-dedupe
authority. Both start empty — no connector progress or receipt evidence
can be fabricated for pre-existing databases.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_0014"
down_revision = "20260819_0013"
branch_labels = None
depends_on = None

_STREAMS = "kernel_connector_streams"
_INBOX = "kernel_connector_inbox"

_STREAM_INDEXES = (
    ("ix_kernel_connector_streams_workspace_id", _STREAMS, ("workspace_id",)),
    ("ix_kernel_connector_streams_state", _STREAMS, ("state",)),
)

_INBOX_INDEXES = (
    ("ix_kernel_connector_inbox_workspace_id", _INBOX, ("workspace_id",)),
    ("ix_kernel_connector_inbox_stream_id", _INBOX, ("stream_id",)),
    (
        "ix_kernel_connector_inbox_stream_state",
        _INBOX,
        ("stream_id", "applied_state"),
    ),
)


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _existing_indexes(bind, table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)

    if _STREAMS not in existing:
        op.create_table(
            _STREAMS,
            sa.Column("stream_id", sa.String(length=128), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("cursor_token", sa.String(length=512), nullable=False),
            sa.Column("cursor_seq", sa.BigInteger(), nullable=True),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("reconciliation_reason", sa.Text(), nullable=True),
            sa.Column(
                "applied_kernel_commit_id", sa.Integer(), nullable=False
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.PrimaryKeyConstraint("stream_id"),
        )

    if _INBOX not in existing:
        op.create_table(
            _INBOX,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("stream_id", sa.String(length=128), nullable=False),
            sa.Column(
                "provider_event_id", sa.String(length=256), nullable=False
            ),
            sa.Column("event_kind", sa.String(length=32), nullable=False),
            sa.Column(
                "provider_item_id", sa.String(length=256), nullable=True
            ),
            sa.Column(
                "provider_revision", sa.String(length=256), nullable=True
            ),
            sa.Column("provider_seq", sa.BigInteger(), nullable=True),
            sa.Column("applied_state", sa.String(length=32), nullable=False),
            sa.Column(
                "applied_kernel_commit_id", sa.Integer(), nullable=False
            ),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column(
                "received_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.ForeignKeyConstraint(
                ["stream_id"],
                ["kernel_connector_streams.stream_id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "stream_id", "provider_event_id", name="uq_kernel_connector_inbox_event"
            ),
        )

    for name, table, columns in _STREAM_INDEXES + _INBOX_INDEXES:
        if table in _existing_tables(bind) and name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)
    if _INBOX in existing:
        op.drop_table(_INBOX)
    if _STREAMS in existing:
        op.drop_table(_STREAMS)
