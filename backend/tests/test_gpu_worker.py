"""Tests for the GPU conversion worker (process-side counterpart)."""

import logging
import queue as _q
from unittest.mock import MagicMock, patch

from app.services import gpu_worker
from app.services.job_transport import JobEnvelope, WorkerEvent, WorkerEventType


def _drain(qq):
    out = []
    while True:
        try:
            out.append(qq.get_nowait())
        except _q.Empty:
            break
    return out


def _reset_globals():
    gpu_worker._event_queue = None
    gpu_worker._device_str = "cpu"
    gpu_worker._worker_id = -1
    gpu_worker._model_dict = None
    gpu_worker._current_job_id = None


class TestInitializer:
    def test_lazy_default_does_not_load_models_at_init(self):
        # Phase 1 lazy init: by default models are NOT loaded at pool spawn.
        # The initializer wires monkeypatch/secrets/progress but skips the
        # expensive model load; worker_run_job loads lazily on first marker job.
        _reset_globals()
        qq = _q.Queue()
        snapshot = {"secrets_cache": {"k": "v"}}

        fake_svc = MagicMock()
        fake_svc._model_dict = {"layout_model": "fake"}

        with patch("app.core.api_manager.setup_api_manager_monkeypatch") as mock_patch, \
             patch("app.core.api_manager.seed_secrets_snapshot") as mock_seed, \
             patch("app.services.progress_tracker.set_reporter") as mock_set_reporter, \
             patch("app.services.progress_tracker.install") as mock_install, \
             patch("app.services.marker_service.MarkerService", return_value=fake_svc):

            gpu_worker.worker_initializer("cuda:1", 1, qq, snapshot)

        mock_patch.assert_called_once()
        mock_seed.assert_called_once_with(snapshot)
        mock_set_reporter.assert_called_once()
        mock_install.assert_called_once()
        # Lazy: model load is deferred, so initialize() is NOT called at spawn.
        fake_svc.initialize.assert_not_called()
        assert gpu_worker._model_dict is None
        assert gpu_worker._device_str == "cuda:1"
        assert gpu_worker._worker_id == 1

    def test_eager_load_when_preload_enabled(self):
        # When MARKER_PRELOAD_MODELS=true the initializer eagerly loads models
        # onto the pinned device (the original warm-at-spawn behavior).
        _reset_globals()
        qq = _q.Queue()
        fake_svc = MagicMock()
        fake_svc._model_dict = {"layout_model": "fake"}

        with patch("app.core.api_manager.setup_api_manager_monkeypatch"), \
             patch("app.core.api_manager.seed_secrets_snapshot"), \
             patch("app.services.progress_tracker.set_reporter"), \
             patch("app.services.progress_tracker.install"), \
             patch("app.services.marker_service.MarkerService", return_value=fake_svc), \
             patch("app.core.config.PRELOAD_MARKER_MODELS", True):

            gpu_worker.worker_initializer("cuda:1", 1, qq, {})

        fake_svc.initialize.assert_called_once_with(device="cuda:1")
        assert gpu_worker._model_dict == {"layout_model": "fake"}

    def test_cpu_device_passes_none_to_initialize_when_eager(self):
        _reset_globals()
        qq = _q.Queue()
        fake_svc = MagicMock()
        fake_svc._model_dict = {}

        with patch("app.core.api_manager.setup_api_manager_monkeypatch"), \
             patch("app.core.api_manager.seed_secrets_snapshot"), \
             patch("app.services.progress_tracker.set_reporter"), \
             patch("app.services.progress_tracker.install"), \
             patch("app.services.marker_service.MarkerService", return_value=fake_svc), \
             patch("app.core.config.PRELOAD_MARKER_MODELS", True):

            gpu_worker.worker_initializer("cpu", 0, qq, {})

        fake_svc.initialize.assert_called_once_with(device=None)


class TestRunJob:
    def test_success_emits_status_then_result(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 2
        gpu_worker._device_str = "cuda:0"
        gpu_worker._model_dict = {"layout_model": "fake"}

        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {"text": "# Hi", "extension": "md", "images": {}}

        env = JobEnvelope(job_id="job-1", filepath="/tmp/x.pdf", config={"output_format": "markdown"}, device_str="cuda:0")

        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            ret = gpu_worker.worker_run_job(env)

        assert ret == "job-1"
        # Preloaded model dict reused, not reloaded.
        assert fake_svc._model_dict == {"layout_model": "fake"}
        assert fake_svc._initialized is True
        fake_svc.convert_file.assert_called_once_with("/tmp/x.pdf", {"output_format": "markdown"}, device="cuda:0")

        events = _drain(qq)
        kinds = [e.type for e in events]
        assert WorkerEventType.status in kinds
        assert WorkerEventType.result in kinds

        status_ev = next(e for e in events if e.type == WorkerEventType.status)
        assert status_ev.pid is not None  # PID reported for cancellation
        result_ev = next(e for e in events if e.type == WorkerEventType.result)
        assert result_ev.payload["text"] == "# Hi"
        assert result_ev.job_id == "job-1"
        # _current_job_id cleared after the job.
        assert gpu_worker._current_job_id is None

    def test_failure_emits_error_event(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 0
        gpu_worker._model_dict = {}

        fake_svc = MagicMock()
        fake_svc.convert_file.side_effect = RuntimeError("boom")

        env = JobEnvelope(job_id="job-err", filepath="/tmp/x.pdf", config={})

        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            ret = gpu_worker.worker_run_job(env)

        assert ret is None
        events = _drain(qq)
        err = next(e for e in events if e.type == WorkerEventType.error)
        assert err.error_message == "boom"
        assert err.job_id == "job-err"
        assert gpu_worker._current_job_id is None

    def test_docx_routed_to_office_docx(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 1
        gpu_worker._device_str = "cuda:0"

        fake_svc = MagicMock()
        env = JobEnvelope(job_id="job-docx", filepath="report.docx", config={})

        with patch("app.services.marker_service.MarkerService", return_value=fake_svc), \
             patch("app.conversion.converters.office_docx.OfficeDocxConverter.convert") as mock_docx_convert:
            
            from app.conversion.result import UniversalConversionResult
            mock_docx_convert.return_value = UniversalConversionResult(text="# Word Content", extension="md")
            
            ret = gpu_worker.worker_run_job(env)

        assert ret == "job-docx"
        mock_docx_convert.assert_called_once_with("report.docx", {}, device="cuda:0")
        
        events = _drain(qq)
        result_ev = next(e for e in events if e.type == WorkerEventType.result)
        assert result_ev.payload["text"] == "# Word Content"


class TestQueueLogHandler:
    def test_routes_record_to_queue_only_with_active_job(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 3

        handler = gpu_worker._QueueLogHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        record = logging.LogRecord("marker", logging.INFO, __file__, 1, "hello", None, None)

        # No active job -> nothing emitted.
        handler.emit(record)
        assert _drain(qq) == []

        # Active job -> one log event emitted.
        gpu_worker._current_job_id = "job-9"
        handler.emit(record)
        events = _drain(qq)
        assert len(events) == 1
        assert events[0].type == WorkerEventType.log
        assert "hello" in events[0].message
        assert events[0].job_id == "job-9"
