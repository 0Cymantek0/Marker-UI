"""Conversion worker that runs inside a spawned process, pinned to one GPU.

A worker is the process-side counterpart of the parent ``TaskManager``. Because
each spawned process gets a *fresh* interpreter with none of the parent's
in-memory globals (the httpx monkeypatch, the secrets cache, the tqdm tap), the
initializer re-establishes all of that ONCE per process at pool spawn, then
loads the marker model dict onto its pinned device and keeps it resident for
every job that worker runs.

Everything the worker sends back to the parent goes through the shared
``multiprocessing.Queue`` as :class:`WorkerEvent`s — progress, logs, the worker
PID (for cancellation), and a final ``result`` or ``error``. The worker never
touches the database; the parent's drain thread owns all writes.

Module globals are intentional and correct here: a worker handles exactly one
job at a time, so ``_current_job_id`` is unambiguous, and the model dict / event
queue are per-process singletons.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from app.services.job_transport import JobEnvelope, WorkerEvent, WorkerEventType

logger = logging.getLogger(__name__)

# Per-process state, established by worker_initializer and read by worker_run_job.
_event_queue: Any | None = None
_device_str: str = "cpu"
_worker_id: int = -1
_model_dict: dict[str, Any] | None = None
_current_job_id: str | None = None
# PR69 runtime admission coordinator for THIS process's pinned device; None
# when admission is disabled via MARKER_ADMISSION=false.
_capacity: Any | None = None


def _requested_formats(config: dict[str, Any]) -> list[str]:
    raw = config.get("output_formats")
    if isinstance(raw, list) and raw:
        return [str(fmt).strip().lower() for fmt in raw if fmt]
    return [str(config.get("output_format") or "markdown").strip().lower()]


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def _emit(event: WorkerEvent) -> None:
    """Push one event toward the parent, swallowing transport errors.

    Progress/log emission must never crash a conversion, so a dead/closed queue
    is logged locally and ignored.
    """
    if _event_queue is None:
        return
    try:
        _event_queue.put(event)
    except Exception:  # noqa: BLE001 - best effort; a broken queue must not kill the job
        pass


def _emit_progress(percent: int, label: str) -> None:
    if _current_job_id is None:
        return
    _emit(
        WorkerEvent(
            type=WorkerEventType.progress,
            job_id=_current_job_id,
            worker_id=_worker_id,
            percent=percent,
            label=label,
        )
    )


def _emit_log(message: str, levelname: str) -> None:
    if _current_job_id is None:
        return
    _emit(
        WorkerEvent(
            type=WorkerEventType.log,
            job_id=_current_job_id,
            worker_id=_worker_id,
            message=message,
            levelname=levelname,
        )
    )


def _emit_runtime(job_id: str, phase: str, data: dict[str, Any] | None = None) -> None:
    """Push one structured runtime-capacity observation (PR69).

    Phases: admission_refused, admitted, residency (cold_load / warm_reuse /
    unload), oom_contained, finished. The payload is a plain dict so the
    parent can surface why work waited and what residency cost, without
    parsing logs.
    """
    runtime = {"phase": phase, "device": _device_str, "worker_id": _worker_id}
    if data:
        runtime.update(data)
    _emit(
        WorkerEvent(
            type=WorkerEventType.runtime,
            job_id=job_id,
            worker_id=_worker_id,
            runtime=runtime,
        )
    )


# ---------------------------------------------------------------------------
# Logging handler that routes log records to the event queue
# ---------------------------------------------------------------------------


class _QueueLogHandler(logging.Handler):
    """Mirror of the parent's ``JobLogHandler`` but emitting over the queue.

    In the parent, a log record is mapped to a job by thread id. In a worker
    the mapping is trivial: one job at a time, so any record emitted while a job
    is active belongs to that job.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _current_job_id is None:
                return
            _emit_log(self.format(record), record.levelname)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def _worker_progress_reporter(job_id: str, percent: int, label: str) -> None:
    """Sink handed to ``progress_tracker`` so tqdm taps become queue events."""
    _emit_progress(percent, label)


def _residency_notifier(transition: str, elapsed_seconds: float) -> None:
    """Forward a MarkerService residency transition to the parent (PR69)."""
    if _current_job_id is None:
        return
    _emit_runtime(
        _current_job_id,
        "residency",
        {"transition": transition, "elapsed_seconds": elapsed_seconds},
    )


def _stage_payload_handles(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Move eligible large result fields into verified file handles (PR68A).

    The seam is deliberately best-effort at the producer: any failure keeps
    the classic inline payload, so the data plane can never fail a
    conversion. The parent-side resolver is the strict half. Disable
    entirely with ``MARKER_ARTIFACT_HANDLES=false``.
    """
    try:
        from app.core.config import ARTIFACT_HANDLES_ENABLED, ARTIFACT_HANDLE_INLINE_LIMIT

        if not ARTIFACT_HANDLES_ENABLED:
            return payload
        from app.services.artifact_handles import default_store, stage_worker_payload

        return stage_worker_payload(
            payload,
            store=default_store(),
            job_id=job_id,
            inline_limit=ARTIFACT_HANDLE_INLINE_LIMIT,
        )
    except Exception:  # noqa: BLE001 - degrade to inline, never fail the job
        logger.exception(
            "worker %d: artifact handle staging failed for job %s; emitting inline payload",
            _worker_id,
            job_id,
        )
        return payload


# ---------------------------------------------------------------------------
# Initializer — runs once per process at pool spawn
# ---------------------------------------------------------------------------


def pool_initializer(
    counter: Any,
    lock: Any,
    detected_gpus: int,
    event_queue: Any,
    secrets_snapshot: dict[str, Any],
) -> None:
    """``mp.Pool`` initializer that self-assigns each worker a distinct device.

    ``mp.Pool`` runs the SAME initializer for every worker, so we cannot hand
    worker 0 ``cuda:0`` and worker 1 ``cuda:1`` via fixed initargs. Instead each
    worker atomically reads-and-increments a shared counter to claim its own
    index, then derives its pinned device from that. This is what gives us a
    stable worker<->GPU pinning with a plain pool.
    """
    from app.core.gpu import device_for_worker

    with lock:
        worker_index = counter.value
        counter.value += 1

    device_str = device_for_worker(worker_index, detected_gpus)
    worker_initializer(device_str, worker_index, event_queue, secrets_snapshot)


def worker_initializer(
    device_str: str,
    worker_id: int,
    event_queue: Any,
    secrets_snapshot: dict[str, Any],
) -> None:
    """Establish per-process state for a conversion worker.

    Order matters: install the httpx monkeypatch first, seed the secrets cache
    from the parent's snapshot (no DB round-trip), wire progress/log routing to
    the queue, resolve this process's runtime-capacity coordinator for its
    pinned device, then load the marker model dict onto the pinned device.
    CUDA must only be touched here, inside the spawned process — never in the
    parent.
    """
    global _event_queue, _device_str, _worker_id, _capacity

    _event_queue = event_queue
    _device_str = device_str
    _worker_id = worker_id

    # 0. Runtime admission coordinator for this pinned device (PR69). Cheap
    # (device probe only; no model imports), disabled entirely by kill switch.
    try:
        from app.core.config import ADMISSION_ENABLED

        if ADMISSION_ENABLED:
            from app.services.runtime_capacity import default_coordinator

            _capacity = default_coordinator(device_str)
    except Exception:  # noqa: BLE001 - admission must never kill a worker
        logger.exception("worker %d: capacity coordinator setup failed", worker_id)
        _capacity = None

    # 1. Re-install the live httpx interceptor (process-local globals).
    try:
        from app.core.api_manager import seed_secrets_snapshot, setup_api_manager_monkeypatch

        setup_api_manager_monkeypatch()
        seed_secrets_snapshot(secrets_snapshot)
    except Exception:  # noqa: BLE001 - a worker without secrets can still do local-only work
        logger.exception("worker %d: failed to seed api_manager state", worker_id)

    # 2. Route marker/surya tqdm progress and app/marker logs to the queue.
    try:
        from app.services import progress_tracker

        progress_tracker.set_reporter(_worker_progress_reporter)
        progress_tracker.install()

        handler = _QueueLogHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logging.getLogger("marker").addHandler(handler)
        logging.getLogger("app").addHandler(handler)
    except Exception:  # noqa: BLE001 - progress/log routing is best effort
        logger.exception("worker %d: failed to wire progress/log routing", worker_id)

    # 3. Load the model dict onto the pinned device (the expensive cold start).
    # Phase 1 lazy init: by default models are NOT loaded at pool spawn. They
    # lazy-load on the first marker job via MarkerService.convert_file -> initialize().
    # Set MARKER_PRELOAD_MODELS=true to eagerly warm each worker at spawn.
    try:
        from app.core.config import PRELOAD_MARKER_MODELS
        from app.services.marker_service import MarkerService

        global _model_dict
        if not PRELOAD_MARKER_MODELS:
            logger.info(
                "worker %d ready on device=%s (pid=%d); models lazy-load on first marker job",
                worker_id, device_str, os.getpid(),
            )
            return
        svc = MarkerService()
        if _capacity is not None:
            svc.attach_capacity(_capacity, volunteer_job=None)
        device = None if device_str == "cpu" else device_str
        svc.initialize(device=device)
        _model_dict = svc._model_dict
        logger.info("worker %d ready on device=%s (pid=%d)", worker_id, device_str, os.getpid())
    except Exception:  # noqa: BLE001 - surface, but let the pool keep the worker
        logger.exception("worker %d: model load failed on device=%s", worker_id, device_str)


# ---------------------------------------------------------------------------
# Job entrypoint — the pool task
# ---------------------------------------------------------------------------


def worker_run_job(envelope: JobEnvelope) -> Optional[str]:
    """Run one conversion and stream events back to the parent.

    Returns the job id on success (purely informational for the pool future);
    all real data — progress, logs, the final document — flows over the queue,
    which decouples result delivery from the future and is what a future
    multi-node transport will reuse.

    PR69: when admission is enabled the converter is only entered AFTER the
    runtime coordinator reserves capacity and issues a model lease. A refused
    request never reaches the dangerous allocation path — it fails truthfully
    with a structured reason instead.
    """
    global _current_job_id, _model_dict
    _current_job_id = envelope.job_id

    # Tell the parent which PID owns this job, so a cancel can target it.
    _emit(
        WorkerEvent(
            type=WorkerEventType.status,
            job_id=envelope.job_id,
            worker_id=_worker_id,
            status_text="Loading marker converters...",
            pid=os.getpid(),
        )
    )

    # Pre-execution admission (invariant 30): estimate demand from the
    # pinned preprocessor and reserve capacity + a model lease atomically.
    ticket = None
    if _capacity is not None:
        from app.services.runtime_capacity import AdmissionError

        ocr_enabled = not bool(envelope.config.get("disable_ocr", False))
        try:
            ticket = _capacity.admit(envelope.job_id, envelope.filepath, ocr_enabled=ocr_enabled)
        except AdmissionError as exc:
            _emit_runtime(
                envelope.job_id,
                "admission_refused",
                {"reason": str(exc)},
            )
            _emit(
                WorkerEvent(
                    type=WorkerEventType.error,
                    job_id=envelope.job_id,
                    worker_id=_worker_id,
                    error_message=f"runtime admission refused: {exc}",
                )
            )
            _current_job_id = None
            return None
        _emit_runtime(
            envelope.job_id,
            "admitted",
            {
                "demand": ticket.estimate.summary(),
                "capacity": _capacity.ledger.snapshot(),
            },
        )

    try:
        from app.services.marker_service import MarkerService
        from app.services.conversion_service import ConversionService

        svc = MarkerService()
        # Reuse the model dict loaded once in the initializer; if the initializer
        # failed to load it, fall back to a lazy load so the job still runs.
        if _model_dict is not None:
            svc._model_dict = _model_dict
            svc._initialized = True
        if _capacity is not None:
            svc.attach_capacity(
                _capacity,
                volunteer_job=envelope.job_id,
                runtime_notifier=_residency_notifier,
            )

        conversion_svc = ConversionService(svc)
        device = None if _device_str == "cpu" else _device_str
        config = dict(envelope.config)
        formats_requested = _requested_formats(config)
        formats_envelopes: dict[str, dict[str, Any]] | None = None
        if (
            (len(formats_requested) > 1 or formats_requested[0] != "markdown")
            and conversion_svc.supports_multiple_formats(envelope.filepath, config)
        ):
            formats_envelopes = conversion_svc.convert_file_formats(
                envelope.filepath,
                config,
                formats_requested,
                device=device,
            )
            result = formats_envelopes.get(formats_requested[0]) or next(
                iter(formats_envelopes.values())
            )
        else:
            result = conversion_svc.convert_file(envelope.filepath, config, device=device)

        # Cache a model dict the lazy fallback may have created for later jobs.
        if _model_dict is None and svc._model_dict is not None:
            _model_dict = svc._model_dict

        payload: dict[str, Any] = result
        if formats_envelopes is not None:
            payload = {
                "result": result,
                "formats_payload": formats_envelopes,
            }

        payload = _stage_payload_handles(envelope.job_id, payload)

        if ticket is not None:
            _capacity.finish(ticket, outcome="success")
            _capacity.note_successful_execution()
            _emit_runtime(
                envelope.job_id,
                "finished",
                {"outcome": "success", "elapsed_seconds": time.time() - ticket.admitted_at},
            )

        _emit(
            WorkerEvent(
                type=WorkerEventType.result,
                job_id=envelope.job_id,
                worker_id=_worker_id,
                payload=payload,
            )
        )
        return envelope.job_id
    except Exception as exc:  # noqa: BLE001 - report, never crash the worker process
        logger.exception("worker %d: conversion failed for job %s", _worker_id, envelope.job_id)
        if ticket is not None:
            from app.services.marker_service import _is_cuda_oom

            outcome = "oom" if _is_cuda_oom(exc) else "failed"
            _capacity.finish(ticket, outcome=outcome, detail=str(exc))
            if outcome == "oom":
                _emit_runtime(
                    envelope.job_id,
                    "oom_contained",
                    {"detail": str(exc), "capacity": _capacity.ledger.snapshot()},
                )
            else:
                _emit_runtime(envelope.job_id, "finished", {"outcome": "failed"})
        _emit(
            WorkerEvent(
                type=WorkerEventType.error,
                job_id=envelope.job_id,
                worker_id=_worker_id,
                error_message=str(exc),
            )
        )
        return None
    finally:
        _current_job_id = None
