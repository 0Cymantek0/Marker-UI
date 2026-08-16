"""add kernel scheduling, liveness, and semantic event tables (PR67A)

Revision ID: 20260816_0009
Revises: 20260816_0008
Create Date: 2026-08-16

Creates the PR67A fair-scheduling / challenge-liveness / semantic-event
tables:

* kernel_scheduling_entries — per-work policy metadata (resource class,
  scheduling group, deadline) keyed 1:1 to the outbox row. The outbox
  remains the only work truth; entries never record ownership;
* kernel_scheduling_groups — per-(resource class, group) fair-share
  policy (weight, fan-out window, age boost) and non-authoritative
  served-count bookkeeping;
* kernel_liveness — rotating challenge nonce, monotonic progress
  high-water mark, active-request binding, topology generation, request
  deadline, and cancellation observation backing evidence-bearing lease
  renewal;
* kernel_events — append-only durable semantic events with the
  authoritative per-(workspace, stream) semantic sequence;
* kernel_progress — coalescible latest progress snapshot, one row per
  (workspace, work), updated in place.

All tables are new; no existing table is altered, so the upgrade is
convergent by construction (inspect-and-skip guards kept for symmetry
with the guarded chain). New tables arrive empty: no historical
scheduler, liveness, or event truth is fabricated for work that ran
under PR66 — replay-based consumers must treat pre-0009 history as
absent, not zero. Downgrade drops scheduler, liveness, and event truth
irreversibly — schema symmetry only, never use on a database whose
event history must survive.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260816_0009"
down_revision = "20260816_0008"
branch_labels = None
depends_on = None

_PR67A_TABLES = (
    "kernel_progress",
    "kernel_events",
    "kernel_liveness",
    "kernel_scheduling_groups",
    "kernel_scheduling_entries",
)

_PR67A_INDEXES = (
    (
        "ix_kernel_sched_entries_class_group",
        "kernel_scheduling_entries",
        ["resource_class", "group_id"],
    ),
)


def _existing_indexes(bind: sa.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "kernel_scheduling_entries" not in existing:
        op.create_table(
            "kernel_scheduling_entries",
            sa.Column("work_id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("resource_class", sa.String(length=32), nullable=False),
            sa.Column("group_id", sa.String(length=192), nullable=False),
            sa.Column("deadline_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("work_id"),
        )

    if "kernel_scheduling_groups" not in existing:
        op.create_table(
            "kernel_scheduling_groups",
            sa.Column("resource_class", sa.String(length=32), nullable=False),
            sa.Column("group_id", sa.String(length=192), nullable=False),
            sa.Column("weight", sa.Float(), nullable=False),
            sa.Column("max_in_flight", sa.Integer(), nullable=False),
            sa.Column("age_boost_after_seconds", sa.Float(), nullable=False),
            sa.Column("age_boost_factor", sa.Float(), nullable=False),
            sa.Column("served_count", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("resource_class", "group_id"),
        )

    if "kernel_liveness" not in existing:
        op.create_table(
            "kernel_liveness",
            sa.Column("work_id", sa.Integer(), nullable=False),
            sa.Column("challenge_nonce", sa.String(length=64), nullable=False),
            sa.Column("progress_high_water", sa.Integer(), nullable=False),
            sa.Column("active_request_id", sa.String(length=192), nullable=False),
            sa.Column("topology_generation", sa.Integer(), nullable=True),
            sa.Column("request_expires_at", sa.DateTime(), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(), nullable=True),
            sa.Column("renew_count", sa.Integer(), nullable=False),
            sa.Column("last_activity_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("work_id"),
        )

    if "kernel_events" not in existing:
        op.create_table(
            "kernel_events",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("stream", sa.String(length=64), nullable=False),
            sa.Column("semantic_sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("durability", sa.String(length=16), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id", "stream", "semantic_sequence"),
        )

    if "kernel_progress" not in existing:
        op.create_table(
            "kernel_progress",
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("work_id", sa.Integer(), nullable=False),
            sa.Column("counter", sa.Integer(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("workspace_id", "work_id"),
        )

    for name, table, columns in _PR67A_INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    # WARNING: discards scheduler, liveness, and durable event truth
    # (including the semantic sequence). Schema symmetry only; never use
    # on a live database whose event history must survive.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in _PR67A_TABLES:
        if table in existing:
            op.drop_table(table)
