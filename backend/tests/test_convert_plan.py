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
async def test_convert_plan_docx_without_registration_stays_native(client: AsyncClient):
    """Predicting plan for docx never suggests incompatible Marker fallback."""
    from app.main import _app_state
    with patch.object(_app_state.conversion_service.registry, "has", return_value=False):
        resp = await client.post(
            "/api/convert/plan",
            json={"filename": "my_report.docx", "size": 102400},
        )
        assert resp.status_code == 200
        plan = resp.json()
        assert plan["engine"] == "office_docx"
        assert plan["label"] == "Fast Office (Word)"
        assert plan["needs_marker_models"] is False
        assert plan["fallback_chain"] == []
        assert "office_docx" in plan["warnings"][0]
        assert plan["output_formats"] == ["markdown", "chunks"]


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
async def test_convert_plan_rejects_incompatible_engine_override(client: AsyncClient):
    resp = await client.post(
        "/api/convert/plan",
        json={
            "filename": "my_report.docx",
            "size": 102400,
            "engine_override": "marker_pdf",
        },
    )

    assert resp.status_code == 400
    assert "engine_override 'marker_pdf' is incompatible with extension '.docx'" in resp.json()["detail"]


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
    assert plan["output_formats"] == ["markdown", "json", "html", "chunks"]
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
    fast_plan = fast_resp.json()
    assert fast_plan["engine"] == "liteparse_pdf"
    assert fast_plan["output_formats"] == ["markdown", "chunks"]

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
async def test_convert_plan_local_pdf_reports_mixed_segments(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pdf = tmp_path / "mixed.pdf"
    pdf.write_bytes(b"%PDF")
    probe_payload = {
        "page_count": 3,
        "text_layer_score": 0.5,
        "text_quality_score": 0.7,
        "scan_likelihood": 0.5,
        "sandwich_likelihood": 0.5,
        "layout_complexity_score": 0.0,
        "visual_complexity_score": 0.5,
        "recommended_engine": "marker",
        "reasons": ["mixed page risk"],
        "sampled_image_count": 1,
        "page_results": [
            {
                "page_number": 1,
                "text_layer_score": 0.9,
                "text_quality_score": 1.0,
                "scan_likelihood": 0.0,
                "sandwich_likelihood": 0.0,
                "layout_complexity_score": 0.0,
                "visual_complexity_score": 0.0,
                "recommended_engine": "liteparse",
                "reasons": ["LiteParse fast path is safe"],
                "text_chars": 1000,
                "image_count": 0,
                "full_page_image": False,
            },
            {
                "page_number": 2,
                "text_layer_score": 0.0,
                "text_quality_score": 0.0,
                "scan_likelihood": 1.0,
                "sandwich_likelihood": 0.8,
                "layout_complexity_score": 0.0,
                "visual_complexity_score": 0.9,
                "recommended_engine": "marker",
                "reasons": ["scan likelihood is high"],
                "text_chars": 0,
                "image_count": 1,
                "full_page_image": True,
            },
            {
                "page_number": 3,
                "text_layer_score": 0.0,
                "text_quality_score": 0.0,
                "scan_likelihood": 1.0,
                "sandwich_likelihood": 0.8,
                "layout_complexity_score": 0.0,
                "visual_complexity_score": 0.9,
                "recommended_engine": "marker",
                "reasons": ["scan likelihood is high"],
                "text_chars": 0,
                "image_count": 1,
                "full_page_image": True,
            },
        ],
    }

    class _FakeProbe:
        def to_dict(self):
            return probe_payload

    probe_kwargs = []

    def _fake_probe(_path, **kwargs):
        probe_kwargs.append(kwargs)
        return _FakeProbe()

    monkeypatch.setattr("app.routes.convert.probe_pdf", _fake_probe)

    resp = await client.post(
        "/api/convert/plan",
        json={
            "filename": "mixed.pdf",
            "size": pdf.stat().st_size,
            "local_filepath": str(pdf),
            "enable_mixed_pdf_routing": True,
        },
    )

    assert resp.status_code == 200
    plan = resp.json()
    assert plan["engine"] == "mixed_pdf"
    assert probe_kwargs == [{"full_page_probe": True}]
    assert plan["preliminary"] is False
    assert plan["mixed_engine_segments"] == [
        {
            "pages": [1],
            "page_range": "1",
            "requested_engine": "liteparse_pdf",
            "actual_engine": "liteparse_pdf",
            "reasons": ["LiteParse fast path is safe"],
            "fallback_reason": None,
        },
        {
            "pages": [2, 3],
            "page_range": "2-3",
            "requested_engine": "marker_pdf",
            "actual_engine": "marker_pdf",
            "reasons": ["scan likelihood is high"],
            "fallback_reason": None,
        },
    ]


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
