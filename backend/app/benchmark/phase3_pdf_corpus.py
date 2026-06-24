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

MANUAL_REAL_TABLE_REFERENCE_TEXT = (
    "Real table-heavy sample tables page 1. The page contains actor roles, "
    "example column headers, footnoted expenditure by function, and film credits."
)

MANUAL_REAL_TABLE_REFERENCE_TABLES: list[dict[str, object]] = [
    {
        "caption": "Table 1",
        "headers": [
            "Column header (TH)",
            "Column header (TH)",
            "Column header (TH)",
        ],
        "rows": [
            ["Row header (TH)", "Data cell (TD)", "Data cell (TD)"],
            ["Row header(TH)", "Data cell (TD)", "Data cell (TD)"],
        ],
    },
    {
        "caption": "Table 2: example of footnotes referenced from within a table",
        "headers": ["Expenditure by function \u00a3 million", "", "2009/10", "2010/11 1"],
        "rows": [
            ["Policy functions", "Financial", "22.5", "30.57"],
            ["", "Information 2", "10.2", "14.8"],
            ["", "Contingency", "2.6", "1.2"],
            ["Remunerated functions", "Agency services 3", "44.7", "35.91"],
            ["", "Payments", "22.41", "19.88"],
            ["", "Banking", "22.90", "44.23"],
            ["", "Other", "12.69", "10.32"],
        ],
    },
    {
        "caption": 'Table 3: "film credits" style layout',
        "headers": ["Main character", "Daniel Radcliffe"],
        "rows": [
            ["Sidekick 1", "Rupert Grint"],
            ["Sidekick 2", "Emma Watson"],
            ["Lovable ogre", "Robbie Coltrane"],
            ["Professor", "Maggie Smith"],
            ["Headmaster", "Richard Harris"],
        ],
    },
]

MIXED_ROUTING_REFERENCE_TEXT = (
    "Mixed routing benchmark page 1. Clean digital revenue 100 cost 40. "
    "Mixed routing benchmark page 2. Scanned invoice total 250 tax 25. "
    "Mixed routing benchmark page 3. Quarter Q1 revenue 100 cost 40. "
    "Quarter Q2 revenue 140 cost 55."
)

MIXED_ROUTING_REFERENCE_TABLE = [
    ["Quarter", "Revenue", "Cost"],
    ["Q1", "100", "40"],
    ["Q2", "140", "55"],
]

REAL_MIXED_ROUTING_REFERENCE_TEXT = (
    "Real mixed routing public-pages benchmark. National Bank president and "
    "chief executive officer message discusses 2024, Canadian Western Bank, "
    "Alberta, and the bank's 165th anniversary. The middle page is an image-only "
    "public scan. The table page contains example column headers, footnoted "
    "expenditure by function, and film credits."
)


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


def _write_mixed_routing_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Table

    c = canvas.Canvas(str(path), pagesize=letter)
    _draw_text_page(
        c,
        [
            "Mixed routing benchmark page 1.",
            "Clean digital revenue 100 cost 40.",
            "This page is intentionally dense searchable text for the fast path.",
            "It repeats normal prose without images, tables, scans, or columns.",
            "The routing probe should see a clean text layer and high quality text.",
            "This keeps the first page suitable for LiteParse in mixed routing.",
            "Additional ordinary prose makes the text layer score high enough.",
            "The content is plain paragraph text with stable fonts and spacing.",
            "No raster image dominates this page and no OCR recovery is needed.",
            "This line adds words only so numeric benchmark facts remain unchanged.",
            "The conversion should preserve the revenue and cost facts above.",
            "The remaining filler is deliberately generic document body text.",
            "It makes the page representative of a normal clean digital report.",
            "The mixed benchmark can then exercise LiteParse on this segment.",
            "The later pages still require Marker for scan and table handling.",
            "End of clean digital mixed routing benchmark body text.",
        ],
    )

    img = _text_image(
        [
            "Mixed routing benchmark page 2.",
            "Scanned invoice total 250 tax 25.",
        ]
    )
    c.drawImage(ImageReader(img), 36, 72, width=540, height=700)
    c.showPage()

    c.setFont("Helvetica", 12)
    c.drawString(72, 740, "Mixed routing benchmark page 3.")
    table = Table(
        [
            ["Quarter", "Revenue", "Cost"],
            ["Q1", "100", "40"],
            ["Q2", "140", "55"],
        ],
        colWidths=[120, 120, 120],
    )
    table.wrapOn(c, 420, 300)
    table.drawOn(c, 72, 620)
    c.drawString(72, 570, "Quarter Q1 revenue 100 cost 40.")
    c.drawString(72, 550, "Quarter Q2 revenue 140 cost 55.")
    c.showPage()
    c.save()


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


def generate_mixed_routing_pdf_case(output_dir: str | Path) -> PdfBenchmarkCase:
    """Generate a three-page clean/scanned/table mixed-routing gate fixture."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = out / "mixed_routing.pdf"
    _write_mixed_routing_pdf(pdf_path)
    return PdfBenchmarkCase(
        sample_id="mixed_routing",
        pdf_path=pdf_path,
        document_class="mixed_routing",
        reference_text=MIXED_ROUTING_REFERENCE_TEXT,
        reference_table=MIXED_ROUTING_REFERENCE_TABLE,
        metadata={
            "expected_segments": [
                {"page_range": "1", "engine": "liteparse_pdf"},
                {"page_range": "2-3", "engine": "marker_pdf"},
            ],
        },
    )


def _manifest_source_url(base: Path, filename: str) -> str | None:
    manifest_path = base / "MANIFEST.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    web_pdfs = manifest.get("web_pdfs") if isinstance(manifest, dict) else {}
    if not isinstance(web_pdfs, dict):
        return None
    return web_pdfs.get(filename)


def generate_real_mixed_routing_pdf_case(
    fixture_dir: str | Path,
    output_dir: str | Path,
) -> PdfBenchmarkCase:
    """Build a mixed-routing gate fixture from existing public/manual PDFs.

    The output is a three-page composite:
    1. clean searchable annual-report prose (LiteParse-safe),
    2. image-only scanned public sample (Marker-required),
    3. table-heavy public sample page (Marker-required).
    """
    from pypdf import PdfReader, PdfWriter

    base = Path(fixture_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    sources = {
        "clean_annual_report.pdf": 5,
        "scanned_image_only.pdf": 1,
        "table_heavy_sample_tables.pdf": 1,
    }
    missing = [filename for filename in sources if not (base / filename).is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing real mixed-routing PDF fixtures: " + ", ".join(missing)
        )

    writer = PdfWriter()
    for filename, page_number in sources.items():
        reader = PdfReader(str(base / filename))
        page_index = page_number - 1
        if len(reader.pages) <= page_index:
            raise ValueError(f"{filename} does not contain page {page_number}")
        writer.add_page(reader.pages[page_index])

    pdf_path = out / "real_mixed_public_pages.pdf"
    with pdf_path.open("wb") as handle:
        writer.write(handle)

    return PdfBenchmarkCase(
        sample_id="real_mixed_public_pages",
        pdf_path=pdf_path,
        document_class="mixed_routing",
        reference_text=REAL_MIXED_ROUTING_REFERENCE_TEXT,
        reference_table=MANUAL_REAL_TABLE_REFERENCE_TABLES,
        metadata={
            "expected_segments": [
                {"page_range": "1", "engine": "liteparse_pdf"},
                {"page_range": "2-3", "engine": "marker_pdf"},
            ],
            "source_pages": {
                filename: page_number for filename, page_number in sources.items()
            },
            "source_urls": {
                filename: _manifest_source_url(base, filename)
                for filename in sources
            },
        },
    )


def load_manual_real_table_heavy_pdf_cases(
    fixture_dir: str | Path,
) -> list[PdfBenchmarkCase]:
    """Load optional real-doc table-heavy cases from the manual fixture area.

    These fixtures are intentionally not generated by this module. They come
    from public sample documents listed in the local MANIFEST, so callers must
    opt in and provide the fixture directory explicitly.
    """
    base = Path(fixture_dir)
    pdf_path = base / "table_heavy_sample_tables.pdf"
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Missing real table-heavy PDF fixture: {pdf_path}")

    return [
        PdfBenchmarkCase(
            sample_id="real_table_heavy_sample_tables_page1",
            pdf_path=pdf_path,
            document_class="real_table_heavy",
            reference_text=MANUAL_REAL_TABLE_REFERENCE_TEXT,
            reference_table=MANUAL_REAL_TABLE_REFERENCE_TABLES,
            metadata={
                "conversion_options": {
                    "page_range": "1",
                },
                "source_url": _manifest_source_url(
                    base,
                    "table_heavy_sample_tables.pdf",
                ),
            },
        )
    ]
