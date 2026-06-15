"""Tests for TaskManager - SSE, cancellation, PID tracking, status."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.services.task_manager import TaskManager


@pytest.fixture
def task_manager():
    tm = TaskManager(max_workers=1)
    yield tm
    tm._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_nonexistent_job_returns_pending(self, task_manager: TaskManager):
        status = task_manager.get_status("nonexistent")
        assert status["status"] == "pending"
        assert status["progress"] == 0
        assert status["job_id"] == "nonexistent"

    def test_active_future_shows_processing(self, task_manager: TaskManager):
        mock_future = MagicMock()
        mock_future.done.return_value = False
        task_manager._tasks["active-job"] = mock_future
        task_manager._progress["active-job"] = 42

        status = task_manager.get_status("active-job")
        assert status["status"] == "processing"
        assert status["progress"] == 42

    def test_completed_future_shows_completed(self, task_manager: TaskManager):
        mock_future = MagicMock()
        mock_future.done.return_value = True
        mock_future.exception.return_value = None
        task_manager._tasks["done-job"] = mock_future
        task_manager._progress["done-job"] = 100

        status = task_manager.get_status("done-job")
        assert status["status"] == "completed"
        assert status["progress"] == 100

    def test_failed_future_shows_failed(self, task_manager: TaskManager):
        mock_future = MagicMock()
        mock_future.done.return_value = True
        mock_future.exception.return_value = RuntimeError("boom")
        task_manager._tasks["fail-job"] = mock_future
        task_manager._progress["fail-job"] = 50

        status = task_manager.get_status("fail-job")
        assert status["status"] == "failed"
        assert status["progress"] == 50

    def test_no_future_progress_100_shows_completed(self, task_manager: TaskManager):
        task_manager._progress["fin"] = 100
        status = task_manager.get_status("fin")
        assert status["status"] == "completed"

    def test_get_status_message_fallback(self, task_manager: TaskManager):
        task_manager._progress["fallback-job"] = 75
        task_manager._job_status_text["fallback-job"] = "Starting conversion..."
        
        status = task_manager.get_status("fallback-job")
        assert status["message"] == "Extracting tables..."
        
        # Test custom loading status fallback
        task_manager._progress["fallback-job2"] = 35
        task_manager._job_status_text["fallback-job2"] = "Loading marker converters..."
        status2 = task_manager.get_status("fallback-job2")
        assert status2["message"] == "Detecting document layout..."



# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


class TestCancelJob:
    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self, task_manager: TaskManager):
        result = await task_manager.cancel_job("ghost")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_running_job_cleans_up(self, task_manager: TaskManager):
        mock_future = MagicMock()
        mock_future.done.return_value = False
        mock_future.cancel.return_value = True
        task_manager._tasks["cancel-me"] = mock_future
        task_manager._progress["cancel-me"] = 50
        task_manager._pids["cancel-me"] = 12345

        with patch.object(task_manager, "_update_job_status", new_callable=AsyncMock):
            with patch.object(task_manager, "_kill_pid") as mock_kill:
                result = await task_manager.cancel_job("cancel-me")

        assert result is True
        assert "cancel-me" not in task_manager._tasks
        assert "cancel-me" not in task_manager._progress
        assert "cancel-me" not in task_manager._pids
        mock_kill.assert_called_once_with(12345)

    @pytest.mark.asyncio
    async def test_cancel_already_done_returns_false(self, task_manager: TaskManager):
        mock_future = MagicMock()
        mock_future.done.return_value = True
        task_manager._tasks["already-done"] = mock_future
        task_manager._progress["already-done"] = 100

        result = await task_manager.cancel_job("already-done")
        assert result is False


# ---------------------------------------------------------------------------
# SSE event generator
# ---------------------------------------------------------------------------


class TestSSEEvents:
    @pytest.mark.asyncio
    async def test_sse_yields_completed_event(self, task_manager: TaskManager):
        task_manager._progress["sse-done"] = 100

        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        events = []
        async for event in task_manager.job_events(mock_request, "sse-done"):
            events.append(event)

        assert len(events) >= 1
        data = json.loads(events[0].data)
        assert data["status"] == "completed"
        assert data["progress"] == 100

    @pytest.mark.asyncio
    async def test_sse_detects_client_disconnect(self, task_manager: TaskManager):
        task_manager._progress["disconnect-job"] = 50
        mock_future = MagicMock()
        mock_future.done.return_value = False
        task_manager._tasks["disconnect-job"] = mock_future

        call_count = 0

        async def disconnect_after_first():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        mock_request = AsyncMock()
        mock_request.is_disconnected = disconnect_after_first

        events = []
        async for event in task_manager.job_events(mock_request, "disconnect-job"):
            events.append(event)

        assert call_count >= 2
        # Client disconnect should NOT remove the job or progress from task manager.
        assert "disconnect-job" in task_manager._tasks
        assert "disconnect-job" in task_manager._progress

    @pytest.mark.asyncio
    async def test_sse_stops_on_failed(self, task_manager: TaskManager):
        task_manager._progress["fail-job"] = 50
        mock_future = MagicMock()
        mock_future.done.return_value = True
        mock_future.exception.return_value = RuntimeError("boom")
        task_manager._tasks["fail-job"] = mock_future

        mock_request = AsyncMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        events = []
        async for event in task_manager.job_events(mock_request, "fail-job"):
            events.append(event)

        assert len(events) >= 1
        data = json.loads(events[0].data)
        assert data["status"] == "failed"


# ---------------------------------------------------------------------------
# _kill_pid
# ---------------------------------------------------------------------------


class TestKillPid:
    def test_kill_pid_handles_nonexistent_process(self):
        TaskManager._kill_pid(999999998)

    def test_kill_pid_is_static(self):
        assert isinstance(TaskManager.__dict__["_kill_pid"], staticmethod)


# ---------------------------------------------------------------------------
# report_stage_progress (tqdm tap sink)
# ---------------------------------------------------------------------------


class TestReportStageProgress:
    def test_advances_progress_and_sets_label(self, task_manager: TaskManager):
        task_manager._progress["job"] = 10
        task_manager.report_stage_progress("job", 55, "Recognizing text (40/101)")

        assert task_manager._progress["job"] == 55
        assert task_manager._job_status_text["job"] == "Recognizing text (40/101)"
        assert task_manager._job_has_real_progress["job"] is True

    def test_never_regresses(self, task_manager: TaskManager):
        task_manager._progress["job"] = 70
        task_manager.report_stage_progress("job", 40, "earlier stage")
        # Progress must not go backwards even if a stale/lower value arrives.
        assert task_manager._progress["job"] == 70

    def test_caps_at_96_leaving_room_for_finalization(self, task_manager: TaskManager):
        task_manager._progress["job"] = 10
        task_manager.report_stage_progress("job", 100, "done-ish")
        assert task_manager._progress["job"] == 96

    def test_real_progress_disables_log_string_override(self, task_manager: TaskManager):
        # Once a real tqdm value lands, coarse log parsing must not touch progress.
        task_manager._progress["job"] = 50
        task_manager._job_logs["job"] = []
        task_manager.report_stage_progress("job", 50, "Recognizing text (1/2)")

        task_manager.add_job_log("job", "Detecting layout now", "INFO")
        # Layout log would have forced progress to 30 in the fallback path; the
        # real-progress guard must prevent that regression.
        assert task_manager._progress["job"] == 50

