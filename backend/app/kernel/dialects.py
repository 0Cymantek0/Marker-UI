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
  text matching used for SQLite;
* ``run_with_contention_retry`` — the one bounded retry budget shared by
  every kernel subsystem (PR83B1); subsystems keep their public
  ``busy_retry_*`` knobs, the loop and vocabulary live here;
* ``advisory_xact_lock`` — transaction-scoped serialization primitive
  for invariants that SQLite gets from its single-writer model and
  PostgreSQL gets from ``pg_advisory_xact_lock`` (no-op on SQLite).

Anything not provably equivalent across profiles must stay out of the
authoritative kernel path.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy import Table, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.kernel.errors import KernelBusyError, KernelError

__all__ = [
    "POSTGRESQL",
    "SQLITE",
    "SUPPORTED_BACKENDS",
    "DEFAULT_CONTENTION_RETRY_ATTEMPTS",
    "DEFAULT_CONTENTION_RETRY_BASE_DELAY",
    "MAX_CONTENTION_RETRY_DELAY",
    "UnsupportedBackendError",
    "advisory_xact_lock",
    "backend_name",
    "dialect_insert",
    "integrity_constraint_name",
    "is_retryable_contention",
    "run_with_contention_retry",
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

#: Shared bounded-contention budget (PR83B1). One vocabulary and one
#: retry envelope for every kernel subsystem; exhaustion surfaces as
#: :class:`app.kernel.errors.KernelBusyError` with subsystem context.
DEFAULT_CONTENTION_RETRY_ATTEMPTS = 8
DEFAULT_CONTENTION_RETRY_BASE_DELAY = 0.02
MAX_CONTENTION_RETRY_DELAY = 0.5

_T = TypeVar("_T")


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
    some drivers use ``code``. PostgreSQL SQLSTATEs are five-character
    alphanumeric class/s subclass codes (``40001``, ``40P01``,
    ``55P03``, ...), so only strings of exactly that shape are honored.
    """
    candidate = exc
    for _ in range(3):  # sqlalchemy.exc.* -> driver orig -> cause chain
        for attr in ("sqlstate", "pgcode", "code"):
            value = getattr(candidate, attr, None)
            if (
                isinstance(value, str)
                and len(value) == 5
                and value.isalnum()
                and value == value.upper()
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


def _retry_delay(base: float, attempt: int) -> float:
    return min(base * (2**attempt), MAX_CONTENTION_RETRY_DELAY)


async def run_with_contention_retry(
    operation: Callable[[], Awaitable[_T]],
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
    operation_name: str = "kernel operation",
) -> _T:
    """Run one whole operation under the shared contention budget.

    The unit of retry is the complete operation (fresh session and
    transaction per attempt — callers pass a closure that opens its
    own transaction), matching PostgreSQL's rule that a serialization
    or deadlock abort invalidates the entire transaction. Contention is
    classified by :func:`is_retryable_contention`; every other error
    escapes immediately. Exhaustion raises
    :class:`app.kernel.errors.KernelBusyError` carrying the subsystem
    name and the last error for diagnosis.
    """
    budget = attempts or DEFAULT_CONTENTION_RETRY_ATTEMPTS
    delay = base_delay if base_delay else DEFAULT_CONTENTION_RETRY_BASE_DELAY
    last_error: OperationalError | None = None
    for _attempt in range(budget):
        try:
            return await operation()
        except OperationalError as exc:
            if not is_retryable_contention(exc):
                raise
            last_error = exc
            await asyncio.sleep(_retry_delay(delay, _attempt))
    raise KernelBusyError(
        f"{operation_name} still busy after {budget} attempts: {last_error}"
    )


def advisory_lock_key(*parts: str) -> int:
    """Stable signed-64-bit advisory-lock key for a namespaced scope.

    Deterministic across processes (unlike ``hash()``): each part is
    CRC-32'd and the parts are packed into one 64-bit value. Distinct
    scopes may collide only with probability ~2^-64 per pair, and a
    collision merely over-serializes two scopes — never under-serializes
    one — so correctness never depends on the mapping being injective.
    """
    import zlib

    key = 0
    for part in parts:
        key = (key << 32) | (zlib.crc32(part.encode("utf-8")) & 0xFFFFFFFF)
    key &= (1 << 64) - 1
    return key - (1 << 64) if key >= (1 << 63) else key


async def advisory_xact_lock(session: Any, *parts: str) -> None:
    """Serialize a scope's in-transaction writers on PostgreSQL.

    SQLite's single-writer model already serializes every write
    transaction, so this is a no-op there. On PostgreSQL it takes
    ``pg_advisory_xact_lock(key)`` — held until the surrounding
    transaction commits or rolls back, so caller-owned transactions
    (scheduler claims, liveness coupling) serialize on the same scope
    without any schema change and without session-scoped lock leaks.

    Callers must acquire the advisory lock *after* row locks to keep a
    single global lock ordering (row locks → advisory → event insert),
    which keeps advisory-mediated waiting acyclic.
    """
    if backend_name(session.bind) != POSTGRESQL:
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_lock_key(*parts)}
    )
