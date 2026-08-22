"""add answer-evidence boundary tables (PR85)

Revision ID: 20260823_0015
Revises: 20260823_0014
Create Date: 2026-08-23

Three durable concepts keep retrieval provenance, answer-time disclosed
context, and post-answer support judgment separate (masterplan §9C.11):

* ``kernel_context_disclosures`` — one immutable row per delivered
  EvidencePacket page, carrying the canonical packet JSON (ordered
  evidence, publication, authorization view, budget, status) so
  answer-time truth is preserved, never recomputed;
* ``kernel_answer_traces`` + ``kernel_answer_trace_disclosures`` — one
  immutable answer binding over an ordered disclosure set, idempotent
  per (workspace, answer_ref), with composite tenant foreign keys that
  make cross-workspace references structurally unrepresentable;
* ``kernel_answer_support_assessments`` — append-only support judgments
  with their own provenance; they reference the trace and never mutate
  the answer.

The tables start empty: no historical disclosure, trace, or assessment
can be fabricated.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_0015"
down_revision = "20260823_0014"
branch_labels = None
depends_on = None

_DISCLOSURES = "kernel_context_disclosures"
_TRACES = "kernel_answer_traces"
_LINKS = "kernel_answer_trace_disclosures"
_ASSESSMENTS = "kernel_answer_support_assessments"

_INDEXES = (
    ("ix_kernel_context_disclosures_workspace_id", _DISCLOSURES, ("workspace_id",)),
    ("ix_kernel_answer_traces_workspace_id", _TRACES, ("workspace_id",)),
    ("ix_kernel_answer_trace_disclosures_disclosure", _LINKS, ("disclosure_id",)),
    (
        "ix_kernel_answer_assessments_workspace_trace",
        _ASSESSMENTS,
        ("workspace_id", "trace_id"),
    ),
)


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _existing_indexes(bind, table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)

    if _DISCLOSURES not in existing:
        op.create_table(
            _DISCLOSURES,
            sa.Column("disclosure_id", sa.String(length=128), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("principal_id", sa.String(length=128), nullable=True),
            sa.Column("packet_id", sa.String(length=128), nullable=False),
            sa.Column("packet_json", sa.Text(), nullable=False),
            sa.Column("delivery_status", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("disclosure_id"),
            sa.UniqueConstraint(
                "workspace_id",
                "disclosure_id",
                name="uq_kernel_context_disclosures_tenant",
            ),
        )

    if _TRACES not in existing:
        op.create_table(
            _TRACES,
            sa.Column("trace_id", sa.String(length=128), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("principal_id", sa.String(length=128), nullable=True),
            sa.Column("answer_ref", sa.String(length=256), nullable=False),
            sa.Column("answer_digest", sa.String(length=128), nullable=False),
            sa.Column("answer_content", sa.Text(), nullable=False),
            sa.Column("context_fingerprint", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("trace_id"),
            sa.UniqueConstraint(
                "workspace_id", "answer_ref", name="uq_kernel_answer_traces_ref"
            ),
            sa.UniqueConstraint(
                "workspace_id", "trace_id", name="uq_kernel_answer_traces_tenant"
            ),
        )

    if _LINKS not in existing:
        op.create_table(
            _LINKS,
            sa.Column("trace_id", sa.String(length=128), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("disclosure_id", sa.String(length=128), nullable=False),
            sa.PrimaryKeyConstraint("trace_id", "position"),
            sa.ForeignKeyConstraint(
                ["workspace_id", "trace_id"],
                ["kernel_answer_traces.workspace_id", "kernel_answer_traces.trace_id"],
                name="fk_kernel_answer_trace_disclosures_trace",
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id", "disclosure_id"],
                [
                    "kernel_context_disclosures.workspace_id",
                    "kernel_context_disclosures.disclosure_id",
                ],
                name="fk_kernel_answer_trace_disclosures_disclosure",
            ),
        )

    if _ASSESSMENTS not in existing:
        op.create_table(
            _ASSESSMENTS,
            sa.Column("assessment_id", sa.String(length=128), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("trace_id", sa.String(length=128), nullable=False),
            sa.Column("assessment_key", sa.String(length=256), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("verdict", sa.String(length=16), nullable=False),
            sa.Column("payload_digest", sa.String(length=128), nullable=False),
            sa.Column("claims_json", sa.Text(), nullable=False),
            sa.Column("assessor_kind", sa.String(length=16), nullable=False),
            sa.Column("assessor_id", sa.String(length=256), nullable=False),
            sa.Column("procedure", sa.String(length=256), nullable=False),
            sa.Column("procedure_version", sa.String(length=64), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("assessment_id"),
            sa.UniqueConstraint(
                "trace_id", "assessment_key", name="uq_kernel_answer_assessments_key"
            ),
            sa.UniqueConstraint(
                "trace_id", "seq", name="uq_kernel_answer_assessments_seq"
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id", "trace_id"],
                ["kernel_answer_traces.workspace_id", "kernel_answer_traces.trace_id"],
                name="fk_kernel_answer_assessments_trace",
            ),
        )

    for name, table, columns in _INDEXES:
        if table in _existing_tables(bind) and name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables(bind)
    for table in (_ASSESSMENTS, _LINKS, _TRACES, _DISCLOSURES):
        if table in existing:
            op.drop_table(table)
