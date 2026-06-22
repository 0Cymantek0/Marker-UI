from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.conversion.probe import probe_pdf


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


def test_probe_scanned_full_page_images_recommends_marker(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf)

    result = probe_pdf(pdf)

    assert result.page_count == 3
    assert result.text_layer_score < 0.20
    assert result.scan_likelihood > 0.70
    assert result.visual_complexity_score > 0.30
    assert result.recommended_engine == "marker"


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
