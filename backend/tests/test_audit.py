"""Tests for redacted audit events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent


async def _audit_events(db_session: AsyncSession) -> list[AuditEvent]:
    result = await db_session.execute(select(AuditEvent).order_by(AuditEvent.created_at))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_settings_write_emits_redacted_audit_event(
    client: AsyncClient,
    db_session: AsyncSession,
):
    sensitive_value = "dummy-openai-key-for-audit-redaction-1234567890"

    response = await client.put(
        "/api/settings/",
        json={"key": "openai_api_key", "value": sensitive_value, "category": "llm"},
    )

    assert response.status_code == 200
    events = await _audit_events(db_session)
    settings_events = [event for event in events if event.event_type == "settings.write"]
    assert len(settings_events) == 1
    event = settings_events[0]
    assert event.resource_type == "setting"
    assert event.resource_id == "openai_api_key"
    payload = json.loads(event.redacted_payload_json)
    assert payload == {
        "category": "llm",
        "key": "openai_api_key",
        "operation": "create",
        "sensitive": True,
    }
    serialized = json.dumps([event.redacted_payload_json for event in events])
    assert sensitive_value not in serialized


@pytest.mark.asyncio
async def test_source_url_blocked_emits_redacted_audit_event(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )

    response = await client.post(
        "/api/convert/upload",
        params={"source_url": "https://example.com/private.pdf?token=must-not-log"},
    )

    assert response.status_code == 400
    events = await _audit_events(db_session)
    blocked = [event for event in events if event.event_type == "url_fetch.blocked"]
    assert len(blocked) == 1
    event = blocked[0]
    assert event.status == "denied"
    payload = json.loads(event.redacted_payload_json)
    assert payload["url"] == "https://example.com/private.pdf"
    assert payload["category"] == "unsafe"
    serialized = json.dumps([event.redacted_payload_json for event in events])
    assert "must-not-log" not in serialized


@pytest.mark.asyncio
async def test_upload_emits_job_submitted_audit_event(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
):
    source = tmp_path / "table.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")

    with source.open("rb") as handle:
        response = await client.post(
            "/api/convert/upload",
            files={"file": ("table.tsv", handle, "text/tab-separated-values")},
            params={"output_format": "markdown"},
        )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    events = await _audit_events(db_session)
    job_events = [event for event in events if event.event_type == "job.submitted"]
    assert len(job_events) == 1
    event = job_events[0]
    assert event.resource_type == "job"
    assert event.resource_id == job_id
    payload = json.loads(event.redacted_payload_json)
    assert payload["input_format"] == "tsv"
    assert payload["output_format"] == "markdown"
    assert payload["source"] == "upload"
