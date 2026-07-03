"""Per-format output store — the "no reconverting" cache.

A conversion job produces output for one or more formats (markdown/json/html/
chunks). This module is the single source of truth for the cached
``{format: text}`` map serialised into the ``formats_json`` column on
``ConversionJob``. The data survives restarts, so switching a preview tab reads
from here instead of re-running the conversion.

Why a dedicated store: the legacy job model has ``result_text`` (one string)
and ``result_path`` (one file on disk). Multi-format output does not fit either
— a job can have markdown *and* json ready at once. ``formats_json`` keeps an
explicit ``{format: text}`` map; the writer persists one primary file on disk
and every requested format's text lives in this cache for instant tab switching.

The persisted shape is intentionally flat (``{format: text}``) so the status
endpoint, finalize path, and regenerate endpoint all share one trivial
parse/merge contract.
"""

from __future__ import annotations

import json
from typing import Any

from app.conversion.formats import OUTPUT_FORMATS, OUTPUT_FORMAT_SET, normalize_output_formats

# The formats this store knows how to persist and serve.
SUPPORTED_FORMATS = OUTPUT_FORMATS


def normalize_formats(formats: Any) -> list[str]:
    """Dedupe + drop unknowns, preserving first-seen order.

    A client can send ``["markdown", "markdown", "nonsense"]`` — collapse it to
    ``["markdown"]`` so a bad request never poisons the store or crashes a render.
    """
    if not formats:
        return []
    return normalize_output_formats(formats)


def parse_formats(formats_json: str | None) -> dict[str, str] | None:
    """Parse the cached ``{format: text}`` map.

    Returns ``None`` when the row has no cache (legacy single-format jobs, or
    jobs that never completed), so the status response omits the field and the
    UI falls back to the single-format preview. Corrupt JSON also degrades to
    ``None`` rather than crashing every history row.
    """
    if not formats_json:
        return None
    try:
        parsed = json.loads(formats_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def merge_formats(
    existing: dict[str, str] | None,
    additions: dict[str, str],
) -> dict[str, str]:
    """Merge new ``{format: text}`` entries into an existing cache.

    Already-present formats are preserved (regeneration only appends). Unknown
    formats are dropped so a malformed payload can never add a tab marker cannot
    render. Insertion order is stable: existing formats first, then new ones.
    """
    merged: dict[str, str] = {}
    for fmt, text in (existing or {}).items():
        if fmt in OUTPUT_FORMAT_SET and text is not None:
            merged[fmt] = str(text)
    for fmt, text in additions.items():
        fmt_s = str(fmt).strip().lower()
        if fmt_s in OUTPUT_FORMAT_SET and text is not None:
            merged[fmt_s] = str(text)
    return merged


def available_formats(formats_json: str | None, fallback: str | None) -> list[str]:
    """The ordered list of formats currently viewable for a job.

    Derived from the cache keys when present; for legacy single-format jobs the
    cache is absent, so we fall back to ``output_format`` so older jobs still
    expose exactly one tab.
    """
    cached = parse_formats(formats_json)
    if cached is not None:
        return list(cached.keys())
    fmt = (fallback or "markdown").strip().lower()
    return [fmt] if fmt else ["markdown"]


def serialize(formats: dict[str, str] | None) -> str | None:
    """Serialise a ``{format: text}`` map for the ``formats_json`` column.

    Returns ``None`` for an empty map so legacy rows stay NULL.
    """
    if not formats:
        return None
    cleaned = {str(k): str(v) for k, v in formats.items() if v is not None and k in OUTPUT_FORMAT_SET}
    if not cleaned:
        return None
    return json.dumps(cleaned, ensure_ascii=False)
