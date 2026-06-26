"""Safety tests for full-page mixed PDF routing."""

from __future__ import annotations

from typing import Any

from app.conversion.result import UniversalConversionResult
from app.services.conversion_service import ConversionService


class _FakeMarkerService:
    def __init__(self) -> None:
        self.convert_calls: list[tuple[str, dict[str, Any]]] = []

    def convert_file(self, filepath: str, options: dict[str, Any], device: str | None = None) -> dict[str, Any]:
        self.convert_calls.append((filepath, dict(options)))
        return {
            "text": "# Whole file marker output",
            "extension": "md",
            "images": {},
            "metadata": {},
        }


def test_sampled_mixed_probe_is_labelled_and_does_not_execute_mixed(tmp_path):
    svc = ConversionService(_FakeMarkerService())
    pdf_path = tmp_path / "sampled.pdf"
    pdf_path.write_bytes(b"%PDF")

    config = {
        "enable_mixed_pdf_routing": True,
        "probe_result": _mixed_probe_result(page_count=5, pages=[1, 2, 3, 5]),
    }

    plan = svc.plan(str(pdf_path), config)
    result = svc.convert_file(str(pdf_path), config)

    assert plan.engine != "mixed_pdf"
    assert any("sampled" in warning.lower() for warning in plan.warnings)
    assert result["metadata"]["engine"]["engine"] == "marker_pdf"
    assert "mixed_engine_segments" not in result["metadata"]
    assert any("sampled" in warning.lower() for warning in result["metadata"]["engine"]["warnings"])


def test_mixed_pdf_segments_cover_every_page_in_output_and_metadata(tmp_path):
    svc = ConversionService(_FakeMarkerService())
    pdf_path = tmp_path / "full.pdf"
    pdf_path.write_bytes(b"%PDF")
    config = {
        "enable_mixed_pdf_routing": True,
        "probe_result": _mixed_probe_result(page_count=5, pages=[1, 2, 3, 4, 5]),
    }
    liteparse = svc.registry.get("liteparse_pdf")
    marker = svc.registry.get("marker_pdf")
    assert liteparse is not None
    assert marker is not None
    original_liteparse = liteparse.convert
    original_marker = marker.convert

    def liteparse_convert(filepath, segment_config, device=None):
        return UniversalConversionResult(
            text=f"liteparse pages {segment_config['page_range']} " + ("x" * 200),
            extension="md",
        )

    def marker_convert(filepath, segment_config, device=None):
        return UniversalConversionResult(
            text=f"marker pages {segment_config['page_range']} " + ("x" * 200),
            extension="md",
        )

    liteparse.convert = liteparse_convert  # type: ignore[assignment]
    marker.convert = marker_convert  # type: ignore[assignment]
    try:
        result = svc.convert_file(str(pdf_path), config)
    finally:
        liteparse.convert = original_liteparse  # type: ignore[assignment]
        marker.convert = original_marker  # type: ignore[assignment]

    assert result["metadata"]["engine"]["engine"] == "mixed_pdf"
    assert "<!-- pages: 1-2 -->" in result["text"]
    assert "<!-- pages: 3 -->" in result["text"]
    assert "<!-- pages: 4 -->" in result["text"]
    assert "<!-- pages: 5 -->" in result["text"]
    covered_pages = [
        page
        for segment in result["metadata"]["mixed_engine_segments"]
        for page in segment["pages"]
    ]
    assert covered_pages == [1, 2, 3, 4, 5]
    assert result["metadata"]["probe_result"]["full_page_coverage"] is True


def _mixed_probe_result(*, page_count: int, pages: list[int]) -> dict[str, Any]:
    page_results: list[dict[str, Any]] = []
    for page in pages:
        if page in {1, 2, 4}:
            page_results.append(_page(page, "liteparse"))
        else:
            page_results.append(_page(page, "marker"))
    return {
        "page_count": page_count,
        "text_layer_score": 0.5,
        "text_quality_score": 0.7,
        "scan_likelihood": 0.5,
        "sandwich_likelihood": 0.5,
        "layout_complexity_score": 0.0,
        "visual_complexity_score": 0.5,
        "recommended_engine": "marker",
        "reasons": ["mixed page risk"],
        "sampled_pages": pages,
        "sampled_image_count": 1,
        "full_page_coverage": pages == list(range(1, page_count + 1)),
        "page_results": page_results,
    }


def _page(page_number: int, engine: str) -> dict[str, Any]:
    if engine == "liteparse":
        return {
            "page_number": page_number,
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
        }
    return {
        "page_number": page_number,
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
    }
