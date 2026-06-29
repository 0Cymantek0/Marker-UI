from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.conversion.probe import (
    PageProbeResult,
    PdfProbeResult,
    missing_probe_pages,
    plan_pdf_routing_segments,
    probe_has_full_page_coverage,
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


def test_probe_full_page_option_covers_every_page(tmp_path: Path) -> None:
    pdf = tmp_path / "five-pages.pdf"
    make_text_pdf(pdf, pages=5)

    sampled = probe_pdf(pdf)
    full = probe_pdf(pdf, full_page_probe=True)

    assert sampled.page_count == 5
    assert [page.page_number for page in sampled.page_results] == [1, 2, 3, 5]
    assert probe_has_full_page_coverage(sampled) is False
    assert missing_probe_pages(sampled) == [4]
    assert [page.page_number for page in full.page_results] == [1, 2, 3, 4, 5]
    assert full.full_page_coverage is True
    assert probe_has_full_page_coverage(full) is True


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


def make_text_pdf_with_metadata(path: Path, creator: str, producer: str) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setCreator(creator)
    c.setProducer(producer)
    y = 740
    for line in range(35):
        c.drawString(72, y, "Clean digital PDF with metadata.")
        y -= 18
    c.showPage()
    c.save()


def test_probe_metadata_digital_reduces_scan_likelihood(tmp_path: Path) -> None:
    pdf = tmp_path / "metadata_digital.pdf"
    make_text_pdf_with_metadata(pdf, creator="LaTeX with hyperref", producer="pdfTeX-1.40")

    result = probe_pdf(pdf)

    # LaTeX producer is a digital generator, should decrease scan likelihood
    # Since text_layer_score is 1.0, baseline scan_likelihood is 0.0.
    # Adjusted scan_likelihood is clamp(0.0 - 0.3) = 0.0.
    # Let's verify the metadata reason is present.
    assert result.scan_likelihood == 0.0
    assert any(
        "metadata matches digital generator 'latex'" in r 
        or "metadata matches digital generator 'pdftex'" in r 
        for r in result.reasons
    )


def test_probe_metadata_scanner_increases_scan_likelihood(tmp_path: Path) -> None:
    pdf = tmp_path / "metadata_scanner.pdf"
    make_text_pdf_with_metadata(pdf, creator="Fujitsu ScanSnap", producer="Adobe Acrobat Pro")

    result = probe_pdf(pdf)

    # Fujitsu is a scanner keyword, should increase scan likelihood
    # Baseline scan_likelihood for clean text is 0.0.
    # Adjusted scan_likelihood should be clamp(0.0 + 0.4) = 0.40.
    assert result.scan_likelihood == 0.40
    assert any(
        "metadata matches scanner keyword 'fujitsu'" in r 
        or "metadata matches scanner keyword 'scan'" in r 
        for r in result.reasons
    )


def make_digital_pdf_with_non_overlapping_image(path: Path, img_path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    # Draw large image on left half
    c.drawImage(str(img_path), 50, 50, width=300, height=600)
    # Draw text on right half (no overlap)
    y = 700
    for line in range(15):
        c.drawString(400, y, f"Clean text block line {line + 1} that does not overlap the image.")
        y -= 30
    c.showPage()
    c.save()


def test_probe_sandwich_overlap_precise(tmp_path: Path) -> None:
    # 1. Create a dummy image
    img = Image.new("RGB", (300, 600), "blue")
    img_buf = BytesIO()
    img.save(img_buf, format="PNG")
    img_path = tmp_path / "test_image.png"
    img_path.write_bytes(img_buf.getvalue())

    # 2. PDF with a large image and non-overlapping text (sandwich ratio should be 0.0)
    non_overlap_pdf = tmp_path / "non_overlap.pdf"
    make_digital_pdf_with_non_overlapping_image(non_overlap_pdf, img_path)
    res_non_overlap = probe_pdf(non_overlap_pdf)
    
    # 3. PDF with a large image and overlapping text (sandwich ratio should be 1.0)
    overlap_pdf = tmp_path / "overlap.pdf"
    make_sandwich_pdf(overlap_pdf, pages=1)
    res_overlap = probe_pdf(overlap_pdf)

    # Clean up image file
    img_path.unlink(missing_ok=True)

    # Non-overlapping image page should have 0.0 sandwich likelihood and be routed to liteparse
    assert res_non_overlap.sandwich_likelihood == 0.0
    assert res_non_overlap.recommended_engine == "liteparse"

    # Overlapping image page should have 1.0 (or high) sandwich likelihood and be routed to marker
    assert res_overlap.sandwich_likelihood > 0.80
    assert res_overlap.recommended_engine == "marker"


def make_mixed_pdf(path: Path) -> None:
    # 10 pages total
    # Pages 1-9: digital text
    # Page 10: scanned full page image
    img = Image.new("RGB", (1200, 1600), "white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    img_path = path.with_suffix(".png")
    img_path.write_bytes(buf.getvalue())

    c = canvas.Canvas(str(path), pagesize=letter)
    for page in range(10):
        if page == 9: # Page 10 is scanned
            c.drawImage(str(img_path), 0, 0, width=letter[0], height=letter[1])
        else:
            y = 740
            for line in range(35):
                c.drawString(72, y, f"Clean digital PDF page {page + 1} line {line + 1} with extractable words.")
                y -= 18
        c.showPage()
    c.save()
    img_path.unlink(missing_ok=True)


def test_dynamic_sampling_borderline_triggers_expansion(tmp_path: Path) -> None:
    pdf = tmp_path / "mixed_sampling.pdf"
    make_mixed_pdf(pdf)

    # By default, max_deep_pages is 4.
    # Base sample: pages 1, 2, 3, and 10 (indices 0, 1, 2, 9).
    # Since page 10 is scanned (recommends marker) and pages 1-3 are digital (recommends liteparse),
    # it contains mixed engine recommendations, which triggers dynamic expansion.
    result = probe_pdf(pdf)

    # Verify that it expanded to sample 8 pages
    assert len(result.page_results) == 8
    assert len(result.sampled_pages) == 8
    # Base pages: 1, 2, 3, 10
    # Unsampled pages to choose from: 4, 5, 6, 7, 8, 9 (indices 3, 4, 5, 6, 7, 8)
    # Uniformly chosen 4 pages: indices 3, 4, 6, 7 (pages 4, 5, 7, 8) or similar.
    # Let's verify that some middle pages (between 4 and 9) are in the sampled list.
    assert any(p in result.sampled_pages for p in [4, 5, 6, 7, 8, 9])



