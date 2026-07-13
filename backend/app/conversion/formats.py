"""Canonical output format constants shared across API surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal


OutputFormat = Literal["markdown", "json", "html", "chunks"]

OUTPUT_FORMATS: tuple[OutputFormat, ...] = ("markdown", "json", "html", "chunks")
OUTPUT_FORMAT_SET = frozenset(OUTPUT_FORMATS)
OUTPUT_FORMATS_DESCRIPTION = ", ".join(OUTPUT_FORMATS)

MARKER_MULTI_FORMAT_EXTENSIONS = frozenset({
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tiff",
    ".bmp",
    ".gif",
    ".epub",
})


@dataclass(frozen=True)
class InputFormatSpec:
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]
    engine: str
    label: str
    category: str
    needs_marker_models: bool
    needs_gpu: bool
    confidence: float
    content_type_extensions: tuple[str, ...] | None = None
    upload_allowed: bool = True
    url_allowed: bool = True

    def __post_init__(self) -> None:
        if self.content_type_extensions is None:
            return
        if len(self.content_type_extensions) != len(self.mime_types):
            raise ValueError(f"{self.label} content_type_extensions must match mime_types")
        unknown = [ext for ext in self.content_type_extensions if ext not in self.extensions]
        if unknown:
            raise ValueError(f"{self.label} maps MIME type(s) to unknown extension(s): {unknown}")


INPUT_FORMATS: tuple[InputFormatSpec, ...] = (
    InputFormatSpec(
        extensions=(".pdf",),
        mime_types=("application/pdf",),
        engine="marker_pdf",
        label="Marker PDF",
        category="document",
        needs_marker_models=True,
        needs_gpu=True,
        confidence=0.75,
    ),
    InputFormatSpec(
        extensions=(".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp", ".gif"),
        mime_types=("image/jpeg", "image/png", "image/webp", "image/tiff", "image/bmp", "image/gif"),
        engine="marker_pdf",
        label="Marker Image OCR",
        category="image",
        needs_marker_models=True,
        needs_gpu=True,
        confidence=1.0,
        content_type_extensions=(
            ".jpg",
            ".png",
            ".webp",
            ".tiff",
            ".bmp",
            ".gif",
        ),
    ),
    InputFormatSpec(
        extensions=(".epub",),
        mime_types=("application/epub+zip",),
        engine="marker_pdf",
        label="Marker EPUB",
        category="ebook",
        needs_marker_models=True,
        needs_gpu=True,
        confidence=1.0,
    ),
    InputFormatSpec(
        extensions=(".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"),
        mime_types=(
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/mp4",
            "audio/x-m4a",
            "audio/flac",
            "audio/ogg",
            "audio/aac",
        ),
        engine="audio",
        label="Local Audio Transcript",
        category="audio",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
        content_type_extensions=(
            ".wav",
            ".wav",
            ".mp3",
            ".m4a",
            ".m4a",
            ".flac",
            ".ogg",
            ".aac",
        ),
    ),
    InputFormatSpec(
        extensions=(".mp4", ".mov", ".mkv", ".webm", ".avi"),
        mime_types=("video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo"),
        engine="video",
        label="Local Video Timeline",
        category="video",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.90,
        content_type_extensions=(
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".avi",
        ),
    ),
    InputFormatSpec(
        extensions=(".docx",),
        mime_types=("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
        engine="office_docx",
        label="Fast Office (Word)",
        category="office",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
    ),
    InputFormatSpec(
        extensions=(".pptx",),
        mime_types=("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
        engine="office_pptx",
        label="Fast Office (PowerPoint)",
        category="office",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
    ),
    InputFormatSpec(
        extensions=(".msg",),
        mime_types=("application/vnd.ms-outlook",),
        engine="outlook_msg",
        label="Outlook MSG",
        category="email",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
    ),
    InputFormatSpec(
        extensions=(".xlsx", ".xls"),
        mime_types=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel"),
        engine="spreadsheet",
        label="Fast Spreadsheet",
        category="office",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
        content_type_extensions=(".xlsx", ".xls"),
    ),
    InputFormatSpec(
        extensions=(".csv", ".tsv"),
        mime_types=("text/csv", "text/tab-separated-values"),
        engine="text_data",
        label="Text / Data",
        category="data",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
        content_type_extensions=(".csv", ".tsv"),
    ),
    InputFormatSpec(
        extensions=(".json", ".jsonl"),
        mime_types=("application/json", "application/x-ndjson"),
        engine="text_data",
        label="Text / Data (JSON)",
        category="data",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
        content_type_extensions=(".json", ".jsonl"),
    ),
    InputFormatSpec(
        extensions=(".xml", ".rss", ".atom"),
        mime_types=("application/xml", "text/xml", "application/rss+xml", "application/atom+xml"),
        engine="xml_rss",
        label="XML / RSS",
        category="data",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.90,
        content_type_extensions=(".xml", ".xml", ".rss", ".atom"),
    ),
    InputFormatSpec(
        extensions=(".html", ".htm"),
        mime_types=("text/html", "application/xhtml+xml"),
        engine="html",
        label="HTML",
        category="document",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.90,
        content_type_extensions=(".html", ".html"),
    ),
    InputFormatSpec(
        extensions=(".txt", ".md", ".rst", ".log"),
        mime_types=("text/plain", "text/markdown", "text/x-rst"),
        engine="text_data",
        label="Plain Text",
        category="text",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=1.0,
        content_type_extensions=(".txt", ".md", ".rst"),
    ),
    InputFormatSpec(
        extensions=(".ipynb",),
        mime_types=("application/x-ipynb+json",),
        engine="notebook",
        label="Jupyter Notebook",
        category="notebook",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.95,
    ),
    InputFormatSpec(
        extensions=(".zip",),
        mime_types=("application/zip",),
        engine="archive",
        label="Archive (ZIP)",
        category="archive",
        needs_marker_models=False,
        needs_gpu=False,
        confidence=0.90,
    ),
)

INPUT_FORMAT_BY_EXTENSION = {
    ext: spec
    for spec in INPUT_FORMATS
    for ext in spec.extensions
}
UPLOAD_ALLOWED_EXTENSIONS = frozenset(
    ext
    for spec in INPUT_FORMATS
    if spec.upload_allowed
    for ext in spec.extensions
)
URL_ALLOWED_EXTENSIONS = frozenset(
    ext
    for spec in INPUT_FORMATS
    if spec.url_allowed
    for ext in spec.extensions
)
CONTENT_TYPE_EXTENSION_MAP = {
    mime_type: extension
    for spec in INPUT_FORMATS
    for mime_type, extension in zip(
        spec.mime_types,
        spec.content_type_extensions or (spec.extensions[0],) * len(spec.mime_types),
    )
}


def normalize_output_formats(formats: Iterable[Any] | None) -> list[str]:
    """Dedupe supported output formats, preserving first-seen order."""
    if not formats:
        return []
    seen: list[str] = []
    for fmt in formats:
        fmt_s = str(fmt).strip().lower()
        if fmt_s in OUTPUT_FORMAT_SET and fmt_s not in seen:
            seen.append(fmt_s)
    return seen


def requested_output_formats_from_config(config: dict[str, Any]) -> list[str]:
    """Return supported requested formats from a conversion config."""
    raw = config.get("output_formats")
    if isinstance(raw, list) and raw:
        formats = normalize_output_formats(raw)
        if formats:
            return formats
    return normalize_output_formats([config.get("output_format") or "markdown"]) or ["markdown"]


def renderable_output_formats_for_extensions(extensions: Iterable[str]) -> list[OutputFormat]:
    """Return truthful output formats for one input extension group.

    Marker-backed PDF/image/EPUB routes can render every public output format.
    Native Markdown-only routes can render Markdown plus derived semantic chunks;
    JSON/HTML need a structured renderer and must not be advertised there.
    """

    normalized = {str(ext).strip().lower() for ext in extensions}
    if normalized and normalized <= MARKER_MULTI_FORMAT_EXTENSIONS:
        return list(OUTPUT_FORMATS)
    return ["markdown", "chunks"]


def renderable_output_formats_for_engine(
    engine: str,
    extensions: Iterable[str],
) -> list[OutputFormat]:
    """Return output formats for a concrete converter engine.

    Extension-level capability answers "can this input type render JSON/HTML
    through some route?" Converter-level capability must describe the selected
    engine itself. A clean PDF can force Marker for JSON/HTML, but LiteParse as
    an engine still emits Markdown plus derived chunks only.
    """

    if str(engine).strip().lower() == "marker_pdf":
        return renderable_output_formats_for_extensions(extensions)
    return ["markdown", "chunks"]
