"""add kernel view heads table (PR73)

Revision ID: 20260817_0010
Revises: 20260816_0009
Create Date: 2026-08-17

Creates the PR73 current-view-revision pointer:

* kernel_view_heads — one row per (workspace, view) naming the current
  immutable view revision and the kernel commit that produced it. The
  row is written ONLY inside kernel commit transactions (conditional
  update under the writer lock), so view-head ordering is subordinate
  to kernel_commit_id ordering by construction — there is no second
  independent "current document truth" store.

The table is new; no existing table is altered, so the upgrade is
convergent by construction (inspect-and-skip guard kept for symmetry
with the guarded chain). New rows arrive empty: no historical view
revision truth is fabricated for pre-PR73 commits; a workspace that
never initialized a view simply has no head row. Downgrade drops view
head state — schema symmetry only, never use on a database whose patch
lineage must stay resolvable to a current revision.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260817_0010"
down_revision = "20260816_0009"
branch_labels = None
depends_on = None

_PR73_TABLES = ("kernel_view_heads",)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "kernel_view_heads" not in existing:
        op.create_table(
            "kernel_view_heads",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("view_id", sa.String(length=64), nullable=False),
            sa.Column("current_revision_id", sa.String(length=80), nullable=False),
            sa.Column("kernel_commit_id", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id", "view_id"),
        )


def downgrade() -> None:
    # WARNING: discards current-view-revision truth. Schema symmetry only;
    # never use on a live database whose view lineage must survive.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _PR73_TABLES:
        if table in existing:
            op.drop_table(table)
