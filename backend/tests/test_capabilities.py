"""Tests for /api/capabilities endpoint."""

from __future__ import annotations

from unittest.mock import patch
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_capabilities_returns_200(client: AsyncClient):
    """Querying capabilities returns 200 and all engines."""
    resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    
    body = resp.json()
    assert "engines" in body
    engines = body["engines"]
    
    # Assert standard engines are represented
    assert "marker_pdf" in engines
    assert "office_docx" in engines
    assert "office_pptx" in engines
    assert "spreadsheet" in engines
    assert "text_data" in engines
    assert "html" in engines
    
    # Since text_data needs no extra dependencies, it must be "ready"
    assert engines["text_data"] == "ready"


@pytest.mark.asyncio
async def test_get_capabilities_with_mocked_dependencies(client: AsyncClient):
    """Capabilities endpoint responds correctly to missing dependencies."""
    with patch("app.conversion.dependencies.is_dependency_available") as mock_dep:
        # Mock dependencies so office_docx looks ready but office_pptx looks missing
        def side_effect(name):
            if name in ("mammoth", "markdownify"):
                return True
            if name == "python-pptx":
                return False
            return True

        mock_dep.side_effect = side_effect

        resp = await client.get("/api/capabilities")
        assert resp.status_code == 200
        engines = resp.json()["engines"]


@pytest.mark.asyncio
async def test_marker_pdf_ready_when_models_are_downloaded_but_lazy_init_not_started(client: AsyncClient):
    """Downloaded Marker models are conversion-ready even before lazy initialization."""
    tracker_status = {
        "initialized": False,
        "loading": False,
        "overall": {"status": "pending"},
    }

    with (
        patch("app.services.model_tracker.tracker.get_status_dict", return_value=tracker_status),
        patch("app.services.model_tracker.check_models_downloaded", return_value=True),
    ):
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    assert resp.json()["engines"]["marker_pdf"] == "ready"


@pytest.mark.asyncio
async def test_marker_pdf_reports_downloading_when_tracker_is_active(client: AsyncClient):
    """Active model setup should remain visible as a downloading capability state."""
    tracker_status = {
        "initialized": False,
        "loading": True,
        "overall": {"status": "loading"},
    }

    with (
        patch("app.services.model_tracker.tracker.get_status_dict", return_value=tracker_status),
        patch("app.services.model_tracker.check_models_downloaded", return_value=False),
    ):
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    assert resp.json()["engines"]["marker_pdf"] == "models_downloading"
