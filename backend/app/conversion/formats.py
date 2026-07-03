"""Canonical output format constants shared across API surfaces."""

from __future__ import annotations

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
