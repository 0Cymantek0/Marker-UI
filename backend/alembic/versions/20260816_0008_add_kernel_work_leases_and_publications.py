"""add kernel work leases and publications tables (PR66)

Revision ID: 20260816_0008
Revises: 20260815_0007
Create Date: 2026-08-16

Creates the PR66 fenced work authority tables:

* kernel_work_leases — one row per outbox work item holding the current
  fenced ownership: a monotonically increasing fencing token, the current
  owner, a wall-clock lease expiry (takeover eligibility only), and the
  leased/released/accepted lifecycle state;
* kernel_publications — the exactly-once accepted result for one work
  identity, uniquely scoped by (workspace_id, work_id) so the database
  itself enforces "at most one accepted publication".

Both tables are new; no existing table is altered, so the upgrade is
convergent by construction (inspect-and-skip guards kept for symmetry
with the guarded chain). Downgrade drops fencing and accepted-result
truth: lease history and accepted publications are discarded
irreversibly and exist only for schema symmetry — never use on a
database whose work history must survive.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260816_0008"
down_revision = "20260815_0007"
branch_labels = None
depends_on = None

_PR66_TABLES = (
    "kernel_publications",
    "kernel_work_leases",
)

_PR66_INDEXES = (
    ("ix_kernel_work_leases_workspace_id", "kernel_work_leases", ["workspace_id"]),
    ("ix_kernel_publications_workspace_id", "kernel_publications", ["workspace_id"]),
)


def _existing_indexes(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "kernel_work_leases" not in existing:
        op.create_table(
            "kernel_work_leases",
            sa.Column("work_id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("work_kind", sa.String(length=64), nullable=False),
            sa.Column("fencing_token", sa.Integer(), nullable=False),
            sa.Column("owner_id", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("work_id"),
        )

    if "kernel_publications" not in existing:
        op.create_table(
            "kernel_publications",
            sa.Column("publication_id", sa.String(length=80), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("work_id", sa.Integer(), nullable=False),
            sa.Column("work_kind", sa.String(length=64), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("result_hash", sa.String(length=80), nullable=False),
            sa.Column("fencing_token", sa.Integer(), nullable=False),
            sa.Column("owner_id", sa.String(length=64), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("publication_id"),
            sa.UniqueConstraint(
                "workspace_id", "work_id", name="uq_kernel_publications_scope"
            ),
        )

    for name, table, columns in _PR66_INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    # WARNING: discards fencing and accepted-publication truth. Schema
    # symmetry only; never use on a live work database.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _PR66_TABLES:
        if table in existing:
            op.drop_table(table)
