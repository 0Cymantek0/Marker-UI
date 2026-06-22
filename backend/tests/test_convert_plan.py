"""Tests for /api/convert/plan endpoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import pytest
from httpx import AsyncClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def _write_digital_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    for idx in range(30):
        c.drawString(72, 740 - idx * 20, f"Plan endpoint digital text line {idx + 1}.")
    c.showPage()
    c.save()


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
    """Filename-only PDF plan is conservative and marked preliminary."""
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
    assert plan["preliminary"] is True
    assert any("Preliminary" in warning for warning in plan["warnings"])


@pytest.mark.asyncio
async def test_convert_plan_local_pdf_applies_phase5_backend_knobs(
    client: AsyncClient,
    tmp_path: Path,
):
    pdf = tmp_path / "clean.pdf"
    _write_digital_pdf(pdf)

    fast_resp = await client.post(
        "/api/convert/plan",
        json={
            "filename": "clean.pdf",
            "size": pdf.stat().st_size,
            "local_filepath": str(pdf),
        },
    )
    assert fast_resp.status_code == 200
    assert fast_resp.json()["engine"] == "liteparse_pdf"

    accurate_resp = await client.post(
        "/api/convert/plan",
        json={
            "filename": "clean.pdf",
            "size": pdf.stat().st_size,
            "local_filepath": str(pdf),
            "conversion_profile": "high_accuracy",
        },
    )
    assert accurate_resp.status_code == 200
    plan = accurate_resp.json()
    assert plan["engine"] == "marker_pdf"
    assert any("High Accuracy" in reason for reason in plan["reasons"])
    assert plan["preliminary"] is False
    assert plan["probe_result"]["recommended_engine"] == "liteparse"


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
