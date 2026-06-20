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
from typing import Any, Optional

from app.services.job_transport import JobEnvelope, WorkerEvent, WorkerEventType

logger = logging.getLogger(__name__)

# Per-process state, established by worker_initializer and read by worker_run_job.
_event_queue: Any | None = None
_device_str: str = "cpu"
_worker_id: int = -1
_model_dict: dict[str, Any] | None = None
_current_job_id: str | None = None


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
    the queue, then load the marker model dict onto the pinned device. CUDA must
    only be touched here, inside the spawned process — never in the parent.
    """
    global _event_queue, _device_str, _worker_id

    _event_queue = event_queue
    _device_str = device_str
    _worker_id = worker_id

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
    try:
        from app.services.marker_service import MarkerService

        global _model_dict
        svc = MarkerService()
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

    try:
        from app.services.marker_service import MarkerService

        svc = MarkerService()
        # Reuse the model dict loaded once in the initializer; if the initializer
        # failed to load it, fall back to a lazy load so the job still runs.
        if _model_dict is not None:
            svc._model_dict = _model_dict
            svc._initialized = True

        device = None if _device_str == "cpu" else _device_str
        result = svc.convert_file(envelope.filepath, dict(envelope.config), device=device)

        # Cache a model dict the lazy fallback may have created for later jobs.
        if _model_dict is None and svc._model_dict is not None:
            _model_dict = svc._model_dict

        _emit(
            WorkerEvent(
                type=WorkerEventType.result,
                job_id=envelope.job_id,
                worker_id=_worker_id,
                payload=result,
            )
        )
        return envelope.job_id
    except Exception as exc:  # noqa: BLE001 - report, never crash the worker process
        logger.exception("worker %d: conversion failed for job %s", _worker_id, envelope.job_id)
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
