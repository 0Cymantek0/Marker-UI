"""add durable queue fields and job events

Revision ID: 20260626_0002
Revises: 20260626_0001
Create Date: 2026-06-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260626_0002"
down_revision = "20260626_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    job_columns = {column["name"] for column in sa.inspect(bind).get_columns("conversion_jobs")}
    _add_column_if_missing(job_columns, "queue_backend", sa.Column("queue_backend", sa.String(length=50), nullable=True))
    _add_column_if_missing(job_columns, "queued_at", sa.Column("queued_at", sa.DateTime(), nullable=True))
    _add_column_if_missing(job_columns, "started_at", sa.Column("started_at", sa.DateTime(), nullable=True))
    _add_column_if_missing(job_columns, "lease_owner", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    _add_column_if_missing(job_columns, "lease_expires_at", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    _add_column_if_missing(
        job_columns,
        "retry_count",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_if_missing(
        job_columns,
        "max_retries",
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_if_missing(
        job_columns,
        "idempotency_key",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("conversion_jobs")}
    if "ix_conversion_jobs_idempotency_key" not in indexes:
        op.create_index("ix_conversion_jobs_idempotency_key", "conversion_jobs", ["idempotency_key"])

    if "job_events" not in tables:
        op.create_table(
            "job_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["conversion_jobs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_job_events_created_at", "job_events", ["created_at"])
        op.create_index("ix_job_events_event_type", "job_events", ["event_type"])
        op.create_index("ix_job_events_job_id", "job_events", ["job_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "job_events" in tables:
        op.drop_index("ix_job_events_job_id", table_name="job_events")
        op.drop_index("ix_job_events_event_type", table_name="job_events")
        op.drop_index("ix_job_events_created_at", table_name="job_events")
        op.drop_table("job_events")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("conversion_jobs")}
    if "ix_conversion_jobs_idempotency_key" in indexes:
        op.drop_index("ix_conversion_jobs_idempotency_key", table_name="conversion_jobs")
    job_columns = {column["name"] for column in sa.inspect(bind).get_columns("conversion_jobs")}
    for column in (
        "idempotency_key",
        "max_retries",
        "retry_count",
        "lease_expires_at",
        "lease_owner",
        "started_at",
        "queued_at",
        "queue_backend",
    ):
        if column in job_columns:
            op.drop_column("conversion_jobs", column)


def _add_column_if_missing(existing: set[str], name: str, column: sa.Column) -> None:
    if name not in existing:
        op.add_column("conversion_jobs", column)
        existing.add(name)
