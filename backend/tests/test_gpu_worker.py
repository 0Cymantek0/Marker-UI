"""Tests for the GPU conversion worker (process-side counterpart)."""

import logging
import queue as _q
from unittest.mock import MagicMock, patch

from app.services import gpu_worker
from app.services.job_transport import JobEnvelope, WorkerEventType


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
    gpu_worker._capacity = None


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

    def test_multiformat_job_emits_formats_payload(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 2
        gpu_worker._device_str = "cuda:0"

        fake_marker_svc = MagicMock()
        fake_conversion_svc = MagicMock()
        fake_conversion_svc.supports_multiple_formats.return_value = True
        fake_conversion_svc.convert_file_formats.return_value = {
            "markdown": {"text": "# Hi", "extension": "md", "images": {}, "metadata": {}},
            "chunks": {
                "text": '{"schema_version":"marker.chunks.v1","chunks":[]}',
                "extension": "json",
                "images": {},
                "metadata": {"chunking": {"schema_version": "marker.chunks.v1"}},
            },
        }

        env = JobEnvelope(
            job_id="job-multi",
            filepath="/tmp/x.pdf",
            config={"output_format": "markdown", "output_formats": ["markdown", "chunks"]},
            device_str="cuda:0",
        )

        with patch("app.services.marker_service.MarkerService", return_value=fake_marker_svc), \
             patch("app.services.conversion_service.ConversionService", return_value=fake_conversion_svc):
            ret = gpu_worker.worker_run_job(env)

        assert ret == "job-multi"
        fake_conversion_svc.convert_file_formats.assert_called_once_with(
            "/tmp/x.pdf",
            {"output_format": "markdown", "output_formats": ["markdown", "chunks"]},
            ["markdown", "chunks"],
            device="cuda:0",
        )
        events = _drain(qq)
        result_ev = next(e for e in events if e.type == WorkerEventType.result)
        assert result_ev.payload["result"]["text"] == "# Hi"
        assert result_ev.payload["formats_payload"]["chunks"]["metadata"]["chunking"]["schema_version"] == "marker.chunks.v1"

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


class TestArtifactHandleEmission:
    """PR68A: large worker results travel by verified handle, small stay inline."""

    def _prepare(self, qq, monkeypatch, tmp_path, *, enabled=True, limit=256 * 1024):
        from app.core import config
        from app.services import artifact_handles

        _reset_globals()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 3
        gpu_worker._device_str = "cpu"
        monkeypatch.setattr(config, "ARTIFACT_HANDLES_ENABLED", enabled)
        monkeypatch.setattr(config, "ARTIFACT_HANDLE_INLINE_LIMIT", limit)
        monkeypatch.setattr(config, "ARTIFACT_HANDLE_ROOT", tmp_path / "handles")
        monkeypatch.setattr(artifact_handles, "_DEFAULT_STORE", None)
        return artifact_handles

    def test_large_result_emits_compact_handle_envelope(self, monkeypatch, tmp_path):
        import os

        qq = _q.Queue()
        ah = self._prepare(qq, monkeypatch, tmp_path)

        big_text = "markdown line\n" * 45_000  # ~675 KB utf-8
        big_image = os.urandom(400_000)
        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {
            "text": big_text,
            "extension": "md",
            "images": {"p1.png": big_image},
            "metadata": {"page_count": 2},
        }
        env = JobEnvelope(job_id="job-big", filepath="/tmp/x.pdf", config={"output_format": "markdown"})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)

        result_ev = next(e for e in _drain(qq) if e.type is WorkerEventType.result)
        wire = result_ev.payload
        assert ah.is_handle_envelope(wire)
        # The control message no longer embeds the large bytes.
        inline = wire[ah.HANDLE_WIRE_KEY]["inline"]
        assert "text" not in inline  # single-format payload: result dict is the root
        assert "p1.png" not in inline["images"]
        assert inline["metadata"].get("page_count") == 2

        # The parent-side resolution rebuilds the exact logical result.
        store = ah.default_store()
        rebuilt = ah.resolve_worker_payload(wire, store=store, job_id="job-big")
        assert rebuilt["text"] == big_text
        assert rebuilt["images"]["p1.png"] == big_image
        assert store.count_blobs() == 0

    def test_small_result_stays_inline_when_enabled(self, monkeypatch, tmp_path):
        qq = _q.Queue()
        self._prepare(qq, monkeypatch, tmp_path)

        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {"text": "# tiny", "extension": "md", "images": {}}
        env = JobEnvelope(job_id="job-small", filepath="/tmp/x.pdf", config={"output_format": "markdown"})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)

        result_ev = next(e for e in _drain(qq) if e.type is WorkerEventType.result)
        assert result_ev.payload["text"] == "# tiny"

    def test_staging_failure_degrades_to_inline_payload(self, monkeypatch, tmp_path):
        import os

        from app.services.artifact_handles import ArtifactHandleStore

        qq = _q.Queue()
        self._prepare(qq, monkeypatch, tmp_path)

        big_text = "x" * 600_000
        big_image = os.urandom(400_000)
        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {
            "text": big_text,
            "extension": "md",
            "images": {"p1.png": big_image},
        }
        env = JobEnvelope(job_id="job-fallback", filepath="/tmp/x.pdf", config={"output_format": "markdown"})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc), \
             patch.object(ArtifactHandleStore, "stage", side_effect=OSError("disk full")):
            gpu_worker.worker_run_job(env)

        result_ev = next(e for e in _drain(qq) if e.type is WorkerEventType.result)
        # Classic inline contract preserved: the job still completes truthfully.
        assert result_ev.payload["text"] == big_text
        assert result_ev.payload["images"]["p1.png"] == big_image

    def test_disabled_flag_keeps_pure_inline_transport(self, monkeypatch, tmp_path):
        qq = _q.Queue()
        self._prepare(qq, monkeypatch, tmp_path, enabled=False)

        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {"text": "y" * 600_000, "extension": "md", "images": {}}
        env = JobEnvelope(job_id="job-off", filepath="/tmp/x.pdf", config={"output_format": "markdown"})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)

        result_ev = next(e for e in _drain(qq) if e.type is WorkerEventType.result)
        assert result_ev.payload["text"] == "y" * 600_000
