"""Health, readiness, version, and metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import telemetry

router = APIRouter(prefix="/api", tags=["diagnostics"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    report = await telemetry.readiness_report(db)
    return JSONResponse(
        status_code=200 if report["status"] == "ready" else 503,
        content=report,
    )


@router.get("/version")
async def version() -> dict[str, str | None]:
    return telemetry.version_payload()


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
    if not telemetry.metrics_enabled():
        raise HTTPException(status_code=404, detail="Metrics endpoint is disabled")
    return PlainTextResponse(await telemetry.render_metrics(db), media_type="text/plain; version=0.0.4")
