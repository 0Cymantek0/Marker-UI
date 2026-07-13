"""Verify TaskManager in-memory dicts are evicted after job completion.

Covers the CACHE-1 audit finding: _job_logs, _job_status_text,
_job_start_time, _job_has_real_progress, _progress, and _job_providers
were never cleaned on job completion, leaking one entry per job for the
entire process lifetime.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.task_manager import TaskManager


@pytest.fixture
def tm() -> TaskManager:
    """Build a TaskManager without starting real backends."""
    with patch.object(TaskManager, "__init__", lambda self: None):
        manager = TaskManager()
    manager._tasks = {}
    manager._smooth_tasks = {}
    manager._progress = {}
    manager._pids = {}
    manager._job_logs = {}
    manager._job_status_text = {}
    manager._job_start_time = {}
    manager._job_has_real_progress = {}
    manager._job_started = {}
    manager._job_queued_message = {}
    manager._job_providers = {}
    manager._job_backends = {}
    manager._proc_jobs = {}
    manager._proc_configs = {}
    manager._cancel_requested = set()
    manager._lock = __import__("threading").Lock()
    return manager


def test_cleanup_job_memory_evicts_all_dicts(tm: TaskManager) -> None:
    """_cleanup_job_memory(delay=0) must pop every per-job dict."""
    job_id = "test-job-1"
    tm._progress[job_id] = 100
    tm._job_logs[job_id] = ["line1", "line2"]
    tm._job_status_text[job_id] = "done"
    tm._job_start_time[job_id] = 12345.0
    tm._job_has_real_progress[job_id] = True
    tm._job_providers[job_id] = "openai"

    tm._cleanup_job_memory(job_id, delay=0.0)

    assert job_id not in tm._progress
    assert job_id not in tm._job_logs
    assert job_id not in tm._job_status_text
    assert job_id not in tm._job_start_time
    assert job_id not in tm._job_has_real_progress
    assert job_id not in tm._job_providers


def test_cleanup_job_memory_missing_job_is_noop(tm: TaskManager) -> None:
    """Popping a non-existent job must not raise."""
    tm._cleanup_job_memory("nonexistent", delay=0.0)  # should not raise


def test_cleanup_job_memory_does_not_evict_other_jobs(tm: TaskManager) -> None:
    """Evicting one job must leave another job's entries intact."""
    job_a = "job-a"
    job_b = "job-b"
    for jid in (job_a, job_b):
        tm._progress[jid] = 50
        tm._job_logs[jid] = ["log"]
        tm._job_status_text[jid] = "running"
        tm._job_start_time[jid] = 1.0
        tm._job_has_real_progress[jid] = False
        tm._job_providers[jid] = None

    tm._cleanup_job_memory(job_a, delay=0.0)

    assert job_a not in tm._progress
    assert job_b in tm._progress
    assert job_b in tm._job_logs
