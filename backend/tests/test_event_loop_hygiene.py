"""Session event-loop hygiene regressions (PR80A Track 0).

Root cause these tests pin: a main-thread ``asyncio.run`` (Alembic's
sync migration entry in ``alembic/env.py`` uses one, and the CLI error
tests drive it in-process on first DB creation) leaves the thread's
current event loop unset. pytest-asyncio 0.24 runs async TESTS on the
current loop but async FIXTURES on the session-scoped ``event_loop``
fixture — so once "current" stops being the session loop, fixtures and
tests land on DIFFERENT loops and every background task a fixture
created (kernel dispatch coordinators, executors) sits parked while
the test body runs elsewhere. That was the suite-order-dependent
source-ingress timeout cluster.

``conftest.ensure_current_event_loop`` now repairs by rebinding the
session loop as current. These tests fail if that repair ever mints a
fresh loop again or stops running.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio


async def _noop() -> None:
    return None


def test_main_thread_asyncio_run_clears_current_loop():
    """Document the hazard: ``asyncio.run`` unsets the current loop.

    Runs FIRST (file order) so the later async tests exercise the
    repair path against a genuinely broken current-loop state, exactly
    like a preceding CLI/migration test leaves behind.
    """
    asyncio.run(_noop())
    try:
        current = asyncio.get_event_loop()
    except RuntimeError:
        current = None
    assert current is None or current.is_closed() or not current.is_running()


def test_repeated_main_thread_breaks_keep_the_hazard_alive():
    """A second mid-session break (every CLI test re-triggers it)."""
    asyncio.run(_noop())
    try:
        asyncio.get_event_loop()
        broken = False
    except RuntimeError:
        broken = True
    assert broken


@pytest.mark.asyncio
async def test_current_loop_is_rebound_to_session_loop(event_loop):
    """After the breaks above, the autouse fixture must have rebound
    the session loop as current — never a freshly minted loop."""
    assert asyncio.get_event_loop() is event_loop


# ---------------------------------------------------------------------------
# Minimal dispatch reproduction: fixture-bound background work must run
# on the SAME loop the test body runs on.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def background_heartbeat():
    """A fixture-owned asyncio task, like a kernel dispatch loop."""
    loop = asyncio.get_running_loop()
    beat = asyncio.Event()
    task_done = asyncio.Event()

    async def _heartbeat() -> None:
        beat.set()
        await asyncio.sleep(0.05)
        task_done.set()

    task = loop.create_task(_heartbeat(), name="diag-heartbeat")
    yield beat, task_done, task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_fixture_owned_task_progresses_on_test_loop(background_heartbeat):
    """Pre-fix signature: the heartbeat task lived on the session loop
    while this test ran on a repaired loop — ``beat`` never set and the
    wait timed out. Post-fix both share the session loop."""
    beat, task_done, task = background_heartbeat
    assert task.get_loop() is asyncio.get_running_loop()
    await asyncio.wait_for(beat.wait(), timeout=5.0)
    await asyncio.wait_for(task_done.wait(), timeout=5.0)
