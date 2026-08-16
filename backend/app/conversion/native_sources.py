"""Native source-fact extraction for the PDF/Office tracer bullet (PR72).

This module is the honest seam between real source artifacts and
SourceAnchor selector facts. It extracts only what the artifact itself
declares:

* PDF (via pypdf): page count, media boxes, and content-stream facts —
  text-run baseline origins (``Tm`` + ``Tj``) and drawn rectangles
  (``re``). Coordinates stay exact decimal *text* from the file so the
  fixed-point quantizer sees the written value, never a float64
  round-trip. Font metrics are not guessed: text geometry is a
  baseline-origin point, rectangles are exact boxes.
* OOXML packages (stdlib zipfile + ElementTree): the package part
  list, body paragraph order, ``w:bookmarkStart`` native identities,
  and ``wp:anchor`` drawing geometry in native EMU integers.

No Markdown laundering, no fabricated fidelity: facts absent from the
artifact are absent from the anchors built on top of them.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

_W = "{%s}" % _W_NS
_WP = "{%s}" % _WP_NS

_NUMBER = r"-?\d+(?:\.\d+)?"
_RECT_RE = re.compile(rf"({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+re\b")
_TM_RE = re.compile(rf"({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+Tm\b")
_TJ_RE = re.compile(r"\((.*?)\)\s*Tj\b")
_BLOCK_RE = re.compile(r"BT(.*?)ET", re.DOTALL)


@dataclass(frozen=True)
class PdfTextRun:
    """One text-showing operation with its baseline origin.

    ``origin_x``/``origin_y`` are the exact decimal text written in the
    content stream (PDF user-space points, y-up by default).
    """

    page_number: int
    origin_x: str
    origin_y: str
    text: str


@dataclass(frozen=True)
class PdfRectangle:
    """One ``x y w h re`` rectangle (exact decimal operands)."""

    page_number: int
    x: str
    y: str
    width: str
    height: str


@dataclass(frozen=True)
class PdfPageFacts:
    page_number: int
    media_box: tuple[str, ...]
    text_runs: tuple[PdfTextRun, ...]
    rectangles: tuple[PdfRectangle, ...]


@dataclass(frozen=True)
class PdfDocumentFacts:
    pages: tuple[PdfPageFacts, ...]


def _unescape_pdf_string(raw: str) -> str:
    return raw.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")


def extract_pdf_facts(data: bytes) -> PdfDocumentFacts:
    """Extract page/content-stream facts from a PDF artifact."""
    from pypdf import PdfReader  # deferred: heavy import, PDF-only need

    reader = PdfReader(io.BytesIO(data))
    pages: list[PdfPageFacts] = []
    for page_number, page in enumerate(reader.pages, start=1):
        media_box = tuple(str(component) for component in page.mediabox)
        contents = page.get_contents()
        stream = contents.get_data().decode("latin-1") if contents is not None else ""

        text_runs: list[PdfTextRun] = []
        for block in _BLOCK_RE.finditer(stream):
            body = block.group(1)
            tm = _TM_RE.search(body)
            if tm is None:
                continue
            origin_x, origin_y = tm.group(5), tm.group(6)
            for shown in _TJ_RE.finditer(body):
                text_runs.append(
                    PdfTextRun(
                        page_number=page_number,
                        origin_x=origin_x,
                        origin_y=origin_y,
                        text=_unescape_pdf_string(shown.group(1)),
                    )
                )

        rectangles = tuple(
            PdfRectangle(
                page_number=page_number,
                x=match.group(1),
                y=match.group(2),
                width=match.group(3),
                height=match.group(4),
            )
            for match in _RECT_RE.finditer(stream)
        )
        pages.append(
            PdfPageFacts(
                page_number=page_number,
                media_box=media_box,
                text_runs=tuple(text_runs),
                rectangles=rectangles,
            )
        )
    return PdfDocumentFacts(pages=tuple(pages))


@dataclass(frozen=True)
class DocxBookmark:
    paragraph_index: int
    bookmark_id: str
    name: str


@dataclass(frozen=True)
class DocxDrawing:
    """An anchored drawing's native EMU geometry."""

    paragraph_index: int
    offset_x_emu: int
    offset_y_emu: int
    extent_cx_emu: int
    extent_cy_emu: int


@dataclass(frozen=True)
class DocxParagraph:
    index: int
    text: str
    bookmarks: tuple[DocxBookmark, ...]
    drawings: tuple[DocxDrawing, ...]


@dataclass(frozen=True)
class DocxPackageFacts:
    package_parts: tuple[str, ...]
    paragraphs: tuple[DocxParagraph, ...]


def extract_docx_facts(data: bytes) -> DocxPackageFacts:
    """Extract package/bookmark/EMU facts from an OOXML docx artifact."""
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        parts = tuple(sorted(package.namelist()))
        document = package.read("word/document.xml")

    import xml.etree.ElementTree as ET

    root = ET.fromstring(document)
    body = root.find(_W + "body")
    paragraphs: list[DocxParagraph] = []
    if body is not None:
        for index, paragraph in enumerate(body.findall(_W + "p")):
            text = "".join(
                node.text or "" for node in paragraph.iter(_W + "t")
            )
            bookmarks = tuple(
                DocxBookmark(
                    paragraph_index=index,
                    bookmark_id=marker.get(_W + "id", ""),
                    name=marker.get(_W + "name", ""),
                )
                for marker in paragraph.iter(_W + "bookmarkStart")
            )
            drawings = []
            for anchor in paragraph.iter(_WP + "anchor"):
                offset_x = anchor.findtext(_WP + "positionH/" + _WP + "posOffset")
                offset_y = anchor.findtext(_WP + "positionV/" + _WP + "posOffset")
                extent = anchor.find(_WP + "extent")
                drawings.append(
                    DocxDrawing(
                        paragraph_index=index,
                        offset_x_emu=int(offset_x) if offset_x is not None else 0,
                        offset_y_emu=int(offset_y) if offset_y is not None else 0,
                        extent_cx_emu=int(extent.get("cx", "0")) if extent is not None else 0,
                        extent_cy_emu=int(extent.get("cy", "0")) if extent is not None else 0,
                    )
                )
            paragraphs.append(
                DocxParagraph(
                    index=index,
                    text=text,
                    bookmarks=bookmarks,
                    drawings=tuple(drawings),
                )
            )
    return DocxPackageFacts(package_parts=parts, paragraphs=tuple(paragraphs))
