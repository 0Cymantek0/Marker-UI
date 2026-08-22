"""PR69 layers 3-4: worker integration + unexpected-OOM failure injection.

Layer 3 proves the admission lifecycle across the seam that actually owns
the pinned model runtime: the converter is NOT entered when admission
refuses, admitted jobs keep the ArtifactHandle result path unchanged, and
cold/warm/queue cost reaches the caller-visible event stream.

Layer 4 injects OOMs after admission: cleanup happens, the outcome is
truthful, profile feedback engages, later work still runs, and nothing
retries forever.
"""

from __future__ import annotations

import queue as _q
from unittest.mock import MagicMock, patch

import pytest

from app.services import gpu_worker
from app.services.job_transport import JobEnvelope, WorkerEventType
from app.services.runtime_capacity import AdmissionError, ResourceProfile


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


class _FakeCapacity:
    """Deterministic coordinator double for worker-seam tests."""

    def __init__(self, refuse: bool = False):
        self.refuse = refuse
        self.admitted: list[tuple[str, bool]] = []
        self.settled: list[tuple[str, str]] = []
        self.profile = ResourceProfile(
            family="marker-gpu",
            device_label="cuda:0",
            dtype_label="auto",
            batch_vector=(("recognition", 256),),
        )
        self.ledger = MagicMock()
        self.ledger.snapshot.return_value = {"usable_bytes": 1}

    def admit(self, job_id, filepath, ocr_enabled=True):
        if self.refuse:
            raise AdmissionError("capacity refused: demand exceeds available")
        from app.services.runtime_capacity import DemandClass, DemandEstimate

        estimate = DemandEstimate(
            demand_class=DemandClass.NORMAL,
            page_count=1,
            max_layout_slices_per_page=1,
            max_detection_chunks_per_page=1,
            max_recognition_crops_per_page=10,
            max_recognition_tokens_per_crop=703,
            peak_recognition_batch=10,
            envelope_bytes=1 << 20,
            profile_id=self.profile.fingerprint(),
        )
        self.admitted.append((job_id, ocr_enabled))
        return MagicMock(job_id=job_id, estimate=estimate, admitted_at=0.0, completed=False)

    def finish(self, ticket, outcome="success", detail=""):
        self.settled.append((ticket.job_id, outcome))

    def note_successful_execution(self):
        pass


def _runtime_events(events):
    return [e.runtime for e in events if e.type is WorkerEventType.runtime]


class TestAdmissionRefusalPreventsExecution:
    def test_converter_not_entered_when_admission_refuses(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 0
        gpu_worker._device_str = "cuda:0"
        gpu_worker._capacity = _FakeCapacity(refuse=True)

        convert_called = []
        fake_svc = MagicMock()
        fake_svc.convert_file.side_effect = lambda *a, **k: convert_called.append(a) or {
            "text": "should not happen",
            "extension": "md",
            "images": {},
        }

        env = JobEnvelope(job_id="job-refused", filepath="/tmp/x.pdf", config={})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc), \
             patch("app.services.conversion_service.ConversionService") as conv_cls:
            conv_cls.return_value.supports_multiple_formats.return_value = False
            ret = gpu_worker.worker_run_job(env)

        assert ret is None
        # THE property: the dangerous converter path was never entered.
        assert convert_called == []
        assert fake_svc.attach_capacity.call_count == 0

        events = _drain(qq)
        runtime = _runtime_events(events)
        assert any(r["phase"] == "admission_refused" and "capacity refused" in r["reason"] for r in runtime)
        err = next(e for e in events if e.type is WorkerEventType.error)
        assert "runtime admission refused" in err.error_message
        assert gpu_worker._current_job_id is None

    def test_admission_disabled_runs_legacy_path(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 0
        gpu_worker._capacity = None  # MARKER_ADMISSION=false

        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {"text": "# hi", "extension": "md", "images": {}}
        env = JobEnvelope(job_id="job-legacy", filepath="/tmp/x.pdf", config={})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            ret = gpu_worker.worker_run_job(env)

        assert ret == "job-legacy"
        result_ev = next(e for e in _drain(qq) if e.type is WorkerEventType.result)
        assert result_ev.payload["text"] == "# hi"


class TestAdmittedJobLifecycle:
    def test_admitted_success_settles_ticket_and_emits_observation(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 1
        gpu_worker._device_str = "cuda:0"
        capacity = _FakeCapacity()
        gpu_worker._capacity = capacity

        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {"text": "# ok", "extension": "md", "images": {}}
        env = JobEnvelope(job_id="job-ok", filepath="/tmp/x.pdf", config={})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            ret = gpu_worker.worker_run_job(env)

        assert ret == "job-ok"
        assert [job for job, _ in capacity.admitted] == ["job-ok"]
        assert capacity.settled == [("job-ok", "success")]
        # The service was bound to the coordinator for residency events.
        fake_svc.attach_capacity.assert_called_once()
        runtime = _runtime_events(_drain(qq))
        phases = [r["phase"] for r in runtime]
        assert "admitted" in phases
        assert "finished" in phases

    def test_residency_notifier_forwards_cold_load_to_parent(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 1
        gpu_worker._current_job_id = "job-cold"
        gpu_worker._residency_notifier("cold_load", 12.5)
        runtime = _runtime_events(_drain(qq))
        assert runtime == [
            {
                "phase": "residency",
                "device": "cpu",
                "worker_id": 1,
                "transition": "cold_load",
                "elapsed_seconds": 12.5,
            }
        ]

    def test_ocr_disabled_flag_reaches_admission(self):
        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        capacity = _FakeCapacity()
        gpu_worker._capacity = capacity
        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {"text": "x", "extension": "md", "images": {}}
        env = JobEnvelope(
            job_id="job-noocr", filepath="/tmp/x.pdf", config={"disable_ocr": True}
        )
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)
        # admit() received ocr_enabled=False for the marker config.
        assert capacity.admitted == [("job-noocr", False)]

    def test_artifact_handle_path_unchanged_for_admitted_job(self, monkeypatch, tmp_path):
        import os

        from app.core import config
        from app.services import artifact_handles

        _reset_globals()
        qq = _q.Queue()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 2
        gpu_worker._device_str = "cpu"
        gpu_worker._capacity = _FakeCapacity()
        monkeypatch.setattr(config, "ARTIFACT_HANDLES_ENABLED", True)
        monkeypatch.setattr(config, "ARTIFACT_HANDLE_INLINE_LIMIT", 256 * 1024)
        monkeypatch.setattr(config, "ARTIFACT_HANDLE_ROOT", tmp_path / "handles")
        monkeypatch.setattr(artifact_handles, "_DEFAULT_STORE", None)

        big_text = "warm admission path\n" * 45_000  # ~855 KB
        fake_svc = MagicMock()
        fake_svc.convert_file.return_value = {
            "text": big_text,
            "extension": "md",
            "images": {"p1.png": os.urandom(400_000)},
        }
        env = JobEnvelope(job_id="job-handle", filepath="/tmp/x.pdf", config={})
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)

        result_ev = next(e for e in _drain(qq) if e.type is WorkerEventType.result)
        wire = result_ev.payload
        assert artifact_handles.is_handle_envelope(wire)
        store = artifact_handles.default_store()
        rebuilt = artifact_handles.resolve_worker_payload(wire, store=store, job_id="job-handle")
        assert rebuilt["text"] == big_text
        assert store.count_blobs() == 0


class TestOomFailureInjection:
    def _prepare_oom_job(self, runtime_error: RuntimeError | None = None):
        qq = _q.Queue()
        _reset_globals()
        gpu_worker._event_queue = qq
        gpu_worker._worker_id = 0
        gpu_worker._device_str = "cuda:0"
        capacity = _FakeCapacity()
        gpu_worker._capacity = capacity

        fake_svc = MagicMock()
        error = runtime_error or RuntimeError("CUDA out of memory: Tried to allocate 2.00 GiB")
        fake_svc.convert_file.side_effect = error
        env = JobEnvelope(job_id="job-oom", filepath="/tmp/x.pdf", config={})
        return qq, capacity, fake_svc, env

    def test_oom_settles_as_oom_and_emits_containment_event(self):
        qq, capacity, fake_svc, env = self._prepare_oom_job()
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            ret = gpu_worker.worker_run_job(env)
        assert ret is None
        # Cleanup happened with the truthful OOM outcome, not "failed".
        assert capacity.settled == [("job-oom", "oom")]
        events = _drain(qq)
        runtime = _runtime_events(events)
        assert any(r["phase"] == "oom_contained" for r in runtime)
        err = next(e for e in events if e.type is WorkerEventType.error)
        assert "out of memory" in err.error_message

    def test_non_oom_failure_settles_as_failed(self):
        qq, capacity, fake_svc, env = self._prepare_oom_job(
            runtime_error=RuntimeError("ordinary converter bug")
        )
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)
        assert capacity.settled == [("job-oom", "failed")]

    def test_later_independent_work_still_runs_after_oom(self):
        qq, capacity, fake_svc, env = self._prepare_oom_job()
        with patch("app.services.marker_service.MarkerService", return_value=fake_svc):
            gpu_worker.worker_run_job(env)

        # An independent later job on the same runtime completes normally.
        ok_svc = MagicMock()
        ok_svc.convert_file.return_value = {"text": "# fine", "extension": "md", "images": {}}
        env2 = JobEnvelope(job_id="job-after", filepath="/tmp/y.pdf", config={})
        with patch("app.services.marker_service.MarkerService", return_value=ok_svc):
            ret = gpu_worker.worker_run_job(env2)
        assert ret == "job-after"
        assert capacity.settled[-1] == ("job-after", "success")


class TestMarkerServiceResidencyIntegration:
    def test_initialize_records_cold_load_through_notifier(self, monkeypatch):
        from app.services.marker_service import MarkerService

        svc = MarkerService()
        events = []
        svc._runtime_notifier = lambda transition, elapsed=0.0: events.append((transition, elapsed))
        capacity = MagicMock()
        svc._capacity = capacity

        monkeypatch.setattr(
            "app.services.gpu_service.GPUService.status_dict", {"status": "ready"}
        )
        with patch("marker.models.create_model_dict", return_value={"m": object()}), \
             patch("app.services.model_tracker.check_models_downloaded", return_value=True), \
             patch("app.services.marker_service._import_marker"), \
             patch("app.services.model_tracker.tracker"):
            svc.initialize(device=None)

        assert svc._initialized is True
        transitions = [t for t, _ in events]
        assert "loading" in transitions
        assert "cold_load" in transitions
        capacity.note_residency_states.assert_any_call(loading=True)
        capacity.observe_cold_load.assert_called_once()

    def test_second_initialize_is_warm_reuse(self, monkeypatch):
        from app.services.marker_service import MarkerService

        svc = MarkerService()
        events = []
        svc._runtime_notifier = lambda transition, elapsed=0.0: events.append((transition, elapsed))
        svc._capacity = MagicMock()
        monkeypatch.setattr(
            "app.services.gpu_service.GPUService.status_dict", {"status": "ready"}
        )
        with patch("marker.models.create_model_dict", return_value={"m": object()}), \
             patch("app.services.model_tracker.check_models_downloaded", return_value=True), \
             patch("app.services.marker_service._import_marker"), \
             patch("app.services.model_tracker.tracker"):
            svc.initialize(device=None)
        svc.initialize(device=None)
        assert events.count(("warm_reuse", 0.0)) == 1
        svc._capacity.observe_warm_reuse.assert_called_once()

    def test_release_models_drains_before_unloading(self):
        from app.services.marker_service import MarkerService

        svc = MarkerService()
        capacity = MagicMock()
        capacity.request_unload.return_value = True
        svc._capacity = capacity
        svc._volunteer_job = "j1"
        with patch("app.services.marker_service._empty_cuda_cache"):
            svc.release_models()
        capacity.request_unload.assert_called_once()
        assert capacity.request_unload.call_args.kwargs.get("volunteer_job") == "j1"
        assert svc._model_dict is None

    def test_release_models_refuses_when_borrowers_hold_generation(self):
        from app.services.marker_service import MarkerService

        svc = MarkerService()
        capacity = MagicMock()
        capacity.request_unload.return_value = False  # drain timeout
        svc._capacity = capacity
        svc._model_dict = {"m": object()}
        with pytest.raises(RuntimeError, match="anti-eviction"):
            svc.release_models()
        # The models were NOT dropped underneath the borrowers.
        assert svc._model_dict is not None

    def test_release_models_without_capacity_keeps_legacy_behavior(self):
        from app.services.marker_service import MarkerService

        svc = MarkerService()
        svc._model_dict = {"m": object()}
        with patch("app.services.marker_service._empty_cuda_cache"):
            svc.release_models()
        assert svc._model_dict is None


class TestOomRetryFeedback:
    def _model_dict(self):
        inner = MagicMock()
        inner.batch_size = 256
        outer = MagicMock()
        outer.batch_size = None
        outer.foundation_predictor = inner
        outer.get_batch_size.return_value = 8
        return {"recognition_model": outer, "layout_model": MagicMock(batch_size=32)}

    def test_run_with_oom_retry_invokes_feedback_after_halving(self):
        from app.services.marker_service import run_with_oom_retry

        ooms = []

        def convert():
            ooms.append(1)
            if len(ooms) < 3:
                raise RuntimeError("CUDA out of memory: boom")
            return "done"

        feedback = MagicMock()
        result = run_with_oom_retry(convert, self._model_dict(), oom_feedback=feedback)
        assert result == "done"
        assert len(ooms) == 3
        assert feedback.call_count == 2

    def test_feedback_exceptions_never_break_the_retry(self):
        from app.services.marker_service import run_with_oom_retry

        state = {"n": 0}

        def convert():
            state["n"] += 1
            if state["n"] < 2:
                raise RuntimeError("CUDA out of memory: boom")
            return "ok"

        def bad_feedback(attempt, exc):
            raise ValueError("feedback itself is broken")

        assert run_with_oom_retry(convert, self._model_dict(), oom_feedback=bad_feedback) == "ok"

    def test_batch_halving_changes_resolved_profile_vector(self):
        from app.services.marker_service import _halve_batch_sizes, _resolved_batch_vector

        model_dict = self._model_dict()
        before = _resolved_batch_vector(model_dict)
        assert _halve_batch_sizes(model_dict) is True
        after = _resolved_batch_vector(model_dict)
        assert before != after
        # Both the wrapper (resolved via get_batch_size) and the
        # memory-dominant foundation batch are part of the identity.
        assert dict(after)["recognition_model"] == 4
        assert dict(after)["recognition_model.foundation"] == 128

    def test_profile_transition_invalidates_old_identity(self):
        from app.services.marker_service import _resolved_batch_vector
        from app.services.runtime_capacity import runtime_versions

        model_dict = self._model_dict()
        profile = ResourceProfile(
            family="marker-gpu",
            device_label="cuda:0",
            dtype_label="auto",
            batch_vector=_resolved_batch_vector(model_dict),
            versions=runtime_versions(),
        )
        old = profile.fingerprint()
        from app.services.marker_service import _halve_batch_sizes

        _halve_batch_sizes(model_dict)
        new_profile = profile.with_batches(_resolved_batch_vector(model_dict))
        assert new_profile.fingerprint() != old


class TestParentRuntimeEventDispatch:
    def test_worker_runtime_event_updates_status_and_runtime_view(self):
        from app.services.job_transport import WorkerEvent
        from app.services.task_manager import TaskManager

        tm = TaskManager(max_workers=1)
        try:
            event = WorkerEvent(
                type=WorkerEventType.runtime,
                job_id="job-rt",
                worker_id=0,
                runtime={
                    "phase": "admission_refused",
                    "reason": "capacity refused: demand exceeds available",
                },
            )
            tm._dispatch_worker_event(event)
            assert "runtime admission refused" in tm._job_status_text["job-rt"].lower()
            status = tm.get_status("job-rt")
            assert status["runtime"]["phase"] == "admission_refused"
            assert "reason" in status["runtime"]
        finally:
            tm.shutdown(wait=False)

    def test_admitted_event_sets_envelope_status_text(self):
        from app.services.job_transport import WorkerEvent
        from app.services.task_manager import TaskManager

        tm = TaskManager(max_workers=1)
        try:
            event = WorkerEvent(
                type=WorkerEventType.runtime,
                job_id="job-adm",
                worker_id=0,
                runtime={
                    "phase": "admitted",
                    "demand": {"demand_class": "normal", "envelope_bytes": 104857600},
                },
            )
            tm._dispatch_worker_event(event)
            assert "Admitted by runtime capacity" in tm._job_status_text["job-adm"]
            assert "100 MiB" in tm._job_status_text["job-adm"]
        finally:
            tm.shutdown(wait=False)
