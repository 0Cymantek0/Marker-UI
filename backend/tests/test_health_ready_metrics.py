"""Diagnostics endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import ConversionJob
from app.services import telemetry


@pytest.mark.asyncio
async def test_healthz_version_and_request_id(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(telemetry, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("MARKER_VERSION", "9.8.7-test")
    monkeypatch.setenv("MARKER_COMMIT_SHA", "abc123")

    health = await client.get("/api/healthz", headers={"X-Request-ID": "req-test-1"})
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["X-Request-ID"] == "req-test-1"

    ready = await client.get("/api/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    version = await client.get("/api/version")
    assert version.status_code == 200
    assert version.json() == {
        "name": "marker-ui-api",
        "version": "9.8.7-test",
        "commit": "abc123",
    }


@pytest.mark.asyncio
async def test_security_headers_block_external_preview_images(client: AsyncClient):
    response = await client.get("/api/healthz")

    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "img-src 'self' data: blob:" in csp
    assert "https:" not in csp.split("img-src", 1)[1].split(";", 1)[0]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.asyncio
async def test_readyz_fails_when_output_dir_unavailable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    missing_output = tmp_path / "missing" / "output"
    monkeypatch.setattr(telemetry, "OUTPUT_DIR", missing_output)

    response = await client.get("/api/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["output_dir"]["ok"] is False


@pytest.mark.asyncio
async def test_readyz_fails_when_database_unavailable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(telemetry, "OUTPUT_DIR", tmp_path)

    async def fail_database(_db):
        raise RuntimeError("database offline")

    monkeypatch.setattr(telemetry, "check_database_ready", fail_database)

    response = await client.get("/api/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False
    assert "database offline" in body["checks"]["database"]["error"]
    assert body["checks"]["output_dir"]["ok"] is True


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_job_counters_when_enabled(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(telemetry, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("MARKER_ENABLE_METRICS", "true")
    db_session.add_all(
        [
            ConversionJob(
                id="11111111-1111-4111-8111-111111111111",
                filename="done.tsv",
                original_name="done.tsv",
                status="completed",
                input_format="tsv",
                output_format="markdown",
            ),
            ConversionJob(
                id="22222222-2222-4222-8222-222222222222",
                filename="failed.tsv",
                original_name="failed.tsv",
                status="failed",
                input_format="tsv",
                output_format="markdown",
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/api/metrics")

    assert response.status_code == 200
    text = response.text
    assert 'marker_jobs_total{status="completed"} 1' in text
    assert 'marker_jobs_total{status="failed"} 1' in text
    assert "marker_jobs_total 2" in text


@pytest.mark.asyncio
async def test_metrics_endpoint_is_disabled_by_default(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MARKER_ENABLE_METRICS", raising=False)

    response = await client.get("/api/metrics")

    assert response.status_code == 404
