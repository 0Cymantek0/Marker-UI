"""Generated Phase 3 PDF benchmark corpus.

The generated files are intentionally small, generic, and repo-safe. They live
under an operator-selected fixture directory (normally gitignored
``backend/tests/fixtures/phase3_pdf_benchmark``) while this module stores only
the reproducible recipes and golden text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.benchmark.runner import PHASE3_PDF_CLASSES, PdfBenchmarkCase


PHASE3_REFERENCE_TEXT: dict[str, str] = {
    "clean_digital": (
        "Clean digital benchmark. Revenue 100. Cost 40. Margin 60. "
        "This file has a normal searchable text layer."
    ),
    "scanned": (
        "Scanned benchmark. This page is a raster image of text. "
        "The correct output should recover invoice total 250 and tax 25."
    ),
    "sandwich": (
        "Sandwich benchmark. Hidden OCR text sits over a full page image. "
        "Account A shows balance 300 and account B shows balance 450."
    ),
    "table_heavy": (
        "Table heavy benchmark. Quarter Q1 revenue 100 cost 40. "
        "Quarter Q2 revenue 140 cost 55. Quarter Q3 revenue 160 cost 65."
    ),
    "formula_heavy": (
        "Formula heavy benchmark. Energy formula E = m c squared. "
        "Integral from 0 to 1 of x squared dx equals one third."
    ),
}

PHASE3_REFERENCE_TABLES: dict[str, list[list[str]] | None] = {
    "clean_digital": None,
    "scanned": None,
    "sandwich": [["Account", "Balance"], ["A", "300"], ["B", "450"]],
    "table_heavy": [
        ["Quarter", "Revenue", "Cost"],
        ["Q1", "100", "40"],
        ["Q2", "140", "55"],
        ["Q3", "160", "65"],
    ],
    "formula_heavy": None,
}


def _draw_text_page(c: Any, lines: list[str]) -> None:
    c.setFont("Helvetica", 12)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 22
    c.showPage()


def _text_image(lines: list[str]) -> Any:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1000, 1300), "white")
    draw = ImageDraw.Draw(img)
    y = 90
    for line in lines:
        draw.text((90, y), line, fill="black")
        y += 42
    return img


def _write_pdf(document_class: str, path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Table

    c = canvas.Canvas(str(path), pagesize=letter)

    if document_class == "clean_digital":
        _draw_text_page(
            c,
            [
                "Clean digital benchmark.",
                "Revenue 100. Cost 40. Margin 60.",
                "This file has a normal searchable text layer.",
            ],
        )
        c.save()
        return

    if document_class == "scanned":
        img = _text_image(
            [
                "Scanned benchmark.",
                "This page is a raster image of text.",
                "Invoice total 250 and tax 25.",
            ]
        )
        c.drawImage(ImageReader(img), 36, 72, width=540, height=700)
        c.showPage()
        c.save()
        return

    if document_class == "sandwich":
        img = _text_image(
            [
                "Sandwich benchmark.",
                "Full page image background.",
                "Account A 300. Account B 450.",
            ]
        )
        c.drawImage(ImageReader(img), 36, 72, width=540, height=700)
        c.setFillColorRGB(1, 1, 1)
        c.drawString(72, 720, "Sandwich benchmark.")
        c.drawString(72, 700, "Hidden OCR text sits over a full page image.")
        c.drawString(72, 680, "Account A shows balance 300 and account B shows balance 450.")
        c.showPage()
        c.save()
        return

    if document_class == "table_heavy":
        c.setFont("Helvetica", 12)
        c.drawString(72, 740, "Table heavy benchmark.")
        table = Table(
            [
                ["Quarter", "Revenue", "Cost"],
                ["Q1", "100", "40"],
                ["Q2", "140", "55"],
                ["Q3", "160", "65"],
            ],
            colWidths=[120, 120, 120],
        )
        table.wrapOn(c, 420, 300)
        table.drawOn(c, 72, 620)
        c.drawString(72, 570, "Quarter Q1 revenue 100 cost 40.")
        c.drawString(72, 550, "Quarter Q2 revenue 140 cost 55.")
        c.drawString(72, 530, "Quarter Q3 revenue 160 cost 65.")
        c.showPage()
        c.save()
        return

    if document_class == "formula_heavy":
        _draw_text_page(
            c,
            [
                "Formula heavy benchmark.",
                "Energy formula E = m c squared.",
                "Integral from 0 to 1 of x squared dx equals one third.",
            ],
        )
        c.save()
        return

    raise ValueError(f"Unknown Phase 3 document class: {document_class}")


def generate_phase3_pdf_cases(output_dir: str | Path) -> list[PdfBenchmarkCase]:
    """Generate the five required Phase 3 PDF classes and golden sidecar."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    sidecar: list[dict[str, object]] = []
    cases: list[PdfBenchmarkCase] = []

    for document_class in PHASE3_PDF_CLASSES:
        pdf_name = f"{document_class}.pdf"
        pdf_path = out / pdf_name
        _write_pdf(document_class, pdf_path)
        reference_table = PHASE3_REFERENCE_TABLES[document_class]
        cases.append(
            PdfBenchmarkCase(
                sample_id=document_class,
                pdf_path=pdf_path,
                document_class=document_class,
                reference_text=PHASE3_REFERENCE_TEXT[document_class],
                reference_table=reference_table,
            )
        )
        sidecar.append(
            {
                "sample_id": document_class,
                "pdf_path": pdf_name,
                "document_class": document_class,
                "reference_text": PHASE3_REFERENCE_TEXT[document_class],
                "reference_table": reference_table,
            }
        )

    (out / "golden.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return cases


def load_phase3_pdf_cases(output_dir: str | Path) -> list[PdfBenchmarkCase]:
    """Load generated Phase 3 cases from ``golden.json``."""
    out = Path(output_dir)
    sidecar_path = out / "golden.json"
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    cases: list[PdfBenchmarkCase] = []
    for item in raw:
        cases.append(
            PdfBenchmarkCase(
                sample_id=str(item["sample_id"]),
                pdf_path=out / str(item["pdf_path"]),
                document_class=str(item["document_class"]),
                reference_text=str(item["reference_text"]),
                reference_table=item.get("reference_table"),
            )
        )
    return cases
