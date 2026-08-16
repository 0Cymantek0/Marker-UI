"""Marker UI FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()  # Load .env file if present

from app.core.config import OUTPUT_DIR, UPLOAD_DIR  # noqa: E402
from app.models.audit import AuditEvent  # noqa: E402, F401 - register table metadata
from app.models.job_event import JobEvent  # noqa: E402, F401 - register table metadata
from app.routes import capabilities, convert, diagnostics, models, settings  # noqa: E402
from app.security.auth import RestAuthMiddleware  # noqa: E402
from app.security.headers import SecurityHeadersMiddleware  # noqa: E402
from app.services.conversion_service import ConversionService  # noqa: E402
from app.services.marker_service import MarkerService  # noqa: E402
from app.services.queue_backends import queue_backend_from_env  # noqa: E402
from app.services.task_manager import TaskManager  # noqa: E402
from app.services.telemetry import RequestContextMiddleware  # noqa: E402

logger = logging.getLogger(__name__)


class _AppState:
    """Holds long-lived service instances for the running app."""

    def __init__(self) -> None:
        self.marker_service: MarkerService = MarkerService()
        self.conversion_service: ConversionService = ConversionService(self.marker_service)
        self.task_manager: TaskManager = TaskManager(durable_queue=queue_backend_from_env())


_app_state = _AppState()

_bg_load_thread: threading.Thread | None = None
_bg_load_thread_lock = threading.Lock()


def _load_models_background() -> None:
    global _bg_load_thread
    from app.services.model_tracker import tracker

    # If already initialized, do nothing
    if tracker.get_status_dict()["initialized"]:
        return

    with _bg_load_thread_lock:
        if _bg_load_thread and _bg_load_thread.is_alive():
            tracker.request_cancel()
            _bg_load_thread.join(timeout=5.0)

        # Reset tracker state for a fresh session
        tracker.reset()

        def _worker() -> None:
            from app.services.model_tracker import check_models_downloaded, setup_monkeypatch, initialize_all_model_metadata
            t0 = time.perf_counter()
            try:
                # Apply download tracker monkeypatching in the background thread
                setup_monkeypatch()

                # If already downloaded, set to loading state
                if check_models_downloaded():
                    tracker.set_loading(True)
                else:
                    # Initialize model metadata for segment progress tracking
                    initialize_all_model_metadata()

                _app_state.marker_service.initialize()
                logger.info(
                    "MarkerService initialised in %.1f s", time.perf_counter() - t0
                )
            except Exception as exc:
                if tracker.cancel_requested:
                    tracker.set_cancelled()
                    logger.info("MarkerService initialization cancelled by user.")
                else:
                    tracker.set_failed(str(exc))
                    logger.warning(
                        "MarkerService could not load models - conversion endpoints will "
                        "retry lazily on first request.",
                        exc_info=True,
                    )

        _bg_load_thread = threading.Thread(target=_worker, daemon=True)
        _bg_load_thread.start()



def _configure_task_manager_backend() -> None:
    """Swap the default thread TaskManager for a process-pool one when >1 GPU.

    The multi-GPU process backend spawns one worker per detected GPU, each pinned
    to a device. Single-GPU / CPU-only keeps the default thread backend (the
    original single-process behavior), so the heavier spawn path only runs when
    there is genuinely more than one GPU to fan out across.
    """
    try:
        from app.core.gpu import detect_gpus
        from app.routes.settings import (
            _load_gpu_worker_rows,
            _read_gpu_worker_settings,
            get_effective_worker_count,
        )
        from app.database import async_session_factory

        detected = detect_gpus()
        if detected <= 1:
            logger.info("GPU worker scaling: %d GPU(s) -> thread backend", detected)
            return

        import asyncio

        async def _read() -> None:
            async with async_session_factory() as session:
                rows = await _load_gpu_worker_rows(session)
            return rows

        try:
            rows = asyncio.get_event_loop().run_until_complete(_read())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                rows = loop.run_until_complete(_read())
            finally:
                loop.close()

        mode, manual_count = _read_gpu_worker_settings(rows)
        num_workers = get_effective_worker_count(mode, manual_count, detected)
        if num_workers <= 1:
            logger.info("GPU worker scaling: resolved to 1 worker -> thread backend")
            return

        from app.services.task_manager import ProcessExecutorBackend, TaskManager

        backend = ProcessExecutorBackend(_app_state.task_manager, detected, num_workers)
        old = _app_state.task_manager
        # Preserve the durable queue across the backend swap: the new manager
        # must keep persisting/recovering durable jobs or a multi-GPU box silently
        # loses the feature (MARKER_QUEUE_BACKEND would be ignored).
        _app_state.task_manager = TaskManager(
            backend=backend,
            durable_queue=getattr(old, "_durable_queue", None),
        )
        logger.info(
            "GPU worker scaling: %d GPUs, %d workers -> process backend",
            detected, num_workers,
        )
        # The old default thread manager is unused; leave it (its pool is empty).
        del old
    except Exception:  # noqa: BLE001 - never let backend selection break startup
        logger.exception("GPU worker scaling: failed to configure process backend, falling back to threads")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Startup: initialise models & tables. Shutdown: cleanup."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Ensure data dirs
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Runtime gate: Alembic owns persistent schema. Startup validates
    # compatibility and refuses to serve against an unproven database;
    # it never creates or repairs schema (see app.db_migration).
    from app.db_migration import verify_database_ready
    await verify_database_ready()

    # Load secrets cache and register live API interceptor monkeypatch
    from app.core.api_manager import load_secrets_from_db, setup_api_manager_monkeypatch
    from app.routes.settings import init_llm_providers_if_missing
    from app.database import async_session_factory
    async with async_session_factory() as session:
        await init_llm_providers_if_missing(session)
    await load_secrets_from_db()
    setup_api_manager_monkeypatch()

    # Runtime authority split (PR67B):
    # * kernel mode (default) — pick the executor backend FIRST (the
    #   coordinator binds to the surviving TaskManager), then reconcile
    #   startup state through the kernel authority: dispatch repair, lost
    #   acknowledgements, accepted-publication projection, legacy durable
    #   row adoption, and the abandoned-row sweep all happen there. The
    #   legacy durable-queue resubmission path is intentionally not run —
    #   two schedulers deciding ownership for one job is exactly the race
    #   this integration closes.
    # * legacy mode — recover durable jobs, sweep the rest, then select
    #   the backend (historical order).
    from app.core.config import KERNEL_RUNTIME_ENABLED

    kernel_started = False
    if KERNEL_RUNTIME_ENABLED:
        _configure_task_manager_backend()
        try:
            from app.services.task_manager import TaskManager
            if isinstance(_app_state.task_manager, TaskManager):
                coordinator = _app_state.task_manager.start_kernel_runtime(
                    _app_state.conversion_service
                )
                try:
                    report = await coordinator.recover()
                    summary = {
                        key: len(value) if isinstance(value, list) else value
                        for key, value in report.items()
                        if value
                    }
                    logger.info(
                        "Kernel runtime reconciliation: %s",
                        summary if summary else "nothing to reconcile",
                    )
                except Exception:  # noqa: BLE001 - recovery must never block startup
                    logger.exception(
                        "Kernel runtime reconciliation failed on startup; the "
                        "dispatch loop still runs and the next restart reconciles"
                    )
                try:
                    coordinator.start()
                    kernel_started = True
                except Exception:  # noqa: BLE001 - fall back to the legacy runtime
                    logger.exception(
                        "Kernel runtime dispatch loop failed to start; "
                        "unbinding coordinator so submissions fall back to "
                        "the legacy runtime"
                    )
                    _app_state.task_manager._kernel_runtime = None
        except Exception:  # noqa: BLE001 - never let startup break here
            logger.exception("Kernel runtime initialization failed on startup")
    else:
        # Recover durable queued jobs from a prior session, then sweep any remaining
        # non-durable pending/processing rows as failed. Order matters: recovery must
        # run first so durable rows survive; the sweep only catches non-durable rows
        # that have no queue backend to recover from.
        try:
            from app.services.task_manager import TaskManager
            if isinstance(_app_state.task_manager, TaskManager):
                recovery = await _app_state.task_manager.recover_and_sweep_durable_jobs(
                    _app_state.conversion_service
                )
                recovered_ids = recovery.get("recovered", [])
                swept_ids = recovery.get("swept", [])
                if recovered_ids or swept_ids:
                    logger.info(
                        "Durable job reconciliation: recovered %d job(s) %s, swept %d stale job(s) %s",
                        len(recovered_ids),
                        recovered_ids,
                        len(swept_ids),
                        swept_ids,
                    )
                else:
                    logger.info("Durable job reconciliation: nothing to recover or sweep.")
        except Exception:  # noqa: BLE001 - recovery must never block startup
            logger.exception("Durable job recovery failed on startup; continuing with stale sweep only")
            # Fall back to the legacy unconditional sweep so non-durable rows still
            # get cleaned up even if the durable recovery path errored.
            from app.database import async_session_factory
            from app.models.job import ConversionJob
            from sqlalchemy import update
            from datetime import datetime, timezone
            async with async_session_factory() as session:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.status.in_(["pending", "processing"]))
                    .where(ConversionJob.queue_backend.is_(None))
                    .values(
                        status="failed",
                        error_message="Interrupted by server restart",
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                logger.info("Fallback: non-durable stale pending/processing jobs marked as failed.")

    # Auto-trigger GPU installation if enabled in settings but CUDA is not ready
    from app.services.gpu_service import gpu_service
    from app.models.settings import Setting
    from sqlalchemy import select
    async with async_session_factory() as session:
        try:
            stmt = select(Setting).where(Setting.key == "gpu_acceleration_enabled")
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row and row.value == "true" and not gpu_service.status_dict["cuda_available"]:
                logger.info("GPU acceleration enabled but CUDA not available. Starting background installation...")
                gpu_service.start_install()
        except Exception as e:
            logger.error("Failed to auto-trigger GPU installation: %s", e)

    # Select the conversion backend based on detected GPUs + worker settings.
    # Multi-GPU -> process pool (one worker per GPU). Single/CPU -> threads.
    # Kernel mode already selected the backend before binding the runtime
    # coordinator; selecting again would rebuild the TaskManager the
    # coordinator is bound to.
    if not kernel_started:
        _configure_task_manager_backend()

    # Register download tracker retry callback
    from app.services.model_tracker import register_retry_callback
    register_retry_callback(_load_models_background)

    # Phase 1 lazy init: marker models load on first marker job, not at startup.
    # An office-only deployment should never pay the multi-GB cold start.
    # Set MARKER_PRELOAD_MODELS=true to restore eager startup loading.
    from app.core.config import PRELOAD_MARKER_MODELS
    if PRELOAD_MARKER_MODELS:
        _load_models_background()
    else:
        logger.info(
            "Marker model prewarming disabled (MARKER_PRELOAD_MODELS=false); "
            "models will lazy-load on first marker job."
        )

    # Periodic background sweep for the opt-in LLM response cache.
    # purge_expired() is a no-op when MARKER_LLM_CACHE != 1, so the loop is
    # harmless on deployments that don't use the cache.
    from app.core import llm_cache

    async def _llm_cache_sweep() -> None:
        while True:
            await asyncio.sleep(6 * 3600)  # every 6 hours
            try:
                purged = llm_cache.purge_expired()
                if purged:
                    logger.info("LLM cache sweep: removed %d expired entries", purged)
            except Exception:  # noqa: BLE001 - sweep must never crash the app
                logger.exception("LLM cache sweep failed")

    _cache_sweep_task = asyncio.create_task(_llm_cache_sweep())

    yield

    # Shutdown
    _cache_sweep_task.cancel()
    _app_state.task_manager.shutdown(wait=False)
    logger.info("Shutdown complete")


app = FastAPI(
    title="Marker UI API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - allow Vite dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RestAuthMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# Routers
app.include_router(diagnostics.router)
app.include_router(convert.router)
app.include_router(settings.router)
app.include_router(models.router)
app.include_router(capabilities.router)



@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Lightweight liveness probe."""
    return {"status": "ok"}
