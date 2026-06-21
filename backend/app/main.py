"""Marker UI FastAPI application."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # Load .env file if present

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import UPLOAD_DIR, OUTPUT_DIR
from app.database import create_tables
from app.routes import convert, settings, models, capabilities
from app.services.marker_service import MarkerService
from app.services.task_manager import TaskManager
from app.services.conversion_service import ConversionService

logger = logging.getLogger(__name__)


class _AppState:
    """Holds long-lived service instances for the running app."""

    def __init__(self) -> None:
        self.marker_service: MarkerService = MarkerService()
        self.conversion_service: ConversionService = ConversionService(self.marker_service)
        self.task_manager: TaskManager = TaskManager()


_app_state = _AppState()


import threading

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
        from app.models.schemas import GPUWorkerMode

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
        _app_state.task_manager = TaskManager(backend=backend)
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

    # Create DB tables
    await create_tables()

    # Load secrets cache and register live API interceptor monkeypatch
    from app.core.api_manager import load_secrets_from_db, setup_api_manager_monkeypatch
    from app.routes.settings import init_llm_providers_if_missing
    from app.database import async_session_factory
    async with async_session_factory() as session:
        await init_llm_providers_if_missing(session)
    await load_secrets_from_db()
    setup_api_manager_monkeypatch()

    # Mark stale pending/processing jobs from previous sessions as failed
    from app.database import async_session_factory
    from app.models.job import ConversionJob
    from sqlalchemy import update
    from datetime import datetime, timezone
    async with async_session_factory() as session:
        try:
            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.status.in_(["pending", "processing"]))
                .values(
                    status="failed",
                    error_message="Interrupted by server restart",
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
            logger.info("Stale pending/processing jobs from prior session marked as failed.")
        except Exception as e:
            logger.error("Failed to clean up stale jobs on startup: %s", e)

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
    _configure_task_manager_backend()

    # Register download tracker retry callback
    from app.services.model_tracker import register_retry_callback
    register_retry_callback(_load_models_background)

    _load_models_background()

    yield

    # Shutdown
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

# Routers
app.include_router(convert.router)
app.include_router(settings.router)
app.include_router(models.router)
app.include_router(capabilities.router)



@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Lightweight liveness probe."""
    return {"status": "ok"}
