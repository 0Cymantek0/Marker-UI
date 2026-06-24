from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.conversion.probe import (
    PageProbeResult,
    PdfProbeResult,
    plan_pdf_routing_segments,
    probe_pdf,
)


def make_text_pdf(path: Path, pages: int = 3) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    for page in range(pages):
        y = 740
        for line in range(35):
            c.drawString(72, y, f"Clean digital PDF page {page + 1} line {line + 1} with extractable words and numbers 12345.")
            y -= 18
        c.showPage()
    c.save()


def make_scanned_pdf(path: Path, pages: int = 3) -> None:
    img = Image.new("RGB", (1200, 1600), "white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(pages):
        image_path = path.with_suffix(".png")
        image_path.write_bytes(image_bytes)
        c.drawImage(str(image_path), 0, 0, width=letter[0], height=letter[1])
        c.showPage()
    c.save()
    image_path.unlink(missing_ok=True)


def make_sandwich_pdf(path: Path, pages: int = 3) -> None:
    img = Image.new("RGB", (1200, 1600), "white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    image_path = path.with_suffix(".png")
    image_path.write_bytes(buf.getvalue())

    c = canvas.Canvas(str(path), pagesize=letter)
    for page in range(pages):
        c.drawImage(str(image_path), 0, 0, width=letter[0], height=letter[1])
        y = 740
        for line in range(30):
            c.drawString(72, y, f"OCR text layer page {page + 1} line {line + 1} with readable words.")
            y -= 18
        c.showPage()
    c.save()
    image_path.unlink(missing_ok=True)


def make_table_heavy_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    x_positions = [40, 140, 240, 340, 440, 540]
    y = 740
    for row in range(45):
        for col, x in enumerate(x_positions):
            c.drawString(x, y, f"R{row}C{col}")
        y -= 15
    c.showPage()
    c.save()


def test_probe_clean_text_pdf_recommends_liteparse(tmp_path: Path) -> None:
    pdf = tmp_path / "clean.pdf"
    make_text_pdf(pdf)

    result = probe_pdf(pdf)

    assert result.page_count == 3
    assert result.text_layer_score >= 0.70
    assert result.text_quality_score >= 0.80
    assert result.scan_likelihood <= 0.20
    assert result.sandwich_likelihood <= 0.40
    assert result.recommended_engine == "liteparse"
    assert [page.recommended_engine for page in result.page_results] == [
        "liteparse",
        "liteparse",
        "liteparse",
    ]


def test_probe_scanned_full_page_images_recommends_marker(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf)

    result = probe_pdf(pdf)

    assert result.page_count == 3
    assert result.text_layer_score < 0.20
    assert result.scan_likelihood > 0.70
    assert result.visual_complexity_score > 0.30
    assert result.recommended_engine == "marker"
    assert all(page.recommended_engine == "marker" for page in result.page_results)


def test_probe_sandwich_pdf_recommends_marker(tmp_path: Path) -> None:
    pdf = tmp_path / "sandwich.pdf"
    make_sandwich_pdf(pdf)

    result = probe_pdf(pdf)

    assert result.text_layer_score >= 0.70
    assert result.sandwich_likelihood > 0.40
    assert result.visual_complexity_score > 0.35
    assert result.recommended_engine == "marker"


def test_probe_table_heavy_layout_recommends_marker(tmp_path: Path) -> None:
    pdf = tmp_path / "table-heavy.pdf"
    make_table_heavy_pdf(pdf)

    result = probe_pdf(pdf)

    assert result.text_layer_score >= 0.70
    assert result.layout_complexity_score > 0.45
    assert result.recommended_engine == "marker"


def test_probe_round_trips_page_results_from_mapping(tmp_path: Path) -> None:
    pdf = tmp_path / "clean.pdf"
    make_text_pdf(pdf, pages=1)

    result = probe_pdf(pdf)
    restored = PdfProbeResult.from_mapping(result.to_dict())

    assert restored.page_results[0].page_number == 1
    assert restored.page_results[0].recommended_engine == "liteparse"
    assert restored.page_results[0].text_chars > 0


def test_plan_pdf_routing_segments_groups_contiguous_same_engine_pages() -> None:
    probe = PdfProbeResult(
        page_count=4,
        text_layer_score=0.5,
        text_quality_score=1.0,
        scan_likelihood=0.5,
        sandwich_likelihood=0.0,
        layout_complexity_score=0.0,
        visual_complexity_score=0.0,
        recommended_engine="marker",
        page_results=[
            PageProbeResult(
                page_number=1,
                text_layer_score=0.9,
                text_quality_score=1.0,
                scan_likelihood=0.0,
                sandwich_likelihood=0.0,
                layout_complexity_score=0.0,
                visual_complexity_score=0.0,
                recommended_engine="liteparse",
                reasons=["LiteParse fast path is safe"],
            ),
            PageProbeResult(
                page_number=2,
                text_layer_score=0.9,
                text_quality_score=1.0,
                scan_likelihood=0.0,
                sandwich_likelihood=0.0,
                layout_complexity_score=0.0,
                visual_complexity_score=0.0,
                recommended_engine="liteparse",
                reasons=["LiteParse fast path is safe"],
            ),
            PageProbeResult(
                page_number=3,
                text_layer_score=0.0,
                text_quality_score=0.0,
                scan_likelihood=1.0,
                sandwich_likelihood=0.6,
                layout_complexity_score=0.0,
                visual_complexity_score=0.9,
                recommended_engine="marker",
                reasons=["scan likelihood is high"],
            ),
            PageProbeResult(
                page_number=4,
                text_layer_score=0.9,
                text_quality_score=1.0,
                scan_likelihood=0.0,
                sandwich_likelihood=0.0,
                layout_complexity_score=0.0,
                visual_complexity_score=0.0,
                recommended_engine="liteparse",
                reasons=["LiteParse fast path is safe"],
            ),
        ],
    )

    segments = plan_pdf_routing_segments(probe)

    assert [(segment.pages, segment.engine) for segment in segments] == [
        ([1, 2], "liteparse"),
        ([3], "marker"),
        ([4], "liteparse"),
    ]
    assert segments[0].fallback_chain == ["liteparse", "marker"]
    assert segments[1].fallback_chain == []
