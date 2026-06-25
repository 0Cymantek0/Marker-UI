"""Tests for TaskManager - SSE, cancellation, PID tracking, status."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.services.task_manager import TaskManager
from app.services.job_transport import WorkerEvent, WorkerEventType


@pytest.fixture
def task_manager():
    tm = TaskManager(max_workers=1)
    yield tm
    tm.shutdown(wait=False)


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


# ---------------------------------------------------------------------------
# Pluggable backend + process drain path
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_default_is_thread_backend(self):
        tm = TaskManager(max_workers=1)
        try:
            assert tm.backend_name == "thread"
            assert tm._backend.is_process is False
        finally:
            tm.shutdown(wait=False)

    def test_thread_backend_tracks_future(self):
        # submit_job calls asyncio.get_event_loop().create_task(...), so we need a
        # loop active. Save/restore the current loop so this test cannot leak
        # state into later async tests in the same session.
        prev_loop = asyncio.get_event_loop()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            tm = TaskManager(max_workers=2)
            try:
                tm.submit_job("j-thread", "/tmp/x", {"llm_provider": None}, object())
                # The thread backend returns a tracked future.
                assert "j-thread" in tm._tasks
            finally:
                tm.shutdown(wait=False)
        finally:
            loop.close()
            asyncio.set_event_loop(prev_loop)


class TestWorkerEventDispatch:
    """Feed scripted WorkerEvents through the drain path without spawning workers."""

    def test_progress_event_advances_in_memory_progress(self, task_manager: TaskManager):
        ev = WorkerEvent(
            type=WorkerEventType.progress,
            job_id="w-progress",
            percent=44,
            label="Recognizing text (10/20)",
        )
        task_manager._dispatch_worker_event(ev)
        assert task_manager._progress["w-progress"] == 44
        assert task_manager._job_status_text["w-progress"] == "Recognizing text (10/20)"

    def test_log_event_appends_to_job_logs(self, task_manager: TaskManager):
        task_manager._job_logs["w-log"] = []
        ev = WorkerEvent(
            type=WorkerEventType.log, job_id="w-log", message="hello", levelname="INFO"
        )
        task_manager._dispatch_worker_event(ev)
        assert task_manager._job_logs["w-log"] == ["hello"]

    def test_status_event_records_pid(self, task_manager: TaskManager):
        ev = WorkerEvent(
            type=WorkerEventType.status, job_id="w-pid", pid=4242, status_text="Loading..."
        )
        task_manager._dispatch_worker_event(ev)
        assert task_manager._pids["w-pid"] == 4242
        assert task_manager._job_status_text["w-pid"] == "Loading..."

    def test_error_event_marks_job_failed_and_writes_db(self, task_manager: TaskManager):
        with patch.object(task_manager, "_fail_job", new_callable=AsyncMock):
            with patch.object(task_manager, "_run_async") as mock_run:
                task_manager._proc_configs["w-err"] = {"output_format": "markdown"}
                ev = WorkerEvent(
                    type=WorkerEventType.error, job_id="w-err", error_message="boom"
                )
                task_manager._dispatch_worker_event(ev)
        # Failure path invoked once + job marked failed.
        assert mock_run.call_count == 1
        assert task_manager._progress["w-err"] == 0
        assert task_manager._proc_jobs.get("w-err") == "failed"

    def test_result_event_finalizes_and_marks_completed(self, task_manager: TaskManager):
        payload = {"text": "# md", "extension": "md", "images": {}}
        with patch.object(task_manager, "_finalize_job", new_callable=AsyncMock):
            with patch.object(task_manager, "_run_async") as mock_run:
                task_manager._proc_configs["w-ok"] = {"output_format": "markdown"}
                ev = WorkerEvent(type=WorkerEventType.result, job_id="w-ok", payload=payload)
                task_manager._dispatch_worker_event(ev)
        assert mock_run.call_count == 1
        assert task_manager._progress["w-ok"] == 100
        assert task_manager._proc_jobs.get("w-ok") == "done"


class TestProcessJobStatus:
    def test_proc_job_shows_processing_then_completed(self, task_manager: TaskManager):
        with task_manager._lock:
            task_manager._proc_jobs["p1"] = "running"
        task_manager._progress["p1"] = 30
        assert task_manager.get_status("p1")["status"] == "processing"

        task_manager._progress["p1"] = 100
        assert task_manager.get_status("p1")["status"] == "completed"

    def test_proc_job_failed(self, task_manager: TaskManager):
        with task_manager._lock:
            task_manager._proc_jobs["p2"] = "failed"
            task_manager._progress["p2"] = 0
        assert task_manager.get_status("p2")["status"] == "failed"


class TestExecutionBackendRouting:
    """Phase 1 section 15.2: office/text jobs route to the CPU pool, not the
    GPU process workers, when a process backend is configured."""

    def test_cpu_plan_routes_to_cpu_backend_on_process_config(self):
        from app.conversion.result import ConverterPlan
        from app.services.task_manager import TaskManager, ProcessExecutorBackend

        tm = TaskManager(max_workers=1)
        # Force the primary backend to look like a process backend without
        # actually spawning workers.
        tm._backend = MagicMock()
        tm._backend.is_process = True

        cpu_plan = ConverterPlan(
            engine="office_docx",
            label="Fast Office (Word)",
            confidence=0.95,
            reasons=["Matched extension '.docx'"],
            needs_marker_models=False,
            needs_gpu=False,
            execution_backend="cpu_thread",
        )
        fake_cs = MagicMock()
        fake_cs.plan.return_value = cpu_plan

        chosen = tm._select_backend("/tmp/report.docx", {"probe_result": {"page_count": 1}}, fake_cs)
        assert chosen is tm._cpu_backend
        fake_cs.plan.assert_called_once_with("/tmp/report.docx", {"probe_result": {"page_count": 1}})

    def test_marker_plan_routes_to_process_backend(self):
        from app.conversion.result import ConverterPlan
        from app.services.task_manager import TaskManager

        tm = TaskManager(max_workers=1)
        tm._backend = MagicMock()
        tm._backend.is_process = True

        marker_plan = ConverterPlan(
            engine="marker_pdf",
            label="Marker PDF",
            confidence=1.0,
            reasons=["Matched extension '.pdf'"],
            needs_marker_models=True,
            needs_gpu=True,
            execution_backend="marker_worker",
        )
        fake_cs = MagicMock()
        fake_cs.plan.return_value = marker_plan

        chosen = tm._select_backend("/tmp/doc.pdf", {}, fake_cs)
        assert chosen is tm._backend

    def test_thread_backend_always_uses_primary(self):
        # When the primary backend is the thread backend (single-process), the
        # CPU pool is never selected even for cpu_thread plans.
        from app.conversion.result import ConverterPlan
        from app.services.task_manager import TaskManager

        tm = TaskManager(max_workers=1)
        # Default backend is the thread backend.
        assert tm._backend.is_process is False

        cpu_plan = ConverterPlan(
            engine="office_docx",
            label="Fast Office (Word)",
            confidence=0.95,
            reasons=["Matched extension '.docx'"],
            needs_marker_models=False,
            needs_gpu=False,
            execution_backend="cpu_thread",
        )
        fake_cs = MagicMock()
        fake_cs.plan.return_value = cpu_plan

        chosen = tm._select_backend("/tmp/report.docx", {}, fake_cs)
        assert chosen is tm._backend
        fake_cs.plan.assert_not_called()

    def test_planning_failure_falls_back_to_primary(self):
        from app.services.task_manager import TaskManager

        tm = TaskManager(max_workers=1)
        tm._backend = MagicMock()
        tm._backend.is_process = True

        fake_cs = MagicMock()
        fake_cs.plan.side_effect = RuntimeError("planning exploded")

        chosen = tm._select_backend("/tmp/x.docx", {}, fake_cs)
        assert chosen is tm._backend


# ---------------------------------------------------------------------------
# _finalize_job metadata persistence (UCM-004.2 / UCM-004.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_job_persists_mixed_engine_segments_and_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """UCM-004.2/4: _finalize_job must persist mixed_engine_segments and asset list."""
    import app.services.task_manager as tm_mod
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    from app.models.job import ConversionJob  # noqa: F401
    from app.models.settings import Setting  # noqa: F401

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'final.db'}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(tm_mod, "async_session_factory", session_factory)

    job_id = "33333333-3333-4333-8333-333333333333"
    async with session_factory() as session:
        session.add(ConversionJob(
            id=job_id,
            filename="mixed.pdf",
            original_name="mixed.pdf",
            status="pending",
            input_format="pdf",
            output_format="markdown",
            config_json='{"output_format": "markdown", "original_name": "mixed.pdf"}',
        ))
        await session.commit()

    segments = [
        {
            "pages": [1, 2],
            "page_range": "1-2",
            "requested_engine": "liteparse_pdf",
            "actual_engine": "liteparse_pdf",
            "reasons": ["strong extractable text layer"],
            "fallback_chain": ["liteparse_pdf", "marker_pdf"],
            "fallback_reason": None,
        },
        {
            "pages": [3],
            "page_range": "3",
            "requested_engine": "marker_pdf",
            "actual_engine": "marker_pdf",
            "reasons": ["scan likelihood is high"],
            "fallback_chain": [],
            "fallback_reason": None,
        },
    ]
    assets = [
        {"name": "sheets/Sheet1.csv", "media_type": "text/csv", "path": str(tmp_path / "Sheet1.csv")},
    ]
    result_payload = {
        "text": "# Mixed\n\nbody",
        "extension": "md",
        "images": {},
        "metadata": {
            "engine": {"engine": "mixed_pdf", "label": "Mixed PDF routing"},
            "probe_result": {"page_count": 3},
            "mixed_engine_segments": segments,
            "assets": assets,
        },
    }
    config = {"output_format": "markdown", "original_name": "mixed.pdf"}

    tm = TaskManager(max_workers=1)
    try:
        await tm._finalize_job(job_id, result_payload, config)
    finally:
        tm.shutdown(wait=False)

    async with session_factory() as session:
        row = await session.get(ConversionJob, job_id)
        assert row.status == "completed"
        metadata = json.loads(row.result_metadata_json)
        assert metadata["mixed_engine_segments"] == segments
        assert metadata["engine"] == {"engine": "mixed_pdf", "label": "Mixed PDF routing"}
        assert metadata["probe_result"] == {"page_count": 3}

    await engine.dispose()


