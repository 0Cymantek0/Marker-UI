"""add audit events table

Revision ID: 20260626_0001
Revises:
Create Date: 2026-06-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260626_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "conversion_jobs" not in existing:
        op.create_table(
            "conversion_jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("filename", sa.String(length=512), nullable=False),
            sa.Column("original_name", sa.String(length=512), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("input_format", sa.String(length=20), nullable=False),
            sa.Column("output_format", sa.String(length=20), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=True),
            sa.Column("result_text", sa.Text(), nullable=True),
            sa.Column("result_path", sa.String(length=1024), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("progress", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if "settings" not in existing:
        op.create_table(
            "settings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=100), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_settings_key", "settings", ["key"], unique=True)
    if "audit_events" not in existing:
        op.create_table(
            "audit_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("actor", sa.String(length=255), nullable=True),
            sa.Column("surface", sa.String(length=50), nullable=True),
            sa.Column("resource_type", sa.String(length=100), nullable=True),
            sa.Column("resource_id", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("redacted_payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
        op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "audit_events" in existing:
        op.drop_index("ix_audit_events_event_type", table_name="audit_events")
        op.drop_index("ix_audit_events_created_at", table_name="audit_events")
        op.drop_table("audit_events")
    if "settings" in existing:
        op.drop_index("ix_settings_key", table_name="settings")
        op.drop_table("settings")
    if "conversion_jobs" in existing:
        op.drop_table("conversion_jobs")
