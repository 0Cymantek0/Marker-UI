"""Dialect-portable SQL primitives for the authoritative kernel (PR83A).

The Truth Kernel runs against two first-class database profiles: the
local SQLite profile and the industrial PostgreSQL profile. One semantic
kernel serves both, so every construct whose rendering or error
vocabulary is dialect-specific lives here behind helpers with identical
behavior on each supported backend:

* ``dialect_insert`` — INSERT with native ON CONFLICT support (rendered
  by both the SQLite and PostgreSQL dialects, which share the
  ``on_conflict_do_*`` API surface);
* ``is_retryable_contention`` — maps SQLite lock/busy messages and
  PostgreSQL serialization/deadlock/lock-timeout SQLSTATEs onto the one
  "retryable contention" answer the commit protocol already understands;
* ``integrity_constraint_name`` — extracts the violated constraint name
  from a PostgreSQL integrity error so typed kernel conflicts (duplicate
  record identity, concurrent manifest takeover) map identically to the
  text matching used for SQLite.

Anything not provably equivalent across profiles must stay out of the
authoritative commit path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table
from sqlalchemy.exc import IntegrityError, OperationalError

from app.kernel.errors import KernelError

__all__ = [
    "POSTGRESQL",
    "SQLITE",
    "SUPPORTED_BACKENDS",
    "UnsupportedBackendError",
    "backend_name",
    "dialect_insert",
    "integrity_constraint_name",
    "is_retryable_contention",
]

SQLITE = "sqlite"
POSTGRESQL = "postgresql"
SUPPORTED_BACKENDS = (SQLITE, POSTGRESQL)

#: SQLite lock/busy messages (aiosqlite surface them verbatim).
_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
)

#: PostgreSQL SQLSTATEs that mean "contended now, retry the transaction":
#: 40001 serialization_failure, 40P01 deadlock_detected,
#: 55P03 lock_not_available (lock timeout).
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "55P03"})


class UnsupportedBackendError(KernelError):
    """The bind's dialect has no kernel-parity implementation."""


def backend_name(bind: Any) -> str:
    """Dialect name of an engine/connection/session bind."""
    return bind.dialect.name


def dialect_insert(bind: Any, table: Table):
    """A dialect-native INSERT construct with ON CONFLICT support.

    Both supported dialects expose ``on_conflict_do_nothing`` /
    ``on_conflict_do_update`` with the same call signature, so the commit
    path writes one statement and each backend renders its own native
    upsert. Unsupported dialects fail closed instead of silently losing
    conflict semantics.
    """
    name = backend_name(bind)
    if name == POSTGRESQL:
        from sqlalchemy.dialects.postgresql import insert
    elif name == SQLITE:
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise UnsupportedBackendError(
            f"kernel commit path does not support backend {name!r}; "
            f"supported backends: {', '.join(SUPPORTED_BACKENDS)}"
        )
    return insert(table)


def _sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a wrapped driver error, across driver vocabularies.

    asyncpg exposes ``sqlstate``; the DBAPI convention is ``pgcode``;
    some drivers use ``code``. Only string values that look like a
    five-character SQLSTATE are honored.
    """
    candidate = exc
    for _ in range(3):  # sqlalchemy.exc.* -> driver orig -> cause chain
        for attr in ("sqlstate", "pgcode", "code"):
            value = getattr(candidate, attr, None)
            if (
                isinstance(value, str)
                and len(value) == 5
                and value.isdigit()
            ):
                return value
        candidate = getattr(candidate, "orig", None) or candidate.__cause__
        if candidate is None:
            break
    return None


def is_retryable_contention(exc: OperationalError) -> bool:
    """True when the error means transient contention, not corruption.

    SQLite reports lock contention through message text; PostgreSQL
    reports it through the SQLSTATE attached to the driver error. Both
    answers feed the same bounded retry budget in the commit protocol.
    """
    state = _sqlstate(exc)
    if state is not None:
        return state in _RETRYABLE_SQLSTATES
    text = str(exc).lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def integrity_constraint_name(exc: IntegrityError) -> str | None:
    """Constraint name violated by an integrity error, when available.

    PostgreSQL driver errors carry ``constraint_name`` natively; SQLite
    only embeds it in the message text, so callers keep a text fallback.
    """
    candidate: BaseException | None = exc
    for _ in range(3):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
        candidate = getattr(candidate, "orig", None) or candidate.__cause__
        if candidate is None:
            break
    return None
