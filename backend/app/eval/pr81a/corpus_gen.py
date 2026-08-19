"""Deterministic PR81A visual-hard corpus generator.

This module builds every PR81A corpus PDF and its oracle text transcript
from one set of constants. Gold answers in :data:`QUERIES` reference the
same constants the drawing code renders, so the judged answers cannot
drift from the pixels on the page.

Design rules:

* PDFs are byte-deterministic (``reportlab`` ``rl_config.invariant`` is
  enabled per build); building twice yields identical bytes, therefore
  identical content blob keys.
* The oracle transcript is the *character-perfect, structure-realistic*
  text an ideal extractor would produce: perfect characters, table rows
  joined, chart labels as separate runs, form values fragmented per draw
  run, two-column pages fragmented per column block.  The lexical lanes
  receive this oracle text, which deliberately biases the benchmark
  *against* the visual routes: any measured visual gain survives an
  oracle-quality text baseline.
* Only repository-standard ASCII is drawn so font subsetting cannot
  introduce environment-dependent bytes.

The module is a library, not a script: ``backend/scripts/gen_pr81a_corpus.py``
calls :func:`build_all` and writes the committed corpus under
``backend/eval_data/pr81a``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import reportlab.rl_config as rl_config

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

GENERATOR_VERSION = "marker.pr81a_corpus_gen.v1"

PAGE_W, PAGE_H = letter

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"

_PALETTE = (
    HexColor("#1f4e79"),
    HexColor("#c00000"),
    HexColor("#548235"),
    HexColor("#7f7f7f"),
    HexColor("#bf8f00"),
    HexColor("#7030a0"),
)


@dataclass(frozen=True)
class OraclePage:
    """Oracle transcript of one page: the text runs an ideal extractor sees."""

    page_number: int
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class CorpusArtifact:
    """One generated PDF plus its oracle transcript."""

    doc_key: str
    pdf_bytes: bytes
    pages: tuple[OraclePage, ...]


class _Sheet:
    """One page under construction: drawing ops plus oracle text runs."""

    def __init__(self, c: canvas.Canvas) -> None:
        self.c = c
        self.nodes: list[str] = []

    # -- text primitives (each records one oracle run) -----------------

    def text(
        self,
        x: float,
        y: float,
        s: str,
        *,
        size: float = 10,
        bold: bool = False,
        node: bool = True,
        center: bool = False,
        max_x: float | None = None,
    ) -> None:
        self.c.setFont(_FONT_BOLD if bold else _FONT, size)
        if center:
            self.c.drawCentredString(x, y, s)
        else:
            self.c.drawString(x, y, s)
        if node:
            self.nodes.append(s)

    def wrap(
        self,
        x: float,
        y: float,
        text: str,
        *,
        width: float,
        size: float = 10,
        leading: float = 14,
        bold: bool = False,
    ) -> float:
        """Draw left-aligned wrapped prose; one oracle node per output line."""
        self.c.setFont(_FONT_BOLD if bold else _FONT, size)
        words = text.split()
        lines: list[str] = []
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if self.c.stringWidth(candidate, _FONT_BOLD if bold else _FONT, size) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        for i, out in enumerate(lines):
            self.c.drawString(x, y - i * leading, out)
            self.nodes.append(out)
        return y - (len(lines) - 1) * leading

    def box(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        fill=None,
        stroke: bool = True,
        label: str | None = None,
        label_size: float = 9,
        bold: bool = False,
        node_label: bool = True,
    ) -> None:
        self.c.setLineWidth(0.8)
        if fill is not None:
            self.c.setFillColor(fill)
            self.c.rect(x, y, w, h, fill=1, stroke=1 if stroke else 0)
            self.c.setFillColor(HexColor("#000000"))
        elif stroke:
            self.c.rect(x, y, w, h, fill=0, stroke=1)
        if label:
            self.text(
                x + w / 2,
                y + h / 2 - label_size / 3,
                label,
                size=label_size,
                bold=bold,
                center=True,
                node=node_label,
            )


# ---------------------------------------------------------------------------
# Shared value constants. Queries reference these; drawing renders these.
# ---------------------------------------------------------------------------

# doc-fin-01 (2024 report)
FIN01_REF_CODE = "ZETA-9"
FIN01_REVENUE = "48.2M"
FIN01_BAR_TITLE = "Quarterly Revenue by Region (2024)"
FIN01_BAR_TOP_REGION = "West"
FIN01_BAR_TOP_VALUE = "4.0"
FIN01_BAR_SHORT_REGION = "North"
FIN01_TABLE_TITLE = "Regional Product Sales - 2024 Units (thousands)"
FIN01_WT300_WEST = "18.5"

# doc-fin-03 (2023 near-duplicate)
FIN03_REF_CODE = "ZETA-7"
FIN03_BAR_TITLE = "Quarterly Revenue by Region (2023)"
FIN03_BAR_TOP_REGION = "East"
FIN03_BAR_TOP_VALUE = "3.8"
FIN03_TABLE_TITLE = "Regional Product Sales - 2023 Units (thousands)"
FIN03_WT300_WEST = "14.7"

# doc-fin-02
FIN02_TABLE_TITLE = "Cost per Unit by Line (USD)"
FIN02_L3_LABOR = "2.95"
FIN02_L3_UNIT_COST = "8.30"
FIN02_TREND_TITLE = "Unit Cost Trend 2024"
FIN02_TREND_FINAL_L1 = "8.10"

# doc-rd-01 / doc-rd-02
RD01_PIE_TITLE = "Issue Distribution by Category - Beta"
RD01_TOP_SLICE = "Firmware"
RD01_TABLE_TITLE = "Defect Counts by Build - Beta"
RD01_B102_TOTAL = "47"
RD02_PIE_TITLE = "Issue Distribution by Category - Gamma"
RD02_TOP_SLICE = "Display"
RD02_TABLE_TITLE = "Defect Counts by Build - Gamma"
RD02_B102_TOTAL = "39"

# doc-ops-01
OPS01_FORM_TITLE = "HeatTech Facility Inspection Form HT-07"
OPS01_APPROVED_BUDGET = "95000"
OPS01_APPROVED_OUTAGE = "Jun-24"
OPS01_REVIEWER_SIGNED = "Mar-15"
OPS01_TECH_SIGNED = "Mar-12"

# doc-ops-02
OPS02_ROUTE_B_FIRST_STOP = "Depot East"
OPS02_ROUTE_B_FINAL_STOP = "Harbor Terminal"
OPS02_ROUTE_A_DRIVE = "3h40m"

# doc-hr-01
HR01_REMOTE_CODE = "RW-2"
HR01_LEAVE_CODE = "LEAVE-7.3"
HR01_CARRYOVER = "15"

# doc-hr-02
HR02_QUALITY_DIRECTOR = "Omar Haddad"
HR02_VP = "Morgan Tate"

# doc-mfg-01
MFG01_OEE = "87.4%"
MFG01_LOAD = "72%"

# doc-rev-01
REV01_V3_AUDIT = "2026-02-11"
REV01_V3_FINDINGS = "3"
REV01_V4_AUDIT = "2026-08-02"
REV01_V4_FINDINGS = "1"
REV01_DRILL_DUE = "2026-09-01"

# doc-sec-01 / doc-sec-02 / doc-pub-03
SEC01_SEVERANCE = "2.4M"
SEC01_EFFECTIVE = "2026-10-01"
PAYROLL_EFFECTIVE = "2026-09-15"

# doc-leg-01
LEG01_RENT = "2450"
LEG01_TERM = "24"
LEG01_EARLY_TERM_CLAUSE = "CLAUSE-9"


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _title_block(sheet: _Sheet, title: str, subtitle: str | None = None) -> float:
    sheet.text(PAGE_W / 2, PAGE_H - 60, title, size=16, bold=True, center=True)
    if subtitle:
        sheet.text(PAGE_W / 2, PAGE_H - 78, subtitle, size=10, center=True)
    sheet.c.setLineWidth(1.2)
    sheet.c.line(72, PAGE_H - 90, PAGE_W - 72, PAGE_H - 90)
    return PAGE_H - 110


def _bar_chart(
    sheet: _Sheet,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    regions: Sequence[str],
    quarters: Sequence[str],
    values: Mapping[str, Sequence[float]],
) -> None:
    """Grouped vertical bar chart; tallest group is drawn strictly tallest.

    Value labels are computed from the drawn values (label above each
    quarter's tallest bar), and no callout names the global tallest bar:
    binding a value/region label to the tallest bar must be done from the
    rendered geometry, not from text.
    """
    sheet.text(x + w / 2, y + h + 14, title, size=12, bold=True, center=True)
    axis_bottom = y + 28
    plot_h = h - 40
    max_v = 4.4
    n_groups = len(quarters)
    n_series = len(regions)
    group_w = w / n_groups
    bar_w = (group_w - 14) / n_series
    sheet.c.setLineWidth(0.9)
    sheet.c.line(x, axis_bottom, x + w, axis_bottom)
    sheet.c.line(x, axis_bottom, x, axis_bottom + plot_h)
    for qi, quarter in enumerate(quarters):
        gx = x + qi * group_w + 7
        tallest = 0.0
        for si, region in enumerate(regions):
            v = values[region][qi]
            tallest = max(tallest, v)
            bar_h = (v / max_v) * plot_h
            bx = gx + si * bar_w
            sheet.c.setFillColor(_PALETTE[si % len(_PALETTE)])
            sheet.c.rect(bx, axis_bottom, bar_w - 2, bar_h, fill=1, stroke=0)
            sheet.c.setFillColor(HexColor("#000000"))
        gx_center = gx + (n_series * bar_w - 2) / 2
        sheet.text(gx_center, axis_bottom - 12, quarter, size=9, center=True)
        # label above each quarter's tallest bar, computed from drawn values
        sheet.text(gx_center, axis_bottom + (tallest / max_v) * plot_h + 4, f"{tallest:.1f}", size=7, center=True)
    # legend
    for si, region in enumerate(regions):
        lx = x + w - 170 + (si % 2) * 85
        ly = y + h - 6 - (si // 2) * 14
        sheet.c.setFillColor(_PALETTE[si % len(_PALETTE)])
        sheet.c.rect(lx, ly, 8, 8, fill=1, stroke=0)
        sheet.c.setFillColor(HexColor("#000000"))
        sheet.text(lx + 12, ly + 0.5, region, size=8)


def _pie_chart(
    sheet: _Sheet,
    cx: float,
    cy: float,
    r: float,
    title: str,
    categories: Sequence[tuple[str, float]],
) -> None:
    sheet.text(cx, cy + r + 30, title, size=12, bold=True, center=True)
    start = 90.0
    for i, (name, share) in enumerate(categories):
        extent = share * 3.6
        sheet.c.setFillColor(_PALETTE[i % len(_PALETTE)])
        sheet.c.wedge(cx - r, cy - r, cx + r, cy + r, start, -extent, fill=1, stroke=1)
        start -= extent
    sheet.c.setFillColor(HexColor("#000000"))
    for i, (name, share) in enumerate(categories):
        lx = cx + r + 40
        ly = cy + r - 14 - i * 16
        sheet.c.setFillColor(_PALETTE[i % len(_PALETTE)])
        sheet.c.rect(lx, ly, 8, 8, fill=1, stroke=0)
        sheet.c.setFillColor(HexColor("#000000"))
        sheet.text(lx + 12, ly + 0.5, f"{name} {share:g}%", size=9)


def _line_chart(
    sheet: _Sheet,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    months: Sequence[str],
    series: Mapping[str, Sequence[float]],
    label_series: str,
    final_label: str,
) -> None:
    sheet.text(x + w / 2, y + h + 14, title, size=12, bold=True, center=True)
    axis_bottom = y + 28
    plot_h = h - 44
    lo, hi = 6.0, 12.0
    sheet.c.setLineWidth(0.9)
    sheet.c.line(x, axis_bottom, x + w, axis_bottom)
    sheet.c.line(x, axis_bottom, x, axis_bottom + plot_h)
    n = len(months)
    for i, month in enumerate(months):
        px = x + (i + 0.5) * (w / n)
        sheet.text(px, axis_bottom - 12, month, size=8, center=True)
    colors = iter(_PALETTE)
    for name, vals in series.items():
        color = next(colors)
        sheet.c.setStrokeColor(color)
        sheet.c.setLineWidth(1.6)
        points = [
            (x + (i + 0.5) * (w / n), axis_bottom + ((v - lo) / (hi - lo)) * plot_h)
            for i, v in enumerate(vals)
        ]
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            sheet.c.line(x1, y1, x2, y2)
        fx, fy = points[-1]
        sheet.c.setFillColor(color)
        sheet.c.circle(fx, fy, 2.2, fill=1, stroke=0)
        sheet.c.setFillColor(HexColor("#000000"))
        sheet.text(fx + 6, fy + 4, f"{vals[-1]:.2f}", size=8)
        sheet.text(x + 4, axis_bottom + plot_h + 6 - list(series).index(name) * 12, name, size=8)
    sheet.c.setStrokeColor(HexColor("#000000"))
    sheet.c.setLineWidth(0.9)


def _table_grid(
    sheet: _Sheet,
    x: float,
    y_top: float,
    col_widths: Sequence[float],
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    title: str | None = None,
    row_h: float = 20,
) -> None:
    if title:
        sheet.text(x, y_top + 16, title, size=12, bold=True)
    widths = list(col_widths)
    sheet.c.setLineWidth(0.8)
    total_w = sum(widths)
    y = y_top
    # header (shaded)
    sheet.c.setFillColor(HexColor("#dce6f1"))
    sheet.c.rect(x, y - row_h, total_w, row_h, fill=1, stroke=1)
    sheet.c.setFillColor(HexColor("#000000"))
    cx = x
    for w_cell, cell in zip(widths, header):
        sheet.text(cx + 4, y - row_h + 6, cell, size=9, bold=True)
        cx += w_cell
    y -= row_h
    header_row = " ".join(header)
    for row in rows:
        cx = x
        for w_cell, cell in zip(widths, row):
            sheet.text(cx + 4, y - row_h + 6, cell, size=9)
            cx += w_cell
        y -= row_h
    # grid lines
    sheet.c.setLineWidth(0.6)
    gy_top = y_top
    gy_bottom = y
    for i in range(len(rows) + 2):
        yy = gy_top - i * row_h
        sheet.c.line(x, yy, x + total_w, yy)
    cx = x
    for w_cell in widths[:-1]:
        cx += w_cell
        sheet.c.line(cx, gy_bottom, cx, gy_top)
    sheet.c.setLineWidth(0.9)


class _Doc:
    """Multi-page document under construction.

    ``page()`` closes the previous page (reportlab ``showPage``) before
    handing out the next sheet — pages must be closed while drawing, or
    everything lands on page one.
    """

    def __init__(self) -> None:
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=letter)
        self.sheets: list[_Sheet] = []

    def page(self) -> _Sheet:
        if self.sheets:
            self.sheets[-1].c.showPage()
        sheet = _Sheet(self.c)
        self.sheets.append(sheet)
        return sheet

    def finish(self) -> tuple[bytes, tuple[OraclePage, ...]]:
        if self.sheets:
            self.sheets[-1].c.showPage()
        self.c.save()
        pages = tuple(
            OraclePage(page_number=i + 1, nodes=tuple(s.nodes))
            for i, s in enumerate(self.sheets)
        )
        return self.buf.getvalue(), pages


def _new_doc() -> _Doc:
    return _Doc()


# ---------------------------------------------------------------------------
# Per-document builders. Each returns CorpusArtifact.
# ---------------------------------------------------------------------------


def _build_fin_report(
    doc_key: str,
    title: str,
    year: str,
    ref_code: str,
    revenue: str,
    bar_title: str,
    top_region: str,
    top_value: str,
    table_title: str,
    wt300_west: str,
    regions_west: float,
    regions_east: float,
) -> CorpusArtifact:
    d = _new_doc()

    # page 1 — summary prose (text-easy target)
    s1 = d.page()
    y = _title_block(s1, title, f"Published {year} - Advisory Finance Group")
    s1.wrap(72, y, f"Consolidated revenue reached {revenue} USD in {year}, driven by demand across all four operating regions.", width=460, size=11, leading=16)
    s1.wrap(72, y - 40, f"Document reference code {ref_code}. This report is issued for internal distribution and summarizes quarterly revenue by region and regional product sales in units of thousands.", width=460, size=11, leading=16)
    s1.wrap(72, y - 90, "Regional management should reconcile unit figures against warehouse shipment ledgers before external publication.", width=460, size=11, leading=16)

    # page 2 — grouped bar chart
    s2 = d.page()
    _title_block(s2, title, "Quarterly revenue")
    _bar_chart(
        s2,
        90,
        200,
        430,
        300,
        bar_title,
        ("North", "East", "South", "West"),
        ("Q1", "Q2", "Q3", "Q4"),
        {
            "North": (2.1, 2.4, 2.6, 2.8),
            "East": (2.8, 3.1, regions_east, 3.5),
            "South": (2.5, 2.7, 2.9, 3.0),
            "West": (3.0, 3.4, 4.0 if top_region == "West" else 3.2, 3.9 if top_region == "West" else 3.3),
        },
    )

    # page 3 — dense grid table
    s3 = d.page()
    _title_block(s3, title, "Product sales")
    _table_grid(
        s3,
        72,
        560,
        (110, 80, 80, 80, 80, 80),
        ("Region", "WT-100", "WT-200", "WT-300", "WT-400", "Total"),
        (
            ("North", "12.5", "8.3", "15.2", "6.1", "42.1"),
            ("East", "14.2", "9.0", "16.8", "5.4", "45.4"),
            ("South", "11.8", "7.6", "13.9", "4.9", "38.2"),
            ("West", "16.0", "10.2", wt300_west, "7.3", "52.0" if year == "2024" else "47.0"),
        ),
        table_title,
    )
    s3.wrap(72, 430, "Units are thousands of shipped items. Totals include returns processed in the final week of the quarter.", width=450)

    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key=doc_key, pdf_bytes=pdf_bytes, pages=pages)


def _build_fin02() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, "Vertex Manufacturing Cost Analysis", "Prepared by Plant Controlling")
    _table_grid(
        s1,
        72,
        y - 20,
        (90, 90, 90, 90, 100),
        ("Line", "Material", "Labor", "Overhead", "Unit Cost"),
        (
            ("L1", "4.20", "3.10", "1.55", "8.85"),
            ("L2", "5.75", "2.40", "1.30", "9.45"),
            ("L3", "3.60", FIN02_L3_LABOR, "1.75", FIN02_L3_UNIT_COST),
            ("L4", "6.10", "3.55", "1.90", "11.55"),
        ),
        FIN02_TABLE_TITLE,
    )
    s1.wrap(72, 380, "Overhead is allocated by machine hours. Unit costs exclude packaging and freight.", width=440)

    s2 = d.page()
    _title_block(s2, "Vertex Manufacturing Cost Analysis", "Trend")
    _line_chart(
        s2,
        90,
        240,
        430,
        280,
        FIN02_TREND_TITLE,
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun"),
        {
            "L1": (9.60, 9.45, 9.30, 9.20, 9.15, float(FIN02_TREND_FINAL_L1)),
            "L4": (12.10, 11.95, 11.80, 11.70, 11.62, 11.55),
        },
        "L1",
        FIN02_TREND_FINAL_L1,
    )
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-fin-02", pdf_bytes=pdf_bytes, pages=pages)


def _build_rd_results(
    doc_key: str,
    product: str,
    pie_title: str,
    table_title: str,
    categories: Sequence[tuple[str, float]],
    b102_total: str,
    b102_firmware: str,
    b102_display: str,
) -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    _title_block(s1, f"Product {product} Test Results", "Quality Engineering")
    _pie_chart(s1, 200, 380, 110, pie_title, categories)

    s2 = d.page()
    y = _title_block(s2, f"Product {product} Test Results", "Defect ledger")
    _table_grid(
        s2,
        72,
        y - 20,
        (90, 80, 80, 90, 100, 80, 70),
        ("Build", "Firmware", "Display", "Battery", "Connectivity", "Mechanical", "Total"),
        (
            ("B-101", "12", "8", "6", "5", "4", "35"),
            ("B-102", b102_firmware, b102_display, "7", "6", "5", b102_total),
            ("B-103", "9", "6", "5", "4", "3", "27"),
            ("B-104", "7", "5", "4", "3", "2", "21"),
        ),
        table_title,
    )
    s2.wrap(72, 380, f"Counts are unique confirmed defects per build of the {product} hardware revision.", width=440)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key=doc_key, pdf_bytes=pdf_bytes, pages=pages)


def _build_ops01() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, OPS01_FORM_TITLE, "Form HT-07-C")
    labels = (
        ("Maintenance budget (USD)", "120000", OPS01_APPROVED_BUDGET),
        ("Inspection cycle (days)", "30", "45"),
        ("Contractor", "Ace Mechanical", "Ace Mechanical"),
        ("Next outage window", "Jun-10", OPS01_APPROVED_OUTAGE),
    )
    # two side-by-side value columns carrying IDENTICAL labels: only the
    # horizontal position of a value binds it to "Proposed" or "Approved".
    sheet = s1
    sheet.text(120, y - 10, "Proposed", size=11, bold=True)
    sheet.text(380, y - 10, "Approved", size=11, bold=True)
    sheet.c.setLineWidth(1.0)
    sheet.c.line(360, y - 16, 360, y - 16 - len(labels) * 34 - 8)
    yy = y - 44
    for label, proposed, approved in labels:
        sheet.text(120, yy, label, size=10)
        sheet.text(270, yy, proposed, size=10, bold=True)
        sheet.text(380, yy, approved, size=10, bold=True)
        yy -= 34
    sheet.c.setLineWidth(0.8)

    s2 = d.page()
    y2 = _title_block(s2, OPS01_FORM_TITLE, "Checklist and sign-off")
    items = (
        "Pressure vessels inspected within rating",
        "Emergency stop chain tested",
        "Thermal sensors calibrated",
        "Ventilation scrubber media replaced",
    )
    for i, item in enumerate(items):
        s2.c.rect(90, y2 - 10 - i * 26, 10, 10, fill=0, stroke=1)
        s2.text(108, y2 - 8 - i * 26, item, size=10)
    s2.text(90, y2 - 130, "Technician signature", size=10)
    s2.text(270, y2 - 130, OPS01_TECH_SIGNED, size=10, bold=True)
    s2.text(90, y2 - 160, "Reviewer signature", size=10)
    s2.text(270, y2 - 160, OPS01_REVIEWER_SIGNED, size=10, bold=True)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-ops-01", pdf_bytes=pdf_bytes, pages=pages)


def _build_ops02() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    _title_block(s1, "Kantai Logistics Route Plan", "Depot network, cycle 41")
    # Column B (right) is drawn FIRST, then column A (left). The oracle
    # transcript therefore lists Route B runs before Route A runs, exactly
    # like a draw-order extraction; binding a stop to a route needs layout.
    right_x, left_x = 330, 80
    s1.text(right_x, 620, "Route B", size=12, bold=True)
    for i, stop in enumerate(
        (OPS02_ROUTE_B_FIRST_STOP, "Cold Storage 4", "Retail Park KX", OPS02_ROUTE_B_FINAL_STOP)
    ):
        s1.text(right_x, 590 - i * 26, f"Stop {i + 1}: {stop}", size=10)
    s1.text(right_x, 460, "Total drive time: 4h15m", size=10)
    s1.text(left_x, 620, "Route A", size=12, bold=True)
    for i, stop in enumerate(("Depot North", "Bakery Row", "Campus Dock 2", "Return Depot North")):
        s1.text(left_x, 590 - i * 26, f"Stop {i + 1}: {stop}", size=10)
    s1.text(left_x, 460, f"Total drive time: {OPS02_ROUTE_A_DRIVE}", size=10)
    s2 = d.page()
    y2 = _title_block(s2, "Kantai Logistics Route Plan", "Driver notes")
    s2.wrap(72, y2, "Cyclone season contingency: Route B cold-chain stops require pre-cooled boxes when ambient exceeds 30C. Route A campus dock closes at 15:30.", width=450)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-ops-02", pdf_bytes=pdf_bytes, pages=pages)


def _build_hr01() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, "Employee Handbook Excerpt", "Sections 7 and 12")
    s1.wrap(72, y, f"Annual leave ({HR01_LEAVE_CODE}): full-time employees accrue 22 days per year. Up to {HR01_CARRYOVER} unused days may carry over to the next calendar year; the remainder expires.", width=460)
    s1.wrap(72, y - 70, "Sick leave: employees report absence before 09:00 through the attendance portal. Medical certificates are required beyond two consecutive days.", width=460)
    s2 = d.page()
    y2 = _title_block(s2, "Employee Handbook Excerpt", "Remote work")
    s2.wrap(72, y2, f"Remote work policy ({HR01_REMOTE_CODE}): up to three remote days per week with manager approval. Fully remote assignments require an HR exception record.", width=460)
    s2.wrap(72, y2 - 70, "Equipment: home office stipend of 400 USD is granted once per employment and requires returning all dock hardware on departure.", width=460)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-hr-01", pdf_bytes=pdf_bytes, pages=pages)


def _build_hr02() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    _title_block(s1, "Org Chart - Operations Division", "Reporting lines, effective Q3")
    box = s1.box
    box(231, 660, 150, 34, fill=_PALETTE[0], label=f"VP Operations - {HR02_VP}", label_size=9, bold=True)
    for i, (name, role) in enumerate(
        (("Priya Nair", "Dir Manufacturing"), (HR02_QUALITY_DIRECTOR, "Dir Quality"), ("Lena Fischer", "Dir Logistics"))
    ):
        bx = 56 + i * 170
        s1.c.setLineWidth(0.9)
        s1.c.line(306, 660, 306, 630)
        s1.c.line(bx + 75, 630, 431 - 170 + 75, 630)
        s1.c.line(bx + 75, 630, bx + 75, 612)
        box(bx, 578, 150, 34, fill=_PALETTE[2], label=f"{role} - {name}", label_size=8)
    for i, name in enumerate(("R. Osei", "T. Wald", "K. Ito", "M. Brandt")):
        bx = 40 + i * 135
        s1.c.line(bx + 62, 578, bx + 62, 552)
        box(bx, 518, 124, 34, label=f"Line Supervisor {name}", label_size=7)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-hr-02", pdf_bytes=pdf_bytes, pages=pages)


def _build_mfg01() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    _title_block(s1, "Assembly Line 4 Status Dashboard", "Shift 2, live indicators")
    kpis = (("Throughput", "412 units/hr"), ("Defect rate", "1.8%"), ("OEE", MFG01_OEE))
    for i, (label, value) in enumerate(kpis):
        s1.box(60 + i * 170, 560, 150, 70, fill=_PALETTE[0] if i == 0 else None, label=label, label_size=10, bold=True)
        s1.text(60 + i * 170 + 75, 580, value, size=14, bold=True, center=True)
    # gauge
    s1.c.setLineWidth(6)
    s1.c.setStrokeColor(HexColor("#aaaaaa"))
    s1.c.arc(90, 300, 290, 470, 180, 180)
    s1.c.setStrokeColor(_PALETTE[1])
    s1.c.arc(90, 300, 290, 470, 180, 180 * 0.72)
    s1.c.setStrokeColor(HexColor("#000000"))
    s1.c.setLineWidth(0.9)
    s1.text(190, 330, f"Line load {MFG01_LOAD}", size=11, bold=True, center=True)
    # sparkline
    points = (0.3, 0.42, 0.38, 0.5, 0.47, 0.58, 0.55)
    for (v1, v2) in zip(points, points[1:]):
        s1.c.line(330 + 20 * points.index(v1), 340 + v1 * 120, 330 + 20 * (points.index(v1) + 1), 340 + v2 * 120)
    s1.text(350, 290, "Throughput sparkline (6h)", size=9)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-mfg-01", pdf_bytes=pdf_bytes, pages=pages)


def _build_rev01(version: str, audit: str, findings: str) -> CorpusArtifact:
    d = _new_doc()
    doc_key = f"doc-rev-01-{version}"
    s1 = d.page()
    y = _title_block(s1, f"Safety Compliance Checklist {version}", "Facility HT-04")
    fields = (
        ("Responsible officer", "J. Alvarez"),
        ("Last external audit", audit),
        ("Open findings", findings),
        ("Fire drill due", REV01_DRILL_DUE),
    )
    for i, (label, value) in enumerate(fields):
        s1.text(90, y - 10 - i * 30, label, size=11)
        s1.text(300, y - 10 - i * 30, value, size=11, bold=True)
    s2 = d.page()
    y2 = _title_block(s2, f"Safety Compliance Checklist {version}", "Register")
    s2.wrap(72, y2, "The register lists every finding with owner and due date. Findings closed by the responsible officer require evidence attachments before status change.", width=450)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key=doc_key, pdf_bytes=pdf_bytes, pages=pages)


def _build_sec01() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, "Confidential Board Memo - Restructuring", "Distribution: board only")
    s1.wrap(72, y, f"The restructuring program provides a severance budget provision of {SEC01_SEVERANCE} USD covering all affected regions.", width=450)
    s1.wrap(72, y - 50, f"Restructuring effective date: {SEC01_EFFECTIVE}. Site leads communicate staffing plans one week before effectiveness.", width=450)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-sec-01", pdf_bytes=pdf_bytes, pages=pages)


def _build_sec02() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, "Confidential Payroll Schedule", "People Operations")
    s1.wrap(72, y, f"Payroll effective date: {PAYROLL_EFFECTIVE}. The schedule below lists compensation bands for restricted distribution.", width=450)
    _table_grid(
        s1,
        72,
        y - 60,
        (120, 100, 120),
        ("Band", "Min (USD)", "Max (USD)"),
        (
            ("P1", "48000", "62000"),
            ("P2", "63000", "85000"),
            ("P3", "86000", "118000"),
        ),
    )
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-sec-02", pdf_bytes=pdf_bytes, pages=pages)


def _build_pub03() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, "Public Payroll Summary", "People Operations")
    s1.wrap(72, y, f"Payroll effective date: {PAYROLL_EFFECTIVE}. Headcount 214 across three sites. Band boundaries are published in the annual report appendix.", width=450)
    s1.wrap(72, y - 50, "Individual compensation records remain restricted. Questions route to People Operations.", width=450)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-pub-03", pdf_bytes=pdf_bytes, pages=pages)


def _build_leg01() -> CorpusArtifact:
    d = _new_doc()
    s1 = d.page()
    y = _title_block(s1, "Lease Agreement - Elm Street Unit", "Pages 1-2 of 6")
    s1.wrap(72, y, f"Monthly rent is {LEG01_RENT} USD payable on the first business day. Lease term: {LEG01_TERM} months commencing 2026-07-01.", width=450)
    s2 = d.page()
    y2 = _title_block(s2, "Lease Agreement - Elm Street Unit", "Termination")
    s2.wrap(72, y2, f"Early termination is governed by {LEG01_EARLY_TERM_CLAUSE}: notice period 60 days plus one month rent equivalent.", width=450)
    pdf_bytes, pages = d.finish()
    return CorpusArtifact(doc_key="doc-leg-01", pdf_bytes=pdf_bytes, pages=pages)


# ---------------------------------------------------------------------------
# Query set. Gold answers are drawn from the value constants above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    text: str
    slice_tag: str
    doc_id: str
    page_number: int
    answer: str
    answer_kind: str
    phase: str = "baseline"
    profile: str = "default"
    expectation: str = "answer"


def _queries() -> tuple[QuerySpec, ...]:
    q = (
        # -- text.easy_control: unique-token targets on plain-text pages ----
        QuerySpec("q01", "What is the document reference code of the regional sales report for 2024?", "text.easy_control", "doc-fin-01", 1, FIN01_REF_CODE, "string"),
        QuerySpec("q02", "Which policy code governs remote work in the employee handbook?", "text.easy_control", "doc-hr-01", 2, HR01_REMOTE_CODE, "string"),
        QuerySpec("q03", "How many unused annual leave days may carry over to the next year?", "text.easy_control", "doc-hr-01", 1, HR01_CARRYOVER, "count"),
        QuerySpec("q04", "What is the monthly rent in the Elm Street lease agreement?", "text.easy_control", "doc-leg-01", 1, LEG01_RENT, "decimal"),
        QuerySpec("q05", "What is the current OEE shown on the Line 4 status dashboard?", "text.easy_control", "doc-mfg-01", 1, MFG01_OEE, "percent"),
        QuerySpec("q06", "Which clause code governs early termination of the Elm Street lease?", "text.easy_control", "doc-leg-01", 2, LEG01_EARLY_TERM_CLAUSE, "string"),
        # -- chart.appearance: page must be found through its visual form ---
        QuerySpec("q07", "Find the page that draws groups of vertical bars comparing revenue across territories by quarter. What value does the tallest bar show?", "chart.appearance", "doc-fin-01", 2, FIN01_BAR_TOP_VALUE, "decimal"),
        QuerySpec("q08", "Find the page with a circular chart divided into wedges showing issue share for the Beta product. Which category is the largest wedge?", "chart.appearance", "doc-rd-01", 1, RD01_TOP_SLICE, "string"),
        QuerySpec("q09", "Find the diagram of connected boxes showing reporting lines in the operations division. Who is the Quality director?", "chart.appearance", "doc-hr-02", 1, HR02_QUALITY_DIRECTOR, "string"),
        QuerySpec("q10", "Find the chart drawn with connected line segments showing cost change over months. What is the final month unit cost of line L1?", "chart.appearance", "doc-fin-02", 2, FIN02_TREND_FINAL_L1, "decimal"),
        QuerySpec("q11", "Find the status panel with big number tiles and a semicircular dial. What load percentage does the dial show?", "chart.appearance", "doc-mfg-01", 1, MFG01_LOAD, "percent"),
        # -- chart.value_read: gold page is a chart, answer read from it ---
        QuerySpec("q12", "Which region label sits on the tallest bar of the quarterly revenue chart in the 2024 sales report?", "chart.value_read", "doc-fin-01", 2, FIN01_BAR_TOP_REGION, "string"),
        QuerySpec("q13", "In the Beta issue distribution chart, which category owns the largest share?", "chart.value_read", "doc-rd-01", 1, RD01_TOP_SLICE, "string"),
        QuerySpec("q14", "In the operations org chart, who holds the Quality director seat?", "chart.value_read", "doc-hr-02", 1, HR02_QUALITY_DIRECTOR, "string"),
        # -- table.cell_grid: binding inside a dense grid -------------------
        QuerySpec("q15", "What are the 2024 sales units for SKU WT-300 in the West region?", "table.cell_grid", "doc-fin-01", 3, FIN01_WT300_WEST, "decimal"),
        QuerySpec("q16", "What is the labor cost for production line L3?", "table.cell_grid", "doc-fin-02", 1, FIN02_L3_LABOR, "decimal"),
        QuerySpec("q17", "What is the total number of defects recorded for build B-102 in the Beta test results?", "table.cell_grid", "doc-rd-01", 2, RD01_B102_TOTAL, "count"),
        QuerySpec("q18", "Which production line has the lowest unit cost?", "table.cell_grid", "doc-fin-02", 1, "L3", "string"),
        # -- form.label_placement: identical labels, position binds --------
        QuerySpec("q19", "What is the APPROVED maintenance budget on the HeatTech inspection form (not the proposed one)?", "form.label_placement", "doc-ops-01", 1, OPS01_APPROVED_BUDGET, "decimal"),
        QuerySpec("q20", "What is the APPROVED next outage window on the HeatTech inspection form?", "form.label_placement", "doc-ops-01", 1, OPS01_APPROVED_OUTAGE, "string"),
        QuerySpec("q21", "On which date did the REVIEWER (not the technician) sign the inspection form?", "form.label_placement", "doc-ops-01", 2, OPS01_REVIEWER_SIGNED, "string"),
        # -- layout.column_bind: two-column page, draw-order transcript ----
        QuerySpec("q22", "What is the first stop of Route B in the logistics route plan?", "layout.column_bind", "doc-ops-02", 1, OPS02_ROUTE_B_FIRST_STOP, "string"),
        QuerySpec("q23", "What is the total drive time of Route A in the logistics route plan?", "layout.column_bind", "doc-ops-02", 1, OPS02_ROUTE_A_DRIVE, "string"),
        QuerySpec("q24", "What is the final stop of Route B in the logistics route plan?", "layout.column_bind", "doc-ops-02", 1, OPS02_ROUTE_B_FINAL_STOP, "string"),
        # -- near_duplicate.decoy: near-identical template pages -----------
        QuerySpec("q25", "In the 2023 regional sales report, which region sits on the tallest bar of the quarterly revenue chart?", "near_duplicate.decoy", "doc-fin-03", 2, FIN03_BAR_TOP_REGION, "string"),
        QuerySpec("q26", "What are the 2023 sales units for SKU WT-300 in the West region?", "near_duplicate.decoy", "doc-fin-03", 3, FIN03_WT300_WEST, "decimal"),
        QuerySpec("q27", "What is the total number of defects recorded for build B-102 in the GAMMA test results?", "near_duplicate.decoy", "doc-rd-02", 2, RD02_B102_TOTAL, "count"),
        QuerySpec("q28", "In the BETA (not Gamma) issue distribution chart, which category owns the largest share?", "near_duplicate.decoy", "doc-rd-01", 1, RD01_TOP_SLICE, "string"),
        # -- revision.change: same source, two content revisions -----------
        QuerySpec("q29", "How many open findings does the safety compliance checklist list?", "revision.change", "doc-rev-01", 1, REV01_V3_FINDINGS, "count", phase="pre_revision"),
        QuerySpec("q30", "How many open findings does the safety compliance checklist list?", "revision.change", "doc-rev-01", 1, REV01_V4_FINDINGS, "count", phase="post_revision"),
        QuerySpec("q31", "What is the date of the last external audit according to the current safety compliance checklist?", "revision.change", "doc-rev-01", 1, REV01_V4_AUDIT, "date", phase="post_revision"),
        QuerySpec("q32", "How many open findings does the safety compliance checklist list?", "revision.change", "doc-rev-01", 1, REV01_V3_FINDINGS, "count", phase="pinned_pre_revision"),
        # -- authz.revocation: restricted domain holds the best evidence ---
        QuerySpec("q33", "What is the severance budget provision in the restructuring memo?", "authz.revocation", "doc-sec-01", 1, SEC01_SEVERANCE, "money_million", profile="allowed"),
        QuerySpec("q34", "What is the severance budget provision in the restructuring memo?", "authz.revocation", "doc-sec-01", 1, "", "money_million", profile="denied", expectation="no_delivery"),
        QuerySpec("q35", "What is the payroll effective date?", "authz.revocation", "doc-pub-03", 1, PAYROLL_EFFECTIVE, "date", profile="denied"),
    )
    return q


QUERIES: tuple[QuerySpec, ...] = _queries()

ANSWER_KINDS = frozenset(
    {"string", "decimal", "count", "percent", "date", "money_million"}
)
PHASES = frozenset(
    {"baseline", "pre_revision", "post_revision", "pinned_pre_revision"}
)
PROFILES = frozenset({"default", "allowed", "denied"})
EXPECTATIONS = frozenset({"answer", "no_delivery"})


# ---------------------------------------------------------------------------
# Build-all entry point
# ---------------------------------------------------------------------------


def build_all() -> dict[str, CorpusArtifact]:
    """Build every corpus document deterministically."""
    rl_config.invariant = 1
    artifacts: dict[str, CorpusArtifact] = {}
    builders: tuple[Callable[[], CorpusArtifact], ...] = (
        lambda: _build_fin_report(
            "doc-fin-01",
            "Northwind Regional Sales Report 2024",
            "2024",
            FIN01_REF_CODE,
            FIN01_REVENUE,
            FIN01_BAR_TITLE,
            FIN01_BAR_TOP_REGION,
            FIN01_BAR_TOP_VALUE,
            FIN01_TABLE_TITLE,
            FIN01_WT300_WEST,
            regions_west=4.0,
            regions_east=3.3,
        ),
        lambda: _build_fin_report(
            "doc-fin-03",
            "Northwind Regional Sales Report 2023",
            "2023",
            FIN03_REF_CODE,
            "41.6M",
            FIN03_BAR_TITLE,
            FIN03_BAR_TOP_REGION,
            FIN03_BAR_TOP_VALUE,
            FIN03_TABLE_TITLE,
            FIN03_WT300_WEST,
            regions_west=3.2,
            regions_east=3.8,
        ),
        _build_fin02,
        lambda: _build_rd_results(
            "doc-rd-01",
            "Beta",
            RD01_PIE_TITLE,
            RD01_TABLE_TITLE,
            (
                ("Firmware", 34),
                ("Display", 22),
                ("Battery", 18),
                ("Connectivity", 14),
                ("Mechanical", 12),
            ),
            RD01_B102_TOTAL,
            b102_firmware="16",
            b102_display="13",
        ),
        lambda: _build_rd_results(
            "doc-rd-02",
            "Gamma",
            RD02_PIE_TITLE,
            RD02_TABLE_TITLE,
            (
                ("Display", 31),
                ("Firmware", 26),
                ("Battery", 19),
                ("Connectivity", 13),
                ("Mechanical", 11),
            ),
            RD02_B102_TOTAL,
            b102_firmware="11",
            b102_display="12",
        ),
        _build_ops01,
        _build_ops02,
        _build_hr01,
        _build_hr02,
        _build_mfg01,
        lambda: _build_rev01("v3", REV01_V3_AUDIT, REV01_V3_FINDINGS),
        lambda: _build_rev01("v4", REV01_V4_AUDIT, REV01_V4_FINDINGS),
        _build_sec01,
        _build_sec02,
        _build_pub03,
        _build_leg01,
    )
    for builder in builders:
        artifact = builder()
        if artifact.doc_key in artifacts:
            raise ValueError(f"duplicate doc_key: {artifact.doc_key}")
        artifacts[artifact.doc_key] = artifact
    return artifacts
