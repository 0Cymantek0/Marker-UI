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
    op.add_column("conversion_jobs", sa.Column("queue_backend", sa.String(length=50), nullable=True))
    op.add_column("conversion_jobs", sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.add_column("conversion_jobs", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("conversion_jobs", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    op.add_column("conversion_jobs", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("conversion_jobs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversion_jobs", sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversion_jobs", sa.Column("idempotency_key", sa.String(length=255), nullable=True))
    op.create_index("ix_conversion_jobs_idempotency_key", "conversion_jobs", ["idempotency_key"])

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
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_index("ix_job_events_event_type", table_name="job_events")
    op.drop_index("ix_job_events_created_at", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_conversion_jobs_idempotency_key", table_name="conversion_jobs")
    op.drop_column("conversion_jobs", "idempotency_key")
    op.drop_column("conversion_jobs", "max_retries")
    op.drop_column("conversion_jobs", "retry_count")
    op.drop_column("conversion_jobs", "lease_expires_at")
    op.drop_column("conversion_jobs", "lease_owner")
    op.drop_column("conversion_jobs", "started_at")
    op.drop_column("conversion_jobs", "queued_at")
    op.drop_column("conversion_jobs", "queue_backend")
