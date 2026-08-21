"""Dialect-portable primitive contract tests (PR83B1 WS1).

The classification/retry/locking seam in ``app.kernel.dialects`` is the
one vocabulary every control-plane subsystem consumes. These tests pin:

* SQLSTATE extraction walks real SQLAlchemy/driver exception shapes and
  accepts the *alphanumeric* PostgreSQL codes (``40P01``/``55P03``) the
  declared retry vocabulary promises — the PR83A parser only accepted
  digit strings, which silently made deadlock/lock-timeout states
  unclassifiable;
* the shared contention budget retries only retryable errors, retries
  the *whole* operation, and exhausts into a typed failure;
* advisory lock keys are deterministic across processes and always fit
  a signed PostgreSQL bigint.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.kernel.dialects import (
    advisory_lock_key,
    is_retryable_contention,
    run_with_contention_retry,
    _sqlstate,
)
from app.kernel.errors import KernelBusyError

pytestmark = pytest.mark.asyncio


class _FakeDriverError(Exception):
    """Minimal stand-in carrying driver-vocabulary attributes."""


def _wrapped(*, nested: Exception) -> OperationalError:
    op = OperationalError("stmt", {}, nested)
    op.orig = nested
    return op


def test_sqlstate_reads_asyncpg_vocabulary() -> None:
    for code in ("40001", "40P01", "55P03", "23505", "22012"):
        driver = _FakeDriverError()
        driver.sqlstate = code
        assert _sqlstate(_wrapped(nested=driver)) == code


def test_sqlstate_reads_dbapi_pgcode_vocabulary() -> None:
    driver = _FakeDriverError()
    driver.pgcode = "40P01"
    assert _sqlstate(_wrapped(nested=driver)) == "40P01"


def test_sqlstate_walks_cause_chain() -> None:
    inner = _FakeDriverError()
    inner.sqlstate = "40001"
    outer = _FakeDriverError()
    outer.__cause__ = inner
    assert _sqlstate(_wrapped(nested=outer)) == "40001"


def test_sqlstate_rejects_non_sqlstate_strings() -> None:
    for bad in ("40P0", "400011", "hello", "40p01", ""):
        driver = _FakeDriverError()
        driver.sqlstate = bad
        assert _sqlstate(_wrapped(nested=driver)) is None


def test_retryable_vocabulary_matches_declared_sqlstates() -> None:
    for state in ("40001", "40P01", "55P03"):
        driver = _FakeDriverError()
        driver.sqlstate = state
        assert is_retryable_contention(_wrapped(nested=driver)), state
    for state in ("23505", "22012", "42601"):
        driver = _FakeDriverError()
        driver.sqlstate = state
        assert not is_retryable_contention(_wrapped(nested=driver)), state


def test_sqlite_busy_text_still_classifies() -> None:
    for message in (
        "database is locked",
        "database table is locked",
        "database is busy",
    ):
        op = OperationalError("stmt", {}, Exception(message))
        assert is_retryable_contention(op), message
    op = OperationalError("stmt", {}, Exception("no such table: x"))
    assert not is_retryable_contention(op)


def test_sqlstate_preferred_over_message_text() -> None:
    driver = _FakeDriverError()
    driver.sqlstate = "22012"
    op = _wrapped(nested=driver)
    op.orig = Exception("database is locked")  # text would lie
    assert not is_retryable_contention(op)


async def test_contention_budget_retries_whole_operation_then_converges() -> None:
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            driver = _FakeDriverError()
            driver.sqlstate = "40001"
            raise _wrapped(nested=driver)
        return "converged"

    result = await run_with_contention_retry(
        flaky, base_delay=0.001, operation_name="probe"
    )
    assert result == "converged"
    assert calls["n"] == 2


async def test_contention_budget_does_not_swallow_non_retryable() -> None:
    calls = {"n": 0}

    async def broken():
        calls["n"] += 1
        raise _wrapped(nested=_FakeDriverError())  # no sqlstate, no busy text

    with pytest.raises(OperationalError):
        await run_with_contention_retry(broken, operation_name="probe")
    assert calls["n"] == 1


async def test_contention_budget_exhaustion_is_typed_with_context() -> None:
    calls = {"n": 0}

    async def always_busy():
        calls["n"] += 1
        raise OperationalError("stmt", {}, Exception("database is locked"))

    with pytest.raises(KernelBusyError, match="probe-op still busy after 3"):
        await run_with_contention_retry(
            always_busy, attempts=3, base_delay=0.001, operation_name="probe-op"
        )
    assert calls["n"] == 3


def test_advisory_lock_key_is_deterministic_and_bigint_safe() -> None:
    a = advisory_lock_key("workspace-1", "work")
    b = advisory_lock_key("workspace-1", "work")
    c = advisory_lock_key("workspace-2", "work")
    assert a == b
    assert a != c
    for value in (a, c, advisory_lock_key("x"), advisory_lock_key("a", "b", "c")):
        assert -(2**63) <= value < 2**63


def test_advisory_lock_key_same_value_in_separate_processes() -> None:
    # Determinism must not depend on PYTHONHASHSEED; emulate by computing
    # in a fresh interpreter and comparing.
    import json
    import subprocess
    import sys

    code = (
        "from app.kernel.dialects import advisory_lock_key;"
        "import json;print(json.dumps(advisory_lock_key('ws-a','work')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd="."
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == advisory_lock_key("ws-a", "work")
