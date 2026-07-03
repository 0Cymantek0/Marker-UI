"""Async task manager for background conversion jobs.

Two execution backends, selected once at construction:

* ``ThreadExecutorBackend`` (default) — runs conversions in a ``ThreadPoolExecutor``
  inside THIS process, exactly as before. Used for CPU-only and single-GPU
  machines where there is nothing to parallelise across devices. The tqdm tap,
  ``JobLogHandler``, and ``api_manager`` globals all work here because everything
  shares one process.

* ``ProcessExecutorBackend`` — spawns one worker PROCESS per GPU (via
  ``mp.Pool``), each pinned to ``cuda:i``. Workers re-install the httpx
  monkeypatch, seed their secrets, and stream ``WorkerEvent``s (progress / log /
  status / result / error) back over a ``multiprocessing.Queue``. The parent's
  drain thread maps those events into the SAME in-memory dicts the SSE
  ``job_events`` endpoint already reads, so the frontend and the in-process path
  are unaffected. The parent owns ALL database writes (no SQLite multi-process
  contention). This is the seam a future multi-node phase reuses: swap the
  ``QueueTransport`` for a Redis/HTTP transport and a worker becomes a remote
  node with no call-site changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import Request
from sse_starlette.event import ServerSentEvent

from app.conversion.formats import OUTPUT_FORMAT_SET
from app.database import async_session_factory
from app.models.job import ConversionJob
from app.services.output_writer import write_conversion_output
from app.services.queue_backends import DurableQueueBackend
from app.services.job_transport import (
    JobEnvelope,
    QueueTransport,
    WorkerEvent,
    WorkerEventType,
)

logger = logging.getLogger(__name__)

SSE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


def _resolve_requested_formats(config: dict[str, Any]) -> list[str]:
    """Return the deduped list of output formats requested for a job.

    Newer jobs send ``output_formats`` (a list) when the UI multi-selects more
    than one format; ``output_format`` is the single-format legacy field. Both
    are honoured so the legacy single-format path keeps working unchanged.
    Unknown formats are dropped so a malformed request never crashes a render.
    """
    raw = config.get("output_formats")
    if isinstance(raw, list) and raw:
        cleaned = [str(f) for f in dict.fromkeys(raw) if str(f) in OUTPUT_FORMAT_SET]
        if cleaned:
            return cleaned
    single = str(config.get("output_format", "markdown") or "markdown")
    return [single] if single in OUTPUT_FORMAT_SET else ["markdown"]


def _formats_payload_for_finalize(
    primary_result: dict[str, Any],
    primary_format: str,
    formats_payload: dict[str, dict[str, Any]] | None,
) -> str | None:
    """Build the JSON cache of per-format output text for a job.

    The cache maps ``{format: text}`` for every format available on the card so
    the preview tabs can switch instantly without reconverting. It stores text
    only (images/assets live on disk in the primary output) to stay small. When
    multi-format rendering did not run, the cache still records the single
    primary format so the UI knows exactly which formats exist for this file.
    """
    payload: dict[str, str] = {}
    if formats_payload:
        for fmt, envelope in formats_payload.items():
            text = (envelope or {}).get("text") if isinstance(envelope, dict) else None
            if isinstance(text, str):
                payload[str(fmt)] = text
    # Always guarantee the primary format is present even if the multi-format
    # path collapsed to a single markdown envelope.
    if primary_format not in payload:
        payload[primary_format] = str(primary_result.get("text") or "")
    return json.dumps(payload) if payload else None


def _actual_output_format_for_finalize(
    primary_result: dict[str, Any],
    requested_format: str,
) -> str:
    """Return the format the converter actually produced.

    Native converters currently produce Markdown even when old clients request
    json/html/chunks. Trust the result extension for that collapse so the UI and
    downloads do not label Markdown as a structured format. Marker still keeps
    explicit json/chunks requests because both use a JSON file extension.
    """

    requested = str(requested_format or "markdown").strip().lower()
    extension = str(primary_result.get("extension") or "").strip().lower().lstrip(".")
    if extension in {"md", "markdown"}:
        return "markdown"
    if extension in {"html", "htm"}:
        return "html"
    if extension == "json":
        return requested if requested in {"json", "chunks"} else "json"
    return requested if requested in OUTPUT_FORMAT_SET else "markdown"


# Registry of thread ID to job ID (ThreadExecutorBackend only).
active_conversion_threads: dict[int, str] = {}


class JobLogHandler(logging.Handler):
    """Routes marker/app log records to the right job (in-process thread backend).

    The process backend does NOT use this: its workers emit ``WorkerEvent.log``
    over the queue, drained by ``TaskManager._dispatch_worker_event``.
    """

    def __init__(self, task_manager: TaskManager) -> None:
        super().__init__()
        self.task_manager = task_manager

    def emit(self, record: logging.LogRecord) -> None:
        try:
            thread_ident = threading.get_ident()
            job_id = active_conversion_threads.get(thread_ident)
            if job_id:
                message = self.format(record)
                self.task_manager.add_job_log(job_id, message, record.levelname)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Execution backends
# ---------------------------------------------------------------------------


class ExecutorBackend(ABC):
    """How conversions actually run: in-process threads or out-of-process workers."""

    name: str = "abstract"
    is_process: bool = False

    @abstractmethod
    def submit(
        self,
        run_job: Any,
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        marker_service: Any,
    ) -> Optional[asyncio.Future]:
        ...

    def shutdown(self, wait: bool = False) -> None:
        """Release workers/threads. No-op by default."""

    @abstractmethod
    def supports_job(self, job_id: str) -> bool:
        """True if this backend owns *job_id* right now."""


class ThreadExecutorBackend(ExecutorBackend):
    """In-process thread pool for one resource class."""

    name = "thread"
    is_process = False

    def __init__(
        self,
        task_manager: TaskManager,
        max_workers: int = 2,
        *,
        resource_name: str = "thread",
        queued_message: str = "Waiting for conversion worker...",
    ) -> None:
        self._task_manager = task_manager
        self._max_workers = max(1, max_workers)
        self.resource_name = resource_name
        self.queued_message = queued_message
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=f"conversion-{resource_name}",
        )
        # job_id -> asyncio Future returned by loop.run_in_executor.
        self._futures: dict[str, asyncio.Future[Any]] = {}

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def submit(
        self,
        run_job: Any,
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        marker_service: Any,
    ) -> Optional[asyncio.Future]:
        loop = asyncio.get_event_loop()

        def _run_with_start() -> Any:
            self._task_manager._mark_job_started(job_id)
            return run_job(job_id, filepath, config, marker_service)

        future = loop.run_in_executor(
            self._executor,
            _run_with_start,
        )
        self._futures[job_id] = future
        return future

    def supports_job(self, job_id: str) -> bool:
        return job_id in self._futures

    def pop(self, job_id: str) -> Optional[asyncio.Future]:
        return self._futures.pop(job_id, None)

    def get(self, job_id: str) -> Optional[asyncio.Future]:
        return self._futures.get(job_id)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)


class ProcessExecutorBackend(ExecutorBackend):
    """One worker process per GPU, drained via the shared event queue.

    Each worker runs ``worker_run_job`` and streams ``WorkerEvent``s back; the
    parent never inspects the pool future's return value (workers return only a
    job id for liveness). All real data — progress, logs, the document — flows
    over the queue and is dispatched by ``TaskManager._drain_loop``.
    """

    name = "process"
    is_process = True

    def __init__(self, task_manager: TaskManager, detected_gpus: int, num_workers: int) -> None:
        self._task_manager = task_manager
        self._detected_gpus = detected_gpus
        self._num_workers = max(1, num_workers)

        # Shared worker-claim counter + guard, so pool_initializer can hand each
        # worker a distinct device index even though the pool calls the SAME
        # initializer for every worker.
        self._claim_counter = mp.Value("i", 0)
        self._claim_lock = mp.Lock()

        # The event sink handed to workers (queue). The transport wraps it so the
        # parent drain loop uses the same abstraction regardless of transport.
        self._transport = QueueTransport()

        # Lazily-imported worker entrypoints + secrets snapshot, captured now so
        # spawn passes plain picklable data, never the live service.
        from app.services.gpu_worker import pool_initializer, worker_run_job
        from app.core.api_manager import export_secrets_snapshot

        self._worker_run_job = worker_run_job
        self._secrets_snapshot = export_secrets_snapshot()

        self._pool = mp.Pool(
            processes=self._num_workers,
            initializer=pool_initializer,
            initargs=(
                self._claim_counter,
                self._claim_lock,
                detected_gpus,
                self._transport.queue,
                self._secrets_snapshot,
            ),
        )

    @property
    def transport(self) -> QueueTransport:
        return self._transport

    def submit(
        self,
        run_job: Any,  # unused: workers define their own run path
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        marker_service: Any,  # unused: workers build their own service
    ) -> Optional[asyncio.Future]:
        envelope = JobEnvelope(
            job_id=job_id,
            filepath=filepath,
            config=config,
            device_str="worker-pinned",  # set per-worker by pool_initializer
        )
        # Apply_async so a dead worker surfaces; the result is ignored (events own delivery).
        self._pool.apply_async(self._worker_run_job, (envelope,))
        return None

    def supports_job(self, job_id: str) -> bool:
        return False  # process jobs are tracked by TaskManager._proc_jobs, not futures

    def shutdown(self, wait: bool = False) -> None:
        try:
            self._pool.close()
            self._pool.join()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass
        self._transport.close()


class TaskManager:
    """Manages background conversion tasks with progress tracking."""

    def __init__(
        self,
        max_workers: int = 2,
        backend: ExecutorBackend | None = None,
        durable_queue: DurableQueueBackend | None = None,
    ) -> None:
        # Pluggable marker executor. Default single-process marker jobs get one
        # worker because marker/surya predictors keep mutable per-job cache state.
        # Safe CPU-only converters use the separate CPU pool below.
        self._backend: ExecutorBackend = backend or ThreadExecutorBackend(
            self,
            max_workers=1,
            resource_name="marker",
            queued_message="Waiting for marker model worker...",
        )
        # CPU thread pool for office/text/archive jobs. These never need marker
        # models or a GPU, so they can use system parallelism without touching
        # the unsafe shared marker predictor state.
        self._cpu_backend: ExecutorBackend = ThreadExecutorBackend(
            self,
            max_workers=max_workers,
            resource_name="cpu",
            queued_message="Waiting for CPU conversion worker...",
        )

        self._tasks: dict[str, asyncio.Future[Any]] = {}
        self._smooth_tasks: dict[str, asyncio.Task[Any]] = {}
        self._progress: dict[str, int] = {}
        self._pids: dict[str, int] = {}

        # In-memory logs and status texts
        self._job_logs: dict[str, list[str]] = {}
        self._job_status_text: dict[str, str] = {}
        self._job_start_time: dict[str, float] = {}
        # True once a real tqdm-derived progress value has arrived for the job;
        # disables the synthetic crawl so the two don't fight.
        self._job_has_real_progress: dict[str, bool] = {}
        # False while a ThreadPoolExecutor future exists but has not started
        # running yet. This lets status APIs report honest queue/wait messages.
        self._job_started: dict[str, bool] = {}
        self._job_queued_message: dict[str, str] = {}
        # Jobs run via the process backend: terminal status recorded by the drain
        # thread so get_status() can resolve them without an asyncio future.
        self._proc_jobs: dict[str, str] = {}
        # job_id -> config for the process backend (needed at finalize time).
        self._proc_configs: dict[str, dict[str, Any]] = {}
        # job_id -> provider_id whose live model hot-swap we must clear on done.
        self._job_providers: dict[str, Optional[str]] = {}
        # job_id -> backend that owns its Future, so cleanup/cancel works for
        # both marker and CPU pools.
        self._job_backends: dict[str, ExecutorBackend] = {}
        self._durable_queue = durable_queue

        self._lock = threading.Lock()
        self._drain_stop = threading.Event()
        self._drain_thread: Optional[threading.Thread] = None

        # Register custom log handler for marker and app (thread backend only;
        # harmless when unused in process mode).
        self._log_handler = JobLogHandler(self)
        self._log_handler.setFormatter(
            logging.Formatter("[%(levelname)s] %(message)s")
        )
        logging.getLogger("marker").addHandler(self._log_handler)
        logging.getLogger("app").addHandler(self._log_handler)

        # Tap marker/surya tqdm bars for real per-stage progress (thread backend).
        from app.services import progress_tracker

        progress_tracker.set_reporter(self.report_stage_progress)
        progress_tracker.install()

        # Start the drain thread only for the process backend.
        if self._backend.is_process:
            self._start_drain_thread()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def recover_durable_jobs(self) -> list[Any]:
        """Return queued/expired durable jobs for an external resubmitter."""
        if self._durable_queue is None:
            return []
        async with async_session_factory() as session:
            return await self._durable_queue.recover_queued(session)

    async def enqueue_durable_job(
        self,
        session: Any,
        *,
        job_id: str,
        filepath: str | None,
        config: dict[str, Any],
        idempotency_key: str | None = None,
        max_retries: int = 0,
    ) -> bool:
        """Persist durable queue metadata in the caller's transaction."""
        if self._durable_queue is None:
            return False
        await self._durable_queue.enqueue(
            session,
            job_id=job_id,
            filepath=filepath,
            config=config,
            idempotency_key=idempotency_key,
            max_retries=max_retries,
        )
        return True

    def _start_drain_thread(self) -> None:
        """Background loop that reads worker events into the in-memory dicts."""
        if self._drain_thread is not None:
            return
        self._drain_thread = threading.Thread(
            target=self._drain_loop, name="worker-event-drain", daemon=True
        )
        self._drain_thread.start()

    def _drain_loop(self) -> None:
        """Block on the transport queue and dispatch each WorkerEvent."""
        from app.services import progress_tracker  # noqa: F401 - keep reference warm

        transport = self._backend.transport  # type: ignore[attr-defined]
        while not self._drain_stop.is_set():
            for event in transport.drain(timeout=0.5):
                try:
                    self._dispatch_worker_event(event)
                except Exception:  # noqa: BLE001 - a bad event must not kill the drain loop
                    logger.exception("Failed to dispatch worker event: %s", event)

    def _dispatch_worker_event(self, event: WorkerEvent) -> None:
        """Map one worker event into the in-memory dicts (and DB on terminal)."""
        job_id = event.job_id

        if event.type is WorkerEventType.progress:
            self.report_stage_progress(job_id, event.percent, event.label)
            return

        if event.type is WorkerEventType.log:
            self.add_job_log(job_id, event.message, event.levelname)
            return

        if event.type is WorkerEventType.status:
            if event.pid is not None:
                self._pids[job_id] = event.pid
            if event.status_text:
                self._job_status_text[job_id] = event.status_text
            return

        if event.type is WorkerEventType.result:
            self._finalize_proc_job(job_id, event.payload)
            return

        if event.type is WorkerEventType.error:
            self._fail_proc_job(job_id, event.error_message)
            return

    def _finalize_proc_job(self, job_id: str, result: dict[str, Any]) -> None:
        """Persist a worker-completed job and mark it done (process backend)."""
        config = self._proc_configs.pop(job_id, {})
        self._progress[job_id] = 90
        self._job_status_text[job_id] = "Finalizing results..."
        try:
            self._run_async(self._finalize_job(job_id, result, config))
        except Exception:  # noqa: BLE001
            logger.exception("finalize failed for process job %s", job_id)
        self._progress[job_id] = 100
        self._job_status_text[job_id] = "Conversion completed successfully."
        self._cleanup_proc_job(job_id, config, state="done")

    def _fail_proc_job(self, job_id: str, error_message: str) -> None:
        """Record a worker failure (process backend)."""
        self._progress[job_id] = 0
        self._job_status_text[job_id] = f"Conversion failed: {error_message}"
        try:
            self._run_async(self._fail_job(job_id, error_message))
        except Exception:  # noqa: BLE001
            logger.exception("fail-job write failed for process job %s", job_id)
        self._cleanup_proc_job(job_id, self._proc_configs.pop(job_id, {}), state="failed")

    def _cleanup_proc_job(self, job_id: str, config: dict[str, Any], *, state: str) -> None:
        with self._lock:
            self._proc_jobs[job_id] = state
            self._pids.pop(job_id, None)
        # Drop any live model hot-swap for this job's provider so it never bleeds
        # into an unrelated later job. Best effort; process backend overrides are
        # per-worker so this is a no-op in practice but keeps the contract even.
        provider_id = config.get("llm_provider")
        if provider_id:
            try:
                from app.core.api_manager import clear_model_override, reset_stuck_counter

                clear_model_override(provider_id)
                reset_stuck_counter(provider_id)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run an async coroutine to completion from a sync (worker/drainer) context."""
        try:
            return asyncio.run(coro)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    def report_stage_progress(self, job_id: str, percent: int, label: str) -> None:
        """Sink for the tqdm progress tap. Monotonic, never regresses."""
        current = self._progress.get(job_id, 0)
        # Cap at 96 so finalization (DB write + file save) owns the last few %.
        percent = max(0, min(96, percent))
        if percent > current:
            self._progress[job_id] = percent
        if label:
            self._job_status_text[job_id] = label
        self._job_has_real_progress[job_id] = True

    def _mark_job_started(self, job_id: str) -> None:
        """Called by thread backends when a queued job begins executing."""
        self._job_started[job_id] = True

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def submit_job(
        self,
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        marker_service: Any,
    ) -> None:
        """Start conversion via the selected backend and track the job."""
        self._progress[job_id] = 10
        self._job_logs[job_id] = []
        self._job_status_text[job_id] = "Starting conversion..."
        self._job_start_time[job_id] = time.time()
        self._job_has_real_progress[job_id] = False
        self._job_started[job_id] = False
        self._job_providers[job_id] = config.get("llm_provider")

        backend = self._select_backend(filepath, config, marker_service)
        queued_message = getattr(backend, "queued_message", "Waiting for conversion worker...")
        self._job_queued_message[job_id] = queued_message
        self._job_status_text[job_id] = queued_message
        future = backend.submit(
            self._run_conversion,
            job_id,
            filepath,
            config,
            marker_service,
        )

        if future is not None:
            # Thread backend: track the asyncio future for status + done cleanup.
            self._tasks[job_id] = future
            self._job_backends[job_id] = backend

            def _on_done(fut: asyncio.Future[Any]) -> None:
                if fut.cancelled():
                    self._tasks.pop(job_id, None)
                    self._job_backends.pop(job_id, None)
                    self._job_started.pop(job_id, None)
                    self._job_queued_message.pop(job_id, None)
                    smooth = self._smooth_tasks.pop(job_id, None)
                    if smooth is not None:
                        smooth.cancel()
                    if isinstance(backend, ThreadExecutorBackend):
                        backend.pop(job_id)
                    return
                exc = fut.exception()
                if exc:
                    logger.error("Job %s failed: %s", job_id, exc)
                self._tasks.pop(job_id, None)
                self._job_backends.pop(job_id, None)
                self._job_started.pop(job_id, None)
                self._job_queued_message.pop(job_id, None)
                smooth = self._smooth_tasks.pop(job_id, None)
                if smooth is not None:
                    smooth.cancel()
                if isinstance(backend, ThreadExecutorBackend):
                    backend.pop(job_id)

            # Synthetic crawl only until real tqdm-derived progress kicks in.
            async def _smooth_progress():
                while job_id in self._tasks:
                    fut = self._tasks[job_id]
                    if fut.done():
                        break
                    await asyncio.sleep(2)
                    if self._job_has_real_progress.get(job_id):
                        continue
                    current = self._progress.get(job_id, 10)
                    if current < 12:
                        self._progress[job_id] = current + 1

            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._smooth_tasks[job_id] = loop.create_task(_smooth_progress())
            future.add_done_callback(_on_done)
        else:
            # Process backend: no in-process future; record config for the drain
            # thread's finalize/fail path and mark the job as worker-owned.
            with self._lock:
                self._proc_configs[job_id] = dict(config)
                self._proc_jobs[job_id] = "running"

    def _select_backend(self, filepath: str, config: dict[str, Any], conversion_service: Any) -> ExecutorBackend:
        """Pick the executor for a job based on its ConversionPlan.

        cpu_thread plans go to the CPU pool; marker_worker plans go to the
        primary marker backend. In the default single-process mode that marker
        backend is intentionally one-wide, while CPU work can run in parallel.
        Planning failures fall back to the marker backend so a bad plan never
        blocks a job.
        """
        try:
            plan = conversion_service.plan(filepath, config)
            if plan.execution_backend == "cpu_thread":
                return self._cpu_backend
        except Exception:  # noqa: BLE001 - a planning error must not block the job
            logger.exception("Failed to plan execution backend for %s; using primary", filepath)
        return self._backend

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def add_job_log(self, job_id: str, message: str, levelname: str) -> None:
        if job_id not in self._job_logs:
            self._job_logs[job_id] = []
        
        # Append message
        self._job_logs[job_id].append(message)

        # tqdm tap owns progress once it starts reporting; don't let coarse
        # log-string guesses override the real per-stage values.
        if self._job_has_real_progress.get(job_id):
            return

        # Parse log content for status text and progress hints. Only a pre-OCR
        # fallback for the brief window before any tqdm bar appears.
        msg_lower = message.lower()
        if "layout" in msg_lower:
            self._job_status_text[job_id] = "Detecting document layout..."
            if self._progress.get(job_id, 0) < 30:
                self._progress[job_id] = 30
        elif "ocr" in msg_lower or "recognition" in msg_lower or "detector" in msg_lower:
            self._job_status_text[job_id] = "Performing OCR and text recognition..."
            if self._progress.get(job_id, 0) < 33:
                self._progress[job_id] = 33

    def get_status(self, job_id: str) -> dict[str, Any]:
        """Return current in-memory progress for *job_id*."""
        progress = self._progress.get(job_id, 0)
        future = self._tasks.get(job_id)
        proc_state = self._proc_jobs.get(job_id)
        if future is None and proc_state is None:
            status = "completed" if progress >= 100 else "pending"
        elif future is not None:
            if future.done():
                exc = future.exception()
                status = "failed" if exc else "completed"
            else:
                status = "processing"
        else:
            # Process-backend job: resolve from recorded state.
            status = "completed" if progress >= 100 else (
                "failed" if proc_state == "failed" else "processing"
            )

        if future is not None and not future.done() and not self._job_started.get(job_id, True):
            message = self._job_queued_message.get(job_id, "Waiting for conversion worker...")
            progress = max(progress, 10)
            logs = self._job_logs.get(job_id, [])
            start_time = self._job_start_time.get(job_id)
            elapsed = int(time.time() - start_time) if start_time else 0
            return {
                "job_id": job_id,
                "status": "processing",
                "progress": progress,
                "message": message,
                "logs": logs,
                "elapsed": elapsed,
                "eta": 0,
            }

        message = self._job_status_text.get(job_id, "Processing document...")
        if message in ("Starting conversion...", "Loading marker converters...") and progress > 10:
            if progress >= 90:
                message = "Generating structured output..."
            elif progress >= 80:
                message = "Formatting mathematical equations..."
            elif progress >= 70:
                message = "Extracting tables..."
            elif progress >= 50:
                message = "Performing OCR and text recognition..."
            elif progress >= 30:
                message = "Detecting document layout..."
            else:
                message = "Processing document..."

        logs = self._job_logs.get(job_id, [])

        # Calculate elapsed and ETA
        start_time = self._job_start_time.get(job_id)
        elapsed = int(time.time() - start_time) if start_time else 0
        eta = 0
        if status == "processing":
            if progress > 10 and progress < 100:
                estimated_total = elapsed * 100 / progress
                eta = max(1, int(estimated_total - elapsed))
            else:
                eta = 45  # Default fallback

        return {
            "job_id": job_id,
            "status": status,
            "progress": progress,
            "message": message,
            "logs": logs,
            "elapsed": elapsed,
            "eta": eta
        }

    async def cancel_job(self, job_id: str) -> bool:
        """Attempt to cancel a running job and kill its underlying process.

        Thread backend: cancel the asyncio future. Process backend: kill the
        pinned worker PID (the pool respawns a fresh worker). Both then mark the
        job cancelled in the DB.
        """
        future = self._tasks.get(job_id)
        proc_state = self._proc_jobs.get(job_id)
        cancelled = False

        if future and not future.done():
            cancelled = future.cancel()
            self._progress.pop(job_id, None)
            self._tasks.pop(job_id, None)
            backend = self._job_backends.pop(job_id, None)
            if isinstance(backend, ThreadExecutorBackend):
                backend.pop(job_id)
            self._job_has_real_progress.pop(job_id, None)
            self._job_started.pop(job_id, None)
            self._job_queued_message.pop(job_id, None)
            smooth = self._smooth_tasks.pop(job_id, None)
            if smooth is not None:
                smooth.cancel()
        elif proc_state is not None:
            cancelled = True
            with self._lock:
                self._proc_jobs.pop(job_id, None)
                self._proc_configs.pop(job_id, None)
            self._progress.pop(job_id, None)
            self._job_has_real_progress.pop(job_id, None)
            self._job_started.pop(job_id, None)
            self._job_queued_message.pop(job_id, None)
            smooth = self._smooth_tasks.pop(job_id, None)
            if smooth is not None:
                smooth.cancel()

        if cancelled:
            pid = self._pids.pop(job_id, None)
            if pid is not None:
                self._kill_pid(pid)
            await self._update_job_status(job_id, "cancelled")
        return cancelled

    def shutdown(self, wait: bool = False) -> None:
        """Stop the drain thread and release the executor/pool."""
        self._drain_stop.set()
        for smooth in list(self._smooth_tasks.values()):
            smooth.cancel()
        self._smooth_tasks.clear()
        self._cpu_backend.shutdown(wait=wait)
        # Unblock a blocking drain by pushing the stop sentinel.
        transport = getattr(self._backend, "transport", None)
        if transport is not None:
            try:
                transport.stop()
            except Exception:  # noqa: BLE001
                pass
        self._backend.shutdown(wait=wait)

    @staticmethod
    def _kill_pid(pid: int) -> None:
        if pid == os.getpid():
            logger.info("Refusing to kill own main server process (PID %d)", pid)
            return
        try:
            if sys.platform == "win32":
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        break
                    import time
                    time.sleep(0.5)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ------------------------------------------------------------------
    # SSE event generator
    # ------------------------------------------------------------------

    async def job_events(self, request: Request, job_id: str) -> AsyncGenerator[ServerSentEvent, None]:
        """Yield SSE events with progress updates until the job is done.

        Detects client disconnects via *request* and enforces an overall
        timeout of SSE_TIMEOUT_SECONDS.
        """
        last_progress = -1
        last_log_len = 0
        elapsed = 0.0

        while True:
            if await request.is_disconnected():
                # Release the SSE connection handler, but DO NOT pop/cancel the background task!
                return

            info = self.get_status(job_id)
            progress = info["progress"]
            status = info["status"]
            current_log_len = len(info.get("logs", []))

            if progress != last_progress or current_log_len != last_log_len or status in ("completed", "failed", "cancelled"):
                last_progress = progress
                last_log_len = current_log_len
                is_terminal = status in ("completed", "failed", "cancelled")
                event_type = "status" if is_terminal else "progress"
                yield ServerSentEvent(
                    data=json.dumps(info),
                    event=event_type,
                )

            if status in ("completed", "failed", "cancelled"):
                break

            await asyncio.sleep(0.5)
            elapsed += 0.5
            if elapsed >= SSE_TIMEOUT_SECONDS:
                yield ServerSentEvent(
                    data=json.dumps({"job_id": job_id, "status": "timeout", "progress": progress}),
                    event="progress",
                )
                break

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_conversion(
        self,
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        conversion_service: Any,
    ) -> dict[str, Any]:
        """Runs inside ThreadPoolExecutor - updates DB on completion."""
        self._pids[job_id] = os.getpid()
        thread_ident = threading.get_ident()
        active_conversion_threads[thread_ident] = job_id
        try:
            self._progress[job_id] = 10
            self._job_status_text[job_id] = "Starting conversion..."

            # Multi-format output: when the user selected more than one format
            # and the resolved engine can render them from one parse, render all
            # requested formats now (single document parse -> N renders). The
            # primary format drives the persisted file/images; every requested
            # format is cached in formats_json so preview tabs never reconvert.
            formats_requested = _resolve_requested_formats(config)
            formats_envelopes: dict[str, dict[str, Any]] | None = None
            if (
                len(formats_requested) > 1
                and conversion_service.supports_multiple_formats(filepath, dict(config))
            ):
                formats_envelopes = conversion_service.convert_file_formats(
                    filepath, dict(config), formats_requested
                )
                result = formats_envelopes.get(formats_requested[0]) or next(
                    iter(formats_envelopes.values())
                )
            else:
                result = conversion_service.convert_file(filepath, dict(config))

            self._progress[job_id] = 90
            self._job_status_text[job_id] = "Finalizing results..."

            # Persist result synchronously via a new async loop
            try:
                asyncio.run(
                    self._finalize_job(job_id, result, config, formats_envelopes)
                )
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        self._finalize_job(job_id, result, config, formats_envelopes)
                    )
                finally:
                    loop.close()
            self._progress[job_id] = 100
            self._job_status_text[job_id] = "Conversion completed successfully."
            return result
        except Exception as exc:
            logger.exception("Conversion failed for job %s", job_id)
            self._progress[job_id] = 0
            self._job_status_text[job_id] = f"Conversion failed: {exc}"
            try:
                asyncio.run(self._fail_job(job_id, str(exc)))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._fail_job(job_id, str(exc)))
                finally:
                    loop.close()
            except Exception:
                logger.exception("Failed to record error for job %s", job_id)
            raise
        finally:
            active_conversion_threads.pop(thread_ident, None)
            self._pids.pop(job_id, None)
            # Drop any live model hot-swap for this job's provider so it never
            # bleeds into an unrelated later job.
            provider_id = config.get("llm_provider")
            if provider_id:
                try:
                    from app.core.api_manager import clear_model_override, reset_stuck_counter
                    clear_model_override(provider_id)
                    reset_stuck_counter(provider_id)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass

    # ------------------------------------------------------------------
    # DB helpers (async - called via asyncio.run from thread)
    # ------------------------------------------------------------------

    async def _update_job_status(self, job_id: str, status: str) -> None:
        async with async_session_factory() as session:
            from sqlalchemy import update

            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .values(status=status)
            )
            await session.commit()

    async def _finalize_job(
        self,
        job_id: str,
        result: dict[str, Any],
        config: dict[str, Any],
        formats_payload: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        # Check if job still exists and is not cancelled
        async with async_session_factory() as session:
            from sqlalchemy import select
            stmt = select(ConversionJob.status).where(ConversionJob.id == job_id)
            res = await session.execute(stmt)
            status = res.scalar_one_or_none()
            if not status or status == "cancelled":
                logger.info("Job %s was cancelled or deleted. Skipping finalization.", job_id)
                return

        result_text = result.get("text", "")
        metadata = result.get("metadata") or {}
        # Only persist the image-understanding sidecar (a small list); drop any
        # large/binary metadata the renderer may have returned.
        result_metadata = {
            "image_understanding": metadata.get("image_understanding") or [],
        }
        if metadata.get("engine"):
            result_metadata["engine"] = metadata["engine"]
        if metadata.get("probe_result"):
            result_metadata["probe_result"] = metadata["probe_result"]
        if metadata.get("mixed_engine_segments"):
            result_metadata["mixed_engine_segments"] = metadata["mixed_engine_segments"]
        if metadata.get("audio"):
            result_metadata["audio"] = metadata["audio"]
        if metadata.get("audio_batch"):
            result_metadata["audio_batch"] = metadata["audio_batch"]
        if metadata.get("video"):
            result_metadata["video"] = metadata["video"]
        result_metadata_json = json.dumps(result_metadata) if any(result_metadata.values()) else None
        requested_output_format = config.get("output_format", "markdown")
        output_format = _actual_output_format_for_finalize(result, requested_output_format)
        effective_config = dict(config)
        effective_config["output_format"] = output_format
        original_name = config.get("original_name", "output")
        local_filepath = config.get("local_filepath")
        output_dir = config.get("output_dir")

        # Determine target base directory
        if output_dir:
            target_dir = Path(output_dir)
        elif local_filepath:
            target_dir = Path(local_filepath).parent
        else:
            target_dir = Path("data/output")

        written = write_conversion_output(
            result,
            source_name=original_name or job_id,
            output_base=target_dir,
            output_format=output_format,
            conversion_config=effective_config,
            layout="directory_if_assets",
            disable_image_extraction=bool(config.get("disable_image_extraction", False)),
            job_id=job_id,
            source_url=config.get("source_url"),
        )
        if written.asset_entries:
            result_metadata["assets"] = written.asset_entries
        result_metadata["manifest_path"] = str(written.manifest_path.resolve())
        result_metadata_json = json.dumps(result_metadata) if any(result_metadata.values()) else None

        # Cache every generated format's text in formats_json so the preview
        # tabs (markdown/html/json/chunks) can switch instantly with no
        # reconversion. The persisted primary file above already carries images
        # for its format; the cache stores text only to stay small and portable.
        formats_json = _formats_payload_for_finalize(result, output_format, formats_payload)

        async with async_session_factory() as session:
            from sqlalchemy import update

            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status != "cancelled")
                .values(
                    status="completed",
                    result_text=result_text,
                    result_metadata_json=result_metadata_json,
                    formats_json=formats_json,
                    output_format=output_format,
                    result_path=str(written.final_path),
                    progress=100,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    async def _fail_job(self, job_id: str, error_message: str) -> None:
        async with async_session_factory() as session:
            from sqlalchemy import select, update
            stmt = select(ConversionJob.status).where(ConversionJob.id == job_id)
            res = await session.execute(stmt)
            status = res.scalar_one_or_none()
            if not status or status == "cancelled":
                logger.info("Job %s was cancelled or deleted. Skipping failure recording.", job_id)
                return

            # Only mark as failed if not already in a terminal state (e.g. cancelled)
            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status != "cancelled")
                .values(
                    status="failed",
                    error_message=error_message,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

