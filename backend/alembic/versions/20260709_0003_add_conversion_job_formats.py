"""add conversion job metadata and output formats cache

Revision ID: 20260709_0003
Revises: 20260626_0002
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260709_0003"
down_revision = "20260626_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("conversion_jobs")}
    if "result_metadata_json" not in existing:
        op.add_column("conversion_jobs", sa.Column("result_metadata_json", sa.Text(), nullable=True))
    if "formats_json" not in existing:
        op.add_column("conversion_jobs", sa.Column("formats_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("conversion_jobs")}
    if "formats_json" in existing:
        op.drop_column("conversion_jobs", "formats_json")
    if "result_metadata_json" in existing:
        op.drop_column("conversion_jobs", "result_metadata_json")
