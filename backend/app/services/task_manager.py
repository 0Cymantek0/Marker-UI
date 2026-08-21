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
from app.services import artifact_handles
from app.services.output_writer import write_conversion_output
from app.services.queue_backends import DurableQueueBackend
from app.services.job_transport import (
    JobEnvelope,
    QueueTransport,
    WorkerEvent,
    WorkerEventType,
)

logger = logging.getLogger(__name__)


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

SSE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
DB_METADATA_AUDIO_SEGMENT_LIMIT = 200
DB_METADATA_WORDS_PER_SEGMENT_LIMIT = 20
DB_METADATA_VIDEO_FRAME_LIMIT = 50


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


def _resolved_asset_entries_for_metadata(
    asset_entries: list[dict[str, Any]],
    asset_paths: list[Path],
) -> list[dict[str, Any]]:
    """Return DB metadata assets with readable absolute paths.

    Output manifests keep portable relative paths for downloads and bundles.
    Job metadata is local status data, so callers should not have to infer the
    output bundle directory before reading a persisted sidecar.
    """
    resolved: list[dict[str, Any]] = []
    for index, entry in enumerate(asset_entries):
        item = dict(entry)
        if index < len(asset_paths):
            item["path"] = str(asset_paths[index].resolve())
        resolved.append(item)
    return resolved


def _result_metadata_for_db(metadata: dict[str, Any]) -> dict[str, Any]:
    """Build bounded status metadata for the database row.

    Full converter metadata is preserved in the output manifest. The DB row is
    used for job/status UI, so large per-word transcripts and video frame lists
    must be capped to keep history queries cheap.
    """
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
        result_metadata["audio"] = _compact_audio_metadata(metadata["audio"])
    if metadata.get("audio_batch"):
        result_metadata["audio_batch"] = _compact_audio_batch_metadata(metadata["audio_batch"])
    if metadata.get("video"):
        result_metadata["video"] = _compact_video_metadata(metadata["video"])
    if metadata.get("chunking"):
        result_metadata["chunking"] = metadata["chunking"]
    return result_metadata


def _compact_audio_metadata(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = {
        key: item
        for key, item in value.items()
        if key != "raw_provider_metadata"
    }
    if "transcript" in compact:
        compact["transcript"] = _compact_transcript_payload(compact["transcript"])
    if "raw_provider_metadata" in value:
        compact["raw_provider_metadata_omitted"] = True
    return compact


def _compact_audio_batch_metadata(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    sources = value.get("sources")
    if isinstance(sources, list):
        remaining = DB_METADATA_AUDIO_SEGMENT_LIMIT
        compact_sources: list[Any] = []
        for source in sources:
            if not isinstance(source, dict):
                compact_sources.append(source)
                continue
            next_source = _compact_transcript_payload(source, max_segments=max(0, remaining))
            compact_sources.append(next_source)
            remaining -= min(len(source.get("segments") or []), max(0, remaining))
        compact["sources"] = compact_sources
        source_segment_count = sum(
            len(source.get("segments") or [])
            for source in sources
            if isinstance(source, dict)
        )
        if source_segment_count > DB_METADATA_AUDIO_SEGMENT_LIMIT:
            compact["segments_truncated"] = True
            compact["db_segment_limit"] = DB_METADATA_AUDIO_SEGMENT_LIMIT
    return compact


def _compact_video_metadata(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    if "transcript" in compact:
        compact["transcript"] = _compact_transcript_payload(compact["transcript"])
    frames = compact.get("frames")
    if isinstance(frames, list) and len(frames) > DB_METADATA_VIDEO_FRAME_LIMIT:
        compact["frames"] = frames[:DB_METADATA_VIDEO_FRAME_LIMIT]
        compact["frame_count"] = len(frames)
        compact["frames_truncated"] = True
        compact["db_frame_limit"] = DB_METADATA_VIDEO_FRAME_LIMIT
    return compact


def _compact_transcript_payload(value: Any, *, max_segments: int = DB_METADATA_AUDIO_SEGMENT_LIMIT) -> Any:
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    segments = value.get("segments")
    if isinstance(segments, list):
        compact_segments = [
            _compact_audio_segment_payload(segment)
            for segment in segments[:max_segments]
        ]
        compact["segments"] = compact_segments
        compact["segment_count"] = len(segments)
        if len(segments) > max_segments:
            compact["segments_truncated"] = True
            compact["db_segment_limit"] = max_segments
    return compact


def _compact_audio_segment_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    words = value.get("words")
    if isinstance(words, list) and len(words) > DB_METADATA_WORDS_PER_SEGMENT_LIMIT:
        compact["words"] = words[:DB_METADATA_WORDS_PER_SEGMENT_LIMIT]
        compact["word_count"] = len(words)
        compact["words_truncated"] = True
        compact["db_words_per_segment_limit"] = DB_METADATA_WORDS_PER_SEGMENT_LIMIT
    return compact


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
        loop = _get_or_create_event_loop()

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
        # ThreadPoolExecutor cannot kill a running Python thread. This flag
        # makes cancellation sticky so a worker that finishes later cannot
        # overwrite a cancelled DB row with completed/failed.
        self._cancel_requested: set[str] = set()
        self._durable_queue = durable_queue
        # Lease TTL for durable jobs. Long enough that a healthy conversion is
        # never wrongly flagged as stuck, short enough that a crashed worker is
        # recoverable within a reasonable window. Overridable via env for ops.
        try:
            self._lease_seconds = int(os.getenv("MARKER_DURABLE_LEASE_SECONDS", "1800"))
        except (TypeError, ValueError):
            self._lease_seconds = 1800

        self._lock = threading.Lock()
        # Event loop that owns the shared async engine's connections
        # (captured on the first kernel-path submit). Worker threads
        # marshal DB coroutines here instead of driving pooled asyncpg
        # connections from private loops, which corrupts the protocol.
        self._db_loop: asyncio.AbstractEventLoop | None = None
        self._drain_stop = threading.Event()
        self._drain_thread: Optional[threading.Thread] = None

        # Kernel runtime authority (PR67B): when enabled, conversions are
        # authorized as kernel work, dispatched through claim_fair, kept
        # alive by evidence-backed liveness, and completed only through
        # fenced accepted publication. ``_kernel_claims`` carries the live
        # claim context from the dispatcher to the executor finalize paths.
        self._kernel_runtime: Any = None
        self._kernel_claims: dict[str, Any] = {}

        # Local ArtifactHandle data plane (PR68A): the parent-side store that
        # resolves worker result handles before finalization. Only the process
        # backend crosses the pickled-result boundary the seam optimizes.
        self._artifact_store: Optional[artifact_handles.ArtifactHandleStore] = None
        if self._backend.is_process:
            self._artifact_store = artifact_handles.default_store()
        self._artifact_results_since_sweep = 0

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

    @property
    def kernel_runtime(self) -> Any:
        return self._kernel_runtime

    def start_kernel_runtime(self, conversion_service: Any, **overrides: Any) -> Any:
        """Create (once) the kernel runtime coordinator bound to this manager.

        Reads the PR67B tuning knobs from ``app.core.config``; ``overrides``
        exist for tests. The coordinator is not started here — the caller
        (app lifespan, tests) runs ``recover()`` and then ``start()``.
        """
        try:
            self._db_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass  # constructed outside a loop; worker threads use private loops
        if self._kernel_runtime is not None:
            return self._kernel_runtime
        from app.core.config import (
            KERNEL_DISPATCH_POLL_SECONDS,
            KERNEL_LEASE_SECONDS,
            KERNEL_MAX_IN_FLIGHT,
            KERNEL_RENEW_INTERVAL_SECONDS,
            KERNEL_RUNTIME_OWNER,
            KERNEL_RUNTIME_WORKSPACE,
            KERNEL_WATCHDOG_INTERVAL_SECONDS,
        )
        from app.services.kernel_runtime import KernelRuntimeCoordinator

        coordinator = KernelRuntimeCoordinator(
            self,
            workspace_id=overrides.pop("workspace_id", KERNEL_RUNTIME_WORKSPACE),
            owner_id=overrides.pop("owner_id", KERNEL_RUNTIME_OWNER),
            lease_seconds=overrides.pop("lease_seconds", KERNEL_LEASE_SECONDS),
            renew_interval_seconds=overrides.pop(
                "renew_interval_seconds", KERNEL_RENEW_INTERVAL_SECONDS
            ),
            dispatch_poll_seconds=overrides.pop(
                "dispatch_poll_seconds", KERNEL_DISPATCH_POLL_SECONDS
            ),
            watchdog_interval_seconds=overrides.pop(
                "watchdog_interval_seconds", KERNEL_WATCHDOG_INTERVAL_SECONDS
            ),
            max_in_flight=overrides.pop("max_in_flight", KERNEL_MAX_IN_FLIGHT),
            **overrides,
        )
        coordinator.set_conversion_service(conversion_service)
        self._kernel_runtime = coordinator
        return coordinator

    async def submit_conversion(
        self,
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        marker_service: Any,
    ) -> int | None:
        """Submit a conversion through the live runtime authority.

        Kernel mode authorizes the job as exactly one kernel work item and
        returns its work id; the coordinator's dispatch loop claims and
        executes it. Legacy mode falls back to direct executor submission
        and returns None.
        """
        if self._kernel_runtime is None:
            self.submit_job(job_id, filepath, config, marker_service)
            return None
        self._kernel_runtime.set_conversion_service(marker_service)
        self._job_status_text[job_id] = "Queued for kernel dispatch..."
        self._job_logs[job_id] = []
        # Source truth (PR70 local slice): normalize/acquire the source
        # revision before authorization so the work item references
        # committed source truth. REST/agent already acquired pre-probe;
        # this chokepoint covers direct submissions and retries.
        config = await self._kernel_runtime.ensure_source_revision(job_id, filepath, config)
        return await self._kernel_runtime.authorize(job_id, config)

    async def recover_durable_jobs(self, conversion_service: Any) -> list[str]:
        """Reclaim queued/expired durable jobs and resubmit them.

        Called at startup (before the stale-job sweeper) so durable rows
        survive a crash/restart instead of being marked failed. For each
        recoverable item we:

        1. Respect the retry budget — jobs at/over ``max_retries`` are marked
           failed with a clear reason, not retried forever.
        2. Verify the source file still exists — a job whose input vanished
           cannot succeed and is marked failed rather than silently dropped.
        3. Re-enqueue with an incremented ``retry_count`` and a fresh lease,
           then submit to a worker.

        Returns the list of job_ids that were actually resubmitted (excluding
        those marked failed for budget/missing-file reasons).
        """
        if self._durable_queue is None:
            return []
        from sqlalchemy import update
        from app.services.queue_backends import append_job_event

        async with async_session_factory() as recover_session:
            items = await self._durable_queue.recover_queued(recover_session)
        resubmitted: list[str] = []
        async with async_session_factory() as session:
            for item in items:
                # Budget check: retry_count already consumed all attempts.
                if item.max_retries > 0 and item.retry_count >= item.max_retries:
                    await self._durable_queue.mark_terminal(
                        session,
                        job_id=item.job_id,
                        status="failed",
                        message=(
                            f"Durable job exceeded retry budget "
                            f"(retry_count={item.retry_count}, max_retries={item.max_retries})."
                        ),
                    )
                    continue

                # Source file must still exist or recovery is data loss.
                filepath = item.filepath
                if not filepath or not Path(filepath).is_file():
                    await self._durable_queue.mark_terminal(
                        session,
                        job_id=item.job_id,
                        status="failed",
                        message=f"Durable job source file not found: {filepath}",
                    )
                    continue

                # Re-enqueue with incremented retry_count and a fresh lease so a
                # duplicate recovery cannot double-submit the same job.
                config = dict(item.config or {})
                config.setdefault("original_name", item.job_id)
                await self._durable_queue.enqueue(
                    session,
                    job_id=item.job_id,
                    filepath=filepath,
                    config=config,
                    idempotency_key=item.idempotency_key,
                    max_retries=item.max_retries,
                )
                # Increment retry_count for this recovery attempt (enqueue resets
                # it to 0, so we set it explicitly to item.retry_count + 1).
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id == item.job_id)
                    .values(retry_count=item.retry_count + 1)
                )
                await append_job_event(
                    session,
                    job_id=item.job_id,
                    event_type="queue.recovered",
                    status="pending",
                    payload={
                        "retry_count": item.retry_count + 1,
                        "max_retries": item.max_retries,
                        "filepath": filepath,
                    },
                )
                resubmitted.append(item.job_id)
            await session.commit()

        # Submit each reclaimed job to a worker. Done after the commit so the
        # row is durable before any worker can finalize it.
        for job_id in resubmitted:
            # Look up the committed row to rebuild the submit args.
            async with async_session_factory() as session:
                row = await session.get(ConversionJob, job_id)
                if row is None or row.status != "pending":
                    continue
                try:
                    config = json.loads(row.config_json) if row.config_json else {}
                except (TypeError, ValueError):
                    config = {}
                if not isinstance(config, dict):
                    config = {}
                filepath = config.get("durable_filepath") or config.get("local_filepath") or ""
            if not filepath:
                continue
            self.submit_job(job_id, filepath, config, conversion_service)
        return resubmitted

    async def recover_and_sweep_durable_jobs(self, conversion_service: Any) -> dict[str, list[str]]:
        """Startup reconciliation: recover durable jobs, then sweep the rest.

        This is the single entry point ``lifespan`` calls at boot. It must run
        BEFORE any unconditional stale-job sweeper, otherwise the sweeper marks
        every recoverable durable row ``failed`` before recovery can see it.

        Order:
        1. Recover durable jobs (resubmit those within retry budget whose source
           file still exists; mark the rest failed with a clear reason).
        2. Sweep non-durable ``pending``/``processing`` rows left from a prior
           crashed session to ``failed`` — but ONLY non-durable rows, so durable
           rows that were intentionally preserved in step 1 survive.

        Returns ``{"recovered": [...job_ids], "swept": [...job_ids]}`` so callers
        (and tests) can observe exactly what happened.
        """
        if self._kernel_runtime is not None:
            # Kernel authority owns recovery: ``coordinator.recover()``
            # reconciles dispatch, completes lost acks, projects accepted
            # publications, adopts legacy rows, and sweeps the rest. The
            # legacy resubmission path must never run beside it — two
            # schedulers deciding ownership for one job is the exact race
            # PR67B exists to close.
            return {"recovered": [], "swept": []}
        recovered = await self.recover_durable_jobs(conversion_service)

        from sqlalchemy import select, update

        swept: list[str] = []
        async with async_session_factory() as session:
            # Select non-durable pending/processing rows. Durable rows
            # (queue_backend IS NOT NULL) are owned by the durable queue and
            # were handled above; sweeping them here would defeat recovery.
            stmt = (
                select(ConversionJob.id)
                .where(ConversionJob.status.in_(["pending", "processing"]))
                .where(ConversionJob.queue_backend.is_(None))
            )
            stale_ids = [row for row in (await session.execute(stmt)).scalars().all()]
            if stale_ids:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id.in_(stale_ids))
                    .values(
                        status="failed",
                        error_message="Interrupted by server restart",
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                swept.extend(stale_ids)
        return {"recovered": recovered, "swept": swept}

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
        # Startup reclamation: orphaned blobs from a previous crashed session
        # die here, before any live handoff could race the sweep.
        self._sweep_stale_artifacts()
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
            self._kernel_note_activity(job_id)
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
        resolved = self._resolve_artifact_payload(job_id, result)
        if resolved is None:
            # Resolution already failed the job honestly; nothing to finalize.
            return
        result = resolved
        formats_payload = None
        if isinstance(result, dict) and "result" in result:
            formats_payload = result.get("formats_payload")
            result = result.get("result") or {}
        self._progress[job_id] = 90
        self._job_status_text[job_id] = "Finalizing results..."
        projected = True
        try:
            projected = self._run_async(self._finalize_job(job_id, result, config, formats_payload))
        except Exception:  # noqa: BLE001
            logger.exception("finalize failed for process job %s", job_id)
        if projected:
            self._progress[job_id] = 100
            self._job_status_text[job_id] = "Conversion completed successfully."
        self._cleanup_proc_job(job_id, config, state="done")
        self._maybe_sweep_artifacts()

    def _resolve_artifact_payload(self, job_id: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Rebuild the logical payload from an ArtifactHandle wire envelope.

        Consumer side is strict by contract (PR68A): a missing, corrupt,
        truncated, cross-job, or incompatible handle fails the job with a
        truthful message rather than letting wrong bytes become accepted
        output. Inline payloads pass through untouched.
        """
        if not artifact_handles.is_handle_envelope(payload):
            return payload
        store = self._artifact_store
        if store is None:
            logger.error("artifact envelope for job %s rejected: store unavailable", job_id)
            self._fail_proc_job(job_id, "artifact handoff rejected: handle store unavailable")
            return None
        try:
            return artifact_handles.resolve_worker_payload(payload, store=store, job_id=job_id)
        except artifact_handles.ArtifactHandleError as exc:
            logger.error("artifact handoff failed for job %s: %s", job_id, exc)
            self._fail_proc_job(job_id, f"artifact handoff failed: {exc}")
            return None
        except Exception:  # noqa: BLE001 - never let a data-plane bug fake a completion
            logger.exception("unexpected artifact resolution failure for job %s", job_id)
            self._fail_proc_job(job_id, "artifact handoff failed unexpectedly")
            return None

    def _sweep_stale_artifacts(self) -> None:
        """Reclaim orphaned artifact blobs older than the configured age."""
        store = self._artifact_store
        if store is None:
            return
        try:
            from app.core.config import ARTIFACT_HANDLE_SWEEP_SECONDS

            removed = store.sweep(older_than_seconds=ARTIFACT_HANDLE_SWEEP_SECONDS)
            if removed:
                logger.info("artifact sweep removed %d stale blob(s)", len(removed))
        except Exception:  # noqa: BLE001 - reclamation is best effort
            logger.exception("artifact sweep failed")

    def _maybe_sweep_artifacts(self, *, every: int = 25) -> None:
        """Periodic reclamation so long-running sessions stay bounded."""
        if self._artifact_store is None:
            return
        self._artifact_results_since_sweep += 1
        if self._artifact_results_since_sweep >= every:
            self._artifact_results_since_sweep = 0
            self._sweep_stale_artifacts()

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
        self._kernel_claims.pop(job_id, None)
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
        # Deferred eviction of per-job in-memory state. The drain thread is
        # sync-only (no event loop), so use a threading.Timer instead of
        # loop.call_later. The SSE poller has usually already observed the
        # terminal _proc_jobs entry by the time the delay elapses.
        self._cleanup_job_memory(job_id, delay=30.0)

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine to completion from a sync (worker/drainer) context.

        The shared async engine's pooled connections are bound to the
        event loop that created them. asyncpg connections driven from a
        different loop corrupt the wire protocol ("got result for
        unknown protocol callback"), so when the owning loop is known
        (``self._db_loop``, captured on the first kernel-path submit),
        the coroutine is marshalled there with
        ``asyncio.run_coroutine_threadsafe`` and the caller blocks on
        the result — one engine, one loop, many threads.

        Legacy deployments without a captured loop keep the original
        behavior: ``asyncio.run`` from true sync contexts, a private
        loop when a (non-owning) loop is already running in this
        thread.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        owner = self._db_loop
        if owner is not None and owner.is_running() and running is not owner:
            future = asyncio.run_coroutine_threadsafe(coro, owner)
            return future.result(timeout=120.0)
        if running is None:
            return asyncio.run(coro)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
            asyncio.set_event_loop(running)

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
        self._kernel_note_activity(job_id)

    def _kernel_note_activity(self, job_id: str) -> None:
        """Feed real control-loop activity to the live kernel claim, if any.

        Called from worker threads and the process-event drain thread; the
        claim's lock keeps the counter coherent. This is the ONLY source of
        liveness evidence — no timer may renew a lease without it.
        """
        claim = self._kernel_claims.get(job_id)
        if claim is not None:
            claim.note_activity()

    def _mark_job_started(self, job_id: str) -> None:
        """Called by thread backends when a queued job begins executing."""
        self._job_started[job_id] = True

    # Dicts that must survive briefly past terminal status so the SSE
    # stream can emit the final event. The values are evicted by
    # ``_cleanup_job_memory`` after a short grace period.
    _DEFERRED_CLEANUP_KEYS = (
        "_progress",
        "_job_logs",
        "_job_status_text",
        "_job_start_time",
        "_job_has_real_progress",
        "_job_providers",
    )

    def _purge_job_memory(self, job_id: str) -> None:
        """Remove all in-memory tracking entries for *job_id*.

        Safe to call multiple times — each pop has a default. Also clears
        LLM call traces (see CACHE-3).
        """
        for attr in self._DEFERRED_CLEANUP_KEYS:
            getattr(self, attr).pop(job_id, None)
        try:
            from app.core import llm_trace
            llm_trace.reset_traces(job_id)
        except Exception:  # noqa: BLE001 - trace cleanup must never block
            pass

    def _cleanup_job_memory(self, job_id: str, delay: float = 30.0) -> None:
        """Evict in-memory job dicts to prevent unbounded growth.

        ``delay=0`` purges immediately (cancel path). Non-zero delay gives the
        SSE terminal event a grace window to read progress/logs/status before
        eviction (completion/failure path). Thread backend uses the event
        loop's ``call_later``; process backend uses ``threading.Timer``.
        """
        if delay <= 0:
            self._purge_job_memory(job_id)
            return
        try:
            loop = _get_or_create_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.call_later(delay, self._purge_job_memory, job_id)
        else:
            timer = threading.Timer(delay, self._purge_job_memory, args=(job_id,))
            timer.daemon = True
            timer.start()

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def submit_job(
        self,
        job_id: str,
        filepath: str,
        config: dict[str, Any],
        marker_service: Any,
        claim: Any = None,
    ) -> None:
        """Start conversion via the selected backend and track the job.

        ``claim`` binds this execution to one kernel ownership generation
        (PR67B): the executor's terminal paths use exactly the claim they
        launched with, so a stale generation can never finalize under a
        successor's fence. Legacy callers omit it.
        """
        self._progress[job_id] = 10
        self._job_logs[job_id] = []
        self._job_status_text[job_id] = "Starting conversion..."
        self._job_start_time[job_id] = time.time()
        self._job_has_real_progress[job_id] = False
        self._job_started[job_id] = False
        self._job_providers[job_id] = config.get("llm_provider")
        # Clear any LLM call traces from a previous run of this job id.
        try:
            from app.core import llm_trace
            llm_trace.reset_traces(job_id)
        except Exception:  # noqa: BLE001 - trace reset must never block a job
            pass

        backend = self._select_backend(filepath, config, marker_service)
        queued_message = getattr(backend, "queued_message", "Waiting for conversion worker...")
        self._job_queued_message[job_id] = queued_message
        self._job_status_text[job_id] = queued_message
        if claim is not None:
            def _run_bound(
                _job_id=job_id,
                _filepath=filepath,
                _config=config,
                _service=marker_service,
                _claim=claim,
            ) -> dict[str, Any]:
                return self._run_conversion(
                    _job_id, _filepath, _config, _service, claim_ctx=_claim
                )

            run_job: Any = _run_bound
        else:
            run_job = self._run_conversion
        future = backend.submit(
            run_job,
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
                    self._kernel_claims.pop(job_id, None)
                    smooth = self._smooth_tasks.pop(job_id, None)
                    if smooth is not None:
                        smooth.cancel()
                    if isinstance(backend, ThreadExecutorBackend):
                        backend.pop(job_id)
                    self._cleanup_job_memory(job_id)
                    return
                exc = fut.exception()
                if exc:
                    logger.error("Job %s failed: %s", job_id, exc)
                self._tasks.pop(job_id, None)
                self._job_backends.pop(job_id, None)
                self._job_started.pop(job_id, None)
                self._job_queued_message.pop(job_id, None)
                self._kernel_claims.pop(job_id, None)
                smooth = self._smooth_tasks.pop(job_id, None)
                if smooth is not None:
                    smooth.cancel()
                if isinstance(backend, ThreadExecutorBackend):
                    backend.pop(job_id)
                self._cleanup_job_memory(job_id)

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

            loop = _get_or_create_event_loop()
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
        self._kernel_note_activity(job_id)

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
                if future.cancelled() is True:
                    status = "cancelled"
                    exc = None
                else:
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
        eta: int | None = None
        if status == "processing" and self._job_has_real_progress.get(job_id):
            if progress > 10 and progress < 100:
                estimated_total = elapsed * 100 / progress
                eta = max(1, int(estimated_total - elapsed))

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

        # Kernel authority first (PR67B): durably observe the cancellation
        # behind the current fence so a racing worker result cannot publish
        # or renew past it, then stop the executor below.
        kernel_cancelled = False
        if self._kernel_runtime is not None:
            try:
                kernel_cancelled = await self._kernel_runtime.prepare_cancel(job_id)
            except Exception:  # noqa: BLE001 - cancellation must never 500
                logger.exception("kernel cancel failed for job %s", job_id)
                kernel_cancelled = False

        if future and not future.done():
            self._cancel_requested.add(job_id)
            self._job_status_text[job_id] = "Cancellation requested..."
            was_started = self._job_started.get(job_id, False)
            future_cancelled = future.cancel()
            cancelled = True
            if future_cancelled and not was_started:
                self._progress.pop(job_id, None)
                self._tasks.pop(job_id, None)
                backend = self._job_backends.pop(job_id, None)
                if isinstance(backend, ThreadExecutorBackend):
                    backend.pop(job_id)
                self._cancel_requested.discard(job_id)
            self._job_has_real_progress.pop(job_id, None)
            if future_cancelled and not was_started:
                self._job_started.pop(job_id, None)
                self._job_queued_message.pop(job_id, None)
            smooth = self._smooth_tasks.pop(job_id, None)
            if smooth is not None:
                smooth.cancel()
        elif proc_state is not None:
            cancelled = True
            self._cancel_requested.add(job_id)
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

        if cancelled or kernel_cancelled:
            # Cancelled jobs don't need the SSE grace window — evict immediately.
            self._cleanup_job_memory(job_id, delay=0.0)
            pid = self._pids.pop(job_id, None)
            if pid is not None:
                self._kill_pid(pid)
            # Guarded write: work that reached accepted/completed truth
            # between the caller's read and here must not be overwritten.
            await self._update_job_status(job_id, "cancelled", only_if_active=True)
            await self._mark_job_terminal_durable(job_id, status="cancelled", message="Cancelled by user")
        return cancelled or kernel_cancelled

    def shutdown(self, wait: bool = False) -> None:
        """Stop the drain thread and release the executor/pool."""
        if self._kernel_runtime is not None:
            try:
                self._kernel_runtime.stop()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                logger.exception("kernel runtime stop failed")
        self._drain_stop.set()
        for smooth in list(self._smooth_tasks.values()):
            smooth.cancel()
        self._smooth_tasks.clear()
        self._cpu_backend.shutdown(wait=wait)
        for logger_name in ("marker", "app"):
            logging.getLogger(logger_name).removeHandler(self._log_handler)
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
        claim_ctx: Any = None,
    ) -> dict[str, Any]:
        """Runs inside ThreadPoolExecutor - updates DB on completion.

        ``claim_ctx`` is the kernel ownership generation this execution
        launched under; terminal paths carry it explicitly so a stale
        generation finishing late cannot finalize under its successor's
        claim (which a job_id-keyed lookup would hand it).
        """
        self._pids[job_id] = os.getpid()
        thread_ident = threading.get_ident()
        active_conversion_threads[thread_ident] = job_id
        # Record a durable lease so a crash mid-conversion is detectable by
        # recover_queued's expired-lease branch. No-op when no durable backend.
        try:
            self._run_async(self._mark_job_started_durable(job_id))
        except Exception:  # noqa: BLE001 - lease start must never block a job
            logger.exception("mark_started dispatch failed for job %s", job_id)
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
                (len(formats_requested) > 1 or formats_requested[0] != "markdown")
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

            if job_id in self._cancel_requested:
                self._progress[job_id] = 0
                self._job_status_text[job_id] = "Conversion cancelled."
                self._run_async(self._update_job_status(job_id, "cancelled", only_if_active=True))
                self._run_async(
                    self._mark_job_terminal_durable(
                        job_id,
                        status="cancelled",
                        message="Cancelled by user",
                    )
                )
                return {"cancelled": True}

            self._progress[job_id] = 90
            self._job_status_text[job_id] = "Finalizing results..."

            # Persist result synchronously via a new async loop. The claim
            # kwarg is passed only when this execution actually launched
            # under one, so patched legacy seams keep their signature.
            async def _finalize_coro() -> bool:
                if claim_ctx is not None:
                    return await self._finalize_job(
                        job_id, result, config, formats_envelopes, claim=claim_ctx
                    )
                return await self._finalize_job(job_id, result, config, formats_envelopes)

            projected = True
            projected = self._run_async(_finalize_coro())
            if not projected:
                # Acceptance was rejected (stale/conflict/cancelled): the
                # in-memory success view must not claim completion either.
                return result
            if job_id in self._cancel_requested:
                self._progress[job_id] = 0
                self._job_status_text[job_id] = "Conversion cancelled."
                return {"cancelled": True}
            self._progress[job_id] = 100
            self._job_status_text[job_id] = "Conversion completed successfully."
            return result
        except Exception as exc:
            if job_id in self._cancel_requested:
                self._progress[job_id] = 0
                self._job_status_text[job_id] = "Conversion cancelled."
                self._run_async(self._update_job_status(job_id, "cancelled", only_if_active=True))
                self._run_async(
                    self._mark_job_terminal_durable(
                        job_id,
                        status="cancelled",
                        message="Cancelled by user",
                    )
                )
                return {"cancelled": True}
            logger.exception("Conversion failed for job %s", job_id)
            self._progress[job_id] = 0
            self._job_status_text[job_id] = f"Conversion failed: {exc}"
            failure_message = str(exc)
            try:
                async def _fail_coro() -> None:
                    if claim_ctx is not None:
                        await self._fail_job(job_id, failure_message, claim=claim_ctx)
                    else:
                        await self._fail_job(job_id, failure_message)

                self._run_async(_fail_coro())
            except Exception:
                logger.exception("Failed to record error for job %s", job_id)
            raise
        finally:
            active_conversion_threads.pop(thread_ident, None)
            self._pids.pop(job_id, None)
            self._cancel_requested.discard(job_id)
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

    async def _update_job_status(
        self, job_id: str, status: str, *, only_if_active: bool = False
    ) -> None:
        """Write a job's status.

        ``only_if_active`` guards against overwriting terminal truth: the
        write is skipped when the row already reached a terminal status
        (e.g. a cancel racing an accepted completion).
        """
        async with async_session_factory() as session:
            from sqlalchemy import update

            stmt = update(ConversionJob).where(ConversionJob.id == job_id)
            if only_if_active:
                stmt = stmt.where(
                    ConversionJob.status.not_in(["completed", "failed", "cancelled"])
                )
            await session.execute(stmt.values(status=status))
            await session.commit()

    async def _read_job_status_with_commit_race_retry(
        self,
        job_id: str,
        *,
        action: str,
    ) -> Optional[str]:
        """Read a job's status, tolerating the commit-before-submit race.

        ``submit_job`` can start a fast CPU/native converter that reaches
        ``_finalize_job``/``_fail_job`` inside a worker thread before the
        request's ``get_db`` dependency commits the ``ConversionJob`` row.
        A single ``SELECT`` on a fresh session then returns ``None`` and the
        terminal write is silently skipped — the job hangs at ``pending``
        forever. The REST upload route now commits before submit, but we keep
        this retry as defense-in-depth so a future regression cannot lose a
        job silently.

        Bounded: a few short sleeps, total well under one second. If the row
        is still missing we return ``None`` and the caller logs a distinct
        WARNING (not the misleading "cancelled or deleted" message).
        """
        from sqlalchemy import select

        total_attempts = 6
        for attempt in range(total_attempts):
            async with async_session_factory() as session:
                stmt = select(ConversionJob.status).where(ConversionJob.id == job_id)
                res = await session.execute(stmt)
                status = res.scalar_one_or_none()
            if status is not None:
                return status
            # Last attempt: no sleep, just fall through to the missing-row log.
            if attempt < total_attempts - 1:
                await asyncio.sleep(0.1)

        logger.warning(
            "Job %s not visible after %s attempts; %s skipped (row not yet committed or deleted).",
            job_id,
            total_attempts,
            action,
        )
        return None

    async def _mark_job_started_durable(self, job_id: str) -> None:
        """Record a lease when a durable job begins executing.

        Without this the lease columns never get populated in production, so
        ``recover_queued``'s expired-lease branch (the one that detects jobs
        that crashed mid-conversion) can never match. Idempotent and a no-op
        when no durable backend is configured.
        """
        if self._durable_queue is None:
            return
        try:
            async with async_session_factory() as session:
                await self._durable_queue.mark_started(
                    session,
                    job_id=job_id,
                    lease_owner=f"task-manager:{os.getpid()}",
                    lease_seconds=self._lease_seconds,
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - lease bookkeeping must never break a job
            logger.exception("mark_started failed for durable job %s", job_id)

    async def _mark_job_terminal_durable(self, job_id: str, *, status: str, message: str | None = None) -> None:
        """Clear the lease and emit a terminal queue event for durable jobs.

        Called after the terminal UPDATE has already committed. Idempotent and
        a no-op when no durable backend is configured.
        """
        if self._durable_queue is None:
            return
        try:
            async with async_session_factory() as session:
                await self._durable_queue.mark_terminal(
                    session,
                    job_id=job_id,
                    status=status,
                    message=message,
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - lease bookkeeping must never break a job
            logger.exception("mark_terminal failed for durable job %s", job_id)

    async def _finalize_job(
        self,
        job_id: str,
        result: dict[str, Any],
        config: dict[str, Any],
        formats_payload: dict[str, dict[str, Any]] | None = None,
        claim: Any = None,
    ) -> bool:
        """Persist a completed conversion.

        Returns True when this call projected the job terminal-completed
        (legacy: always unless cancelled; kernel: only when the fenced
        accepted publication committed first). ``claim`` is the launching
        generation's claim; when omitted the current claim is looked up
        (process-backend drain path).
        """
        if job_id in self._cancel_requested:
            logger.info("Job %s was cancelled. Skipping finalization.", job_id)
            await self._update_job_status(job_id, "cancelled", only_if_active=True)
            await self._mark_job_terminal_durable(job_id, status="cancelled", message="Cancelled by user")
            return False

        # Check if job still exists and is not cancelled. Tolerate the
        # commit-before-submit race: the request transaction may still be
        # flushing when a fast converter reaches us in a worker thread.
        status = await self._read_job_status_with_commit_race_retry(
            job_id, action="finalization"
        )
        if status is None:
            return False
        if status == "cancelled":
            logger.info("Job %s was cancelled. Skipping finalization.", job_id)
            return False

        result_text = result.get("text", "")
        metadata = result.get("metadata") or {}
        result_metadata = _result_metadata_for_db(metadata)
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
            result_metadata["assets"] = _resolved_asset_entries_for_metadata(
                written.asset_entries,
                written.asset_paths,
            )
        result_metadata["manifest_path"] = str(written.manifest_path.resolve())
        result_metadata_json = json.dumps(result_metadata) if any(result_metadata.values()) else None

        # Cache every generated format's text in formats_json so the preview
        # tabs (markdown/html/json/chunks) can switch instantly with no
        # reconversion. The persisted primary file above already carries images
        # for its format; the cache stores text only to stay small and portable.
        formats_json = _formats_payload_for_finalize(result, output_format, formats_payload)

        # Fenced accepted publication (PR67B): durable output exists on disk
        # now, so build the bounded canonical descriptor and cross the PR66
        # acceptance boundary BEFORE the compatibility row may say completed.
        # ArtifactHandle transport was already resolved into real bytes
        # upstream, so acceptance describes durable output, never an
        # ephemeral handle pathname.
        if self._kernel_runtime is not None:
            active_claim = claim if claim is not None else self._kernel_claims.get(job_id)
            if active_claim is None:
                # Kernel dispatch is authoritative for this runtime: a
                # completion without its owning generation (zombie worker
                # after takeover/cancel) must never write terminal truth.
                logger.warning(
                    "Job %s completion arrived without a live kernel claim; "
                    "refusing to project it terminal-completed.",
                    job_id,
                )
                return False
            from app.services.kernel_runtime import build_result_descriptor

            try:
                formats_keys = list(json.loads(formats_json).keys()) if formats_json else []
            except (TypeError, ValueError):
                formats_keys = []
            descriptor = build_result_descriptor(
                job_id=job_id,
                output_format=output_format,
                result_text=result_text,
                formats_json=formats_json,
                result_metadata_json=result_metadata_json,
                final_path=written.final_path,
                manifest_path=written.manifest_path,
                asset_count=len(written.asset_paths),
                formats=formats_keys,
            )
            disposition = await self._kernel_runtime.accept_result(active_claim, descriptor)
            if disposition != "projected":
                # superseded / conflict / cancelled: an older generation's
                # late bytes must not become user-visible completion truth.
                logger.info(
                    "Job %s finalization not projected (kernel disposition=%s).",
                    job_id,
                    disposition,
                )
                return False

        async with async_session_factory() as session:
            from sqlalchemy import update

            update_result = await session.execute(
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
                    # Clear lease columns on terminal so recover_queued never
                    # sees a completed durable job as "stuck".
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await session.commit()
            if (update_result.rowcount or 0) < 1:
                logger.info("Job %s was cancelled during finalization. Skipping completed terminal mark.", job_id)
                return False
        await self._mark_job_terminal_durable(job_id, status="completed")
        return True

    async def _fail_job(self, job_id: str, error_message: str, claim: Any = None) -> None:
        if job_id in self._cancel_requested:
            logger.info("Job %s was cancelled. Skipping failure recording.", job_id)
            await self._update_job_status(job_id, "cancelled", only_if_active=True)
            await self._mark_job_terminal_durable(job_id, status="cancelled", message="Cancelled by user")
            return

        # Kernel authority (PR67B): a failed execution ends through the
        # coordinator — retry budget, fenced release, durable work.failed
        # event, and the row projection all live behind one decision. The
        # failing generation's own claim is used: a stale generation must
        # not consume its successor's retry budget or terminal-fail its
        # live work.
        if self._kernel_runtime is not None:
            active_claim = claim if claim is not None else self._kernel_claims.get(job_id)
            if active_claim is None:
                logger.warning(
                    "Job %s failure arrived without a live kernel claim; "
                    "the owning generation owns the row projection.",
                    job_id,
                )
                return
            outcome = await self._kernel_runtime.fail_execution(active_claim, error_message)
            logger.info(
                "Job %s failure recorded through kernel authority (%s).", job_id, outcome
            )
            return

        # Same commit-race tolerance as _finalize_job: a fast converter can
        # reach the failure path before the request session commits the row.
        status = await self._read_job_status_with_commit_race_retry(
            job_id, action="failure recording"
        )
        if status is None:
            return
        if status == "cancelled":
            logger.info("Job %s was cancelled. Skipping failure recording.", job_id)
            return

        # Only mark as failed if not already in a terminal state (e.g. cancelled)
        async with async_session_factory() as session:
            from sqlalchemy import update
            update_result = await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status != "cancelled")
                .values(
                    status="failed",
                    error_message=error_message,
                    completed_at=datetime.now(timezone.utc),
                    # Clear lease columns on terminal so recover_queued never
                    # sees a failed durable job as "stuck".
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await session.commit()
            if (update_result.rowcount or 0) < 1:
                logger.info("Job %s was cancelled during failure recording. Skipping failed terminal mark.", job_id)
                return
        await self._mark_job_terminal_durable(job_id, status="failed", message=error_message)

