"""Task-level value normalization for the PR80B benchmark.

These are the DECLARED task conventions from the corpus manifest,
applied identically to gold and to every system's output so the
comparison surface is fair. They intentionally differ from the PR80A
production parser (which is deliberately strict): a system that emits
"03/15/2026" is scored against the task normalization, while the
PR80A route's own acceptance semantics are recorded separately by
the lane adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Sequence

_US_THOUSANDS = re.compile(r"^([+-]?)\d{1,3}(,\d{3})+(\.\d+)?$")
_EU_DECIMAL = re.compile(r"^([+-]?)\d{1,3}(\.\d{3})+,\d{2}$")
_EU_PLAIN_COMMA = re.compile(r"^([+-]?)\d+,\d{2}$")
_US_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_PLAIN_INTEGER = re.compile(r"^[+-]?\d+$")

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH_NAME_DATE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")

_CURRENCY_MAP = {
    "usd": "USD",
    "$": "USD",
    "us$": "USD",
    "us dollars": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "eur": "EUR",
    "€": "EUR",
    "euros": "EUR",
    "euro": "EUR",
    "gbp": "GBP",
    "£": "GBP",
    "pounds sterling": "GBP",
    "pound": "GBP",
}


@dataclass(frozen=True)
class NormResult:
    """One normalization outcome; exactly one of value/error is set."""

    value: str | int | Decimal | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def canonical(self) -> str:
        """Stable string form for artifacts and comparisons."""
        if self.value is None:
            return ""
        if isinstance(self.value, Decimal):
            return format(self.value.normalize(), "f")
        return str(self.value)


def normalize_string(raw: object) -> NormResult:
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return NormResult(error="empty value")
    return NormResult(value=text)


def normalize_integer(raw: object) -> NormResult:
    text = str(raw).strip()
    if _PLAIN_INTEGER.fullmatch(text):
        return NormResult(value=int(text))
    if _US_THOUSANDS.fullmatch(text) and "." not in text:
        return NormResult(value=int(text.replace(",", "")))
    return NormResult(error="not a base-10 integer")


def normalize_decimal(raw: object) -> NormResult:
    text = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return NormResult(error="empty value")
    if _EU_DECIMAL.fullmatch(text):
        sign = "-" if text.startswith("-") else "+"
        body = text.lstrip("+-")
        converted = sign + body.replace(".", "").replace(",", ".")
        try:
            return NormResult(value=Decimal(converted))
        except InvalidOperation:
            return NormResult(error="not a decimal number")
    if _EU_PLAIN_COMMA.fullmatch(text):
        try:
            return NormResult(value=Decimal(text.replace(",", ".")))
        except InvalidOperation:
            return NormResult(error="not a decimal number")
    if _US_THOUSANDS.fullmatch(text):
        try:
            return NormResult(value=Decimal(text.replace(",", "")))
        except InvalidOperation:
            return NormResult(error="not a decimal number")
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return NormResult(error="not a decimal number")
    if not parsed.is_finite():
        return NormResult(error="non-finite decimal")
    return NormResult(value=parsed)


def normalize_date(raw: object) -> NormResult:
    text = str(raw).strip()
    if not text:
        return NormResult(error="empty value")
    try:
        return NormResult(value=date.fromisoformat(text).isoformat())
    except ValueError:
        pass
    slash = _US_SLASH_DATE.fullmatch(text)
    if slash is not None:
        month, day, year = (int(group) for group in slash.groups())
        try:
            return NormResult(value=date(year, month, day).isoformat())
        except ValueError:
            return NormResult(error="invalid US M/D/YYYY date")
    month_name = _MONTH_NAME_DATE.fullmatch(text)
    if month_name is not None:
        month = _MONTHS.get(month_name.group(1).lower())
        if month is None:
            return NormResult(error="unknown month name")
        day = int(month_name.group(2))
        year = int(month_name.group(3))
        try:
            return NormResult(value=date(year, month, day).isoformat())
        except ValueError:
            return NormResult(error="invalid Month D, YYYY date")
    return NormResult(error="unrecognized date format")


def normalize_currency(raw: object) -> NormResult:
    text = str(raw).strip()
    if not text:
        return NormResult(error="empty value")
    mapped = _CURRENCY_MAP.get(text.lower())
    if mapped is None:
        return NormResult(error=f"unrecognized currency {text!r}")
    return NormResult(value=mapped)


def normalize_by_type(
    field_type: str,
    raw: object,
    enum_values: Sequence[str] = (),
) -> NormResult:
    """Dispatch one raw value under the declared task normalization."""
    if field_type == "string":
        return normalize_string(raw)
    if field_type == "integer":
        return normalize_integer(raw)
    if field_type == "decimal":
        return normalize_decimal(raw)
    if field_type == "date":
        return normalize_date(raw)
    if field_type == "enum":
        if str(raw).strip() in enum_values:
            return NormResult(value=str(raw).strip())
        return normalize_currency(raw)
    return NormResult(error=f"unsupported field type {field_type!r}")
