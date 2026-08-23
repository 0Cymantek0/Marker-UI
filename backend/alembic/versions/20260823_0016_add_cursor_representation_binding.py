"""bind query cursors to packet representation semantics (PR86)

Revision ID: 20260823_0016
Revises: 20260823_0015
Create Date: 2026-08-23

One nullable column records the deployed packet representation semantics
(packet schema, citation locator scheme, identity framing,
canonicalization) a continuation chain was created under. Resume
compares it against the currently deployed semantics and invalidates the
cursor explicitly before any page can be emitted under changed citation
or renderer semantics. Existing rows stay NULL by design: a chain that
predates the binding cannot be verified and fails closed on resume
rather than being silently reinterpreted. Short cursor TTLs bound the
operational cost of that one-time fail-closed transition.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260823_0016"
down_revision = "20260823_0015"
branch_labels = None
depends_on = None

_TABLE = "kernel_query_cursors"
_COLUMN = "representation_json"


def _existing_columns(bind) -> set[str]:
    return {
        row["name"] for row in sa.inspect(bind).get_columns(_TABLE)
    }


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _existing_columns(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _COLUMN in _existing_columns(bind):
        op.drop_column(_TABLE, _COLUMN)
