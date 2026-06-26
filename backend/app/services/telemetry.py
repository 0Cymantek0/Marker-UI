"""Diagnostics, request context, and lightweight metrics helpers."""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.config import OUTPUT_DIR
from app.models.job import ConversionJob

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach request_id to state, response headers, and structured logs."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            path = request.url.path
            job_id = request.path_params.get("job_id") or request.query_params.get("job_id")
            logger.info(
                "request_complete",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "status_code": status_code,
                    "elapsed_ms": elapsed_ms,
                    "job_id": job_id,
                },
            )
            response = locals().get("response")
            if response is not None:
                response.headers[REQUEST_ID_HEADER] = request_id


async def readiness_report(db: AsyncSession, *, output_dir: Path | None = None) -> dict[str, object]:
    checks: dict[str, dict[str, object]] = {}
    checks["database"] = await _check("database", check_database_ready(db))
    checks["output_dir"] = await _check("output_dir", check_output_dir_ready(output_dir or OUTPUT_DIR))
    ready = all(check["ok"] for check in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}


async def check_database_ready(db: AsyncSession) -> None:
    await db.execute(text("SELECT 1"))


async def check_output_dir_ready(output_dir: Path) -> None:
    if not output_dir.exists():
        raise RuntimeError(f"Output directory does not exist: {output_dir}")
    if not output_dir.is_dir():
        raise RuntimeError(f"Output path is not a directory: {output_dir}")
    probe = output_dir / f".marker-ready-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)


def metrics_enabled() -> bool:
    return os.getenv("MARKER_ENABLE_METRICS", "false").lower() in {"1", "true", "yes", "on"}


async def render_metrics(db: AsyncSession) -> str:
    rows = (
        await db.execute(
            select(ConversionJob.status, func.count(ConversionJob.id)).group_by(ConversionJob.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    total = sum(counts.values())
    lines = [
        "# HELP marker_jobs_total Conversion jobs by status.",
        "# TYPE marker_jobs_total gauge",
    ]
    for status in sorted(counts):
        lines.append(f'marker_jobs_total{{status="{status}"}} {counts[status]}')
    lines.extend(
        [
            f"marker_jobs_total {total}",
            "# HELP marker_metrics_enabled Marker metrics endpoint enabled flag.",
            "# TYPE marker_metrics_enabled gauge",
            "marker_metrics_enabled 1",
            "",
        ]
    )
    return "\n".join(lines)


def version_payload() -> dict[str, str | None]:
    return {
        "name": "marker-ui-api",
        "version": os.getenv("MARKER_VERSION", "0.1.0"),
        "commit": os.getenv("MARKER_COMMIT_SHA") or None,
    }


async def _check(name: str, awaitable) -> dict[str, object]:
    try:
        await awaitable
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - readiness reports failure reason.
        logger.warning("Readiness check failed: %s: %s", name, exc)
        return {"ok": False, "error": str(exc)}
