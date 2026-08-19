"""Task-level answer normalization for the PR81A benchmark.

Applied identically to gold answers and every route's emitted answer.
Rules are declared once here so no lane can privately redefine equality.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_KINDS = frozenset({"string", "decimal", "count", "percent", "date", "money_million"})

_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[.\;,]+$")
_NUMERIC_NOISE = re.compile(r"(?i)(usd|\$|,)")
_SUFFIX_M = re.compile(r"(?i)\s*m(?=$|\s)")
_PERCENT = re.compile(r"%")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NormalizeError(ValueError):
    """Raised when a value cannot be normalized under its declared kind."""


def normalize_answer(value: str, kind: str) -> str:
    """Normalize one answer under ``kind``; returns canonical string form."""
    if kind not in _KINDS:
        raise NormalizeError(f"unknown answer kind: {kind!r}")
    if not isinstance(value, str):
        raise NormalizeError(f"answer must be a string, got {type(value).__name__}")
    text = _WS.sub(" ", value).strip()
    text = _TRAILING_PUNCT.sub("", text)
    if not text:
        raise NormalizeError("empty answer")
    if kind == "string":
        return text.lower()
    if kind == "date":
        if not _ISO_DATE.match(text):
            raise NormalizeError(f"date must be YYYY-MM-DD, got {text!r}")
        return text
    cleaned = _NUMERIC_NOISE.sub("", text)
    if kind == "percent":
        cleaned = _PERCENT.sub("", cleaned)
    if kind == "money_million":
        cleaned = _SUFFIX_M.sub("", cleaned)
    cleaned = cleaned.strip()
    try:
        dec = Decimal(cleaned)
    except InvalidOperation as exc:
        raise NormalizeError(f"{kind} answer is not numeric: {text!r}") from exc
    if kind == "count":
        if dec != dec.to_integral_value():
            raise NormalizeError(f"count answer is not an integer: {text!r}")
        return str(int(dec))
    normalized = dec.normalize()
    return f"{normalized:f}"
