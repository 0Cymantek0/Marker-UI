"""bind query cursors to an authenticated caller dimension (PR79B)

Revision ID: 20260819_0013
Revises: 20260818_0012
Create Date: 2026-08-19

Cursors issued on an authenticated transport record the trusted principal
binding next to the durable continuation state. NULL keeps pre-PR79B and
explicitly unbound (stdio/no-auth) cursors usable without weakening the
binding enforced for rows that carry it.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260819_0013"
down_revision = "20260818_0012"
branch_labels = None
depends_on = None

_TABLE = "kernel_query_cursors"
_COLUMN = "principal_id"


def _existing_columns(bind) -> set[str]:
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        return set()
    return {
        row["name"]
        for row in sa.inspect(bind).get_columns(_TABLE)
    }


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _existing_columns(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(length=128), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _COLUMN in _existing_columns(bind):
        op.drop_column(_TABLE, _COLUMN)
