"""Deterministic PR72 tracer fixtures: a two-column PDF and a native docx.

Both builders are pure functions producing byte-identical artifacts on
every call (the PDF xref is assembled from computed offsets; the docx
zip entries carry fixed timestamps), so the same fixture always yields
the same content revision blob key and therefore the same anchor ids —
the property the durability/restart tests rely on.

The PDF carries four text runs in two columns plus two drawn
rectangles; the docx carries three bookmarked paragraphs and one
anchored drawing with native EMU geometry.
"""

from __future__ import annotations

import io
import zipfile

PDF_TWO_COLUMN_CONTENT = (
    b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Left column first line.) Tj ET\n"
    b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (Left column second line.) Tj ET\n"
    b"BT /F1 12 Tf 1 0 0 1 316 720 Tm (Right column first line.) Tj ET\n"
    b"BT /F1 12 Tf 1 0 0 1 316 700 Tm (Right column second line.) Tj ET\n"
    b"72 640 200 12 re S\n"
    b"316 640 200 12 re S\n"
)


def build_two_column_pdf() -> bytes:
    """Hand-assemble a minimal one-page PDF with a computed xref table."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(PDF_TWO_COLUMN_CONTENT)).encode()
        + b" >>\nstream\n"
        + PDF_TWO_COLUMN_CONTENT
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_position = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_position)
    )
    return bytes(out)


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>\n"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>\n"
)

_DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    "<w:body>"
    '<w:p><w:bookmarkStart w:id="0" w:name="intro"/>'
    "<w:r><w:t>First paragraph carries a native bookmark.</w:t></w:r>"
    '<w:bookmarkEnd w:id="0"/></w:p>'
    '<w:p><w:bookmarkStart w:id="1" w:name="chart_anchor"/>'
    "<w:r><w:drawing>"
    '<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
    '<wp:simplePos x="0" y="0"/>'
    '<wp:positionH relativeFrom="column"><wp:posOffset>1828800</wp:posOffset></wp:positionH>'
    '<wp:positionV relativeFrom="paragraph"><wp:posOffset>457200</wp:posOffset></wp:positionV>'
    '<wp:extent cx="914400" cy="457200"/>'
    '<wp:wrapNone/>'
    '<wp:docPr id="7" name="Chart Frame"/>'
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"/></a:graphic>'
    "</wp:anchor>"
    "</w:drawing></w:r>"
    '<w:bookmarkEnd w:id="1"/></w:p>'
    '<w:p><w:bookmarkStart w:id="2" w:name="summary"/>'
    "<w:r><w:t>Third paragraph closes the body.</w:t></w:r>"
    '<w:bookmarkEnd w:id="2"/></w:p>'
    "</w:body></w:document>\n"
)

#: Fixed zip metadata so regeneration is byte-identical.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def build_native_docx() -> bytes:
    """Assemble a minimal OOXML package with fixed zip metadata."""
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "word/document.xml": _DOCUMENT_XML,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name in sorted(parts):
            info = zipfile.ZipInfo(filename=name, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            package.writestr(info, parts[name].encode("utf-8"))
    return buffer.getvalue()
