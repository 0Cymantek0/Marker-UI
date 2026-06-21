"""Tests for /api/convert/plan endpoint."""

from __future__ import annotations

from unittest.mock import patch
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_convert_plan_docx_fallback_without_registration(client: AsyncClient):
    """Predicting plan for a docx file falls back to marker_pdf if office_docx is not registered."""
    from app.main import _app_state
    with patch.object(_app_state.conversion_service.registry, "has", return_value=False):
        resp = await client.post(
            "/api/convert/plan",
            json={"filename": "my_report.docx", "size": 102400},
        )
        assert resp.status_code == 200
        plan = resp.json()
        assert plan["engine"] == "marker_pdf"
        assert "no converter registered" in plan["label"]
        assert plan["needs_marker_models"] is True
        assert plan["fallback_chain"] == ["office_docx", "marker_pdf"]
        assert len(plan["warnings"]) > 0


@pytest.mark.asyncio
async def test_convert_plan_docx_with_registration(client: AsyncClient):
    """Predicting plan for a docx file selects office_docx if registered."""
    from app.main import _app_state
    
    # Temporarily register/mock that registry has office_docx
    with patch.object(_app_state.conversion_service.registry, "has", return_value=True):
        resp = await client.post(
            "/api/convert/plan",
            json={"filename": "my_report.docx", "size": 102400},
        )
        assert resp.status_code == 200
        plan = resp.json()
        assert plan["engine"] == "office_docx"
        assert plan["label"] == "Fast Office (Word)"
        assert plan["needs_marker_models"] is False
        assert plan["needs_gpu"] is False
        assert plan["fallback_chain"] == []


@pytest.mark.asyncio
async def test_convert_plan_pdf(client: AsyncClient):
    """Predicting plan for a pdf file selects marker_pdf."""
    resp = await client.post(
        "/api/convert/plan",
        json={"filename": "document.pdf", "size": 5242880},
    )
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["engine"] == "marker_pdf"
    assert plan["label"] == "Marker PDF"
    assert plan["needs_marker_models"] is True
    assert plan["needs_gpu"] is True


@pytest.mark.asyncio
async def test_convert_plan_unknown_extension(client: AsyncClient):
    """Predicting plan for unknown extension falls back to marker_pdf with warnings."""
    resp = await client.post(
        "/api/convert/plan",
        json={"filename": "mystery.xyz", "size": 4096},
    )
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["engine"] == "marker_pdf"
    assert plan["needs_marker_models"] is True
    assert len(plan["warnings"]) > 0
    assert "No dedicated converter for '.xyz'" in plan["warnings"][0]
