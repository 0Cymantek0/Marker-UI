"""Validation heuristics for specialist OCR output."""

from __future__ import annotations

import re

from app.hybrid_ocr.contracts import TargetKind, ValidationReport


def _repeat_ratio(text: str) -> float:
    tokens = re.findall(r"\w+", text.lower())
    if len(tokens) < 6:
        return 0.0
    repeats = sum(1 for a, b in zip(tokens, tokens[1:]) if a == b)
    return repeats / max(1, len(tokens) - 1)


def _gibberish_ratio(text: str) -> float:
    if not text:
        return 1.0
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\t\r")
    bad += sum(1 for ch in text if ch == "\ufffd")
    return bad / max(1, len(text))


def validate_text(text: str, *, baseline_text: str = "", threshold: float = 0.70) -> ValidationReport:
    normalized = text.strip()
    checks = {
        "non_empty": bool(normalized),
        "not_repetitive": _repeat_ratio(normalized) < 0.25,
        "low_gibberish": _gibberish_ratio(normalized) < 0.05,
        "not_suspiciously_short": len(normalized) >= min(12, max(0, len(baseline_text.strip()) // 4)),
    }
    score = sum(checks.values()) / len(checks)
    return ValidationReport(
        accepted=score >= threshold and all(checks.values()),
        score=score,
        checks=checks,
        reasons=[name for name, ok in checks.items() if not ok],
        normalized_text_len=len(normalized),
        output_shape={"chars": len(normalized), "lines": len(normalized.splitlines())},
    )


def validate_table(markdown: str, *, baseline_text: str = "", threshold: float = 0.75) -> ValidationReport:
    normalized = markdown.strip()
    rows = [line for line in normalized.splitlines() if "|" in line]
    cell_counts = [len([cell for cell in row.split("|") if cell.strip()]) for row in rows]
    checks = {
        "non_empty": bool(normalized),
        "has_table_rows": len(rows) >= 2,
        "has_non_empty_cells": any(count >= 2 for count in cell_counts),
        "not_repetitive": _repeat_ratio(normalized) < 0.25,
        "not_suspiciously_short": len(normalized) >= min(12, max(0, len(baseline_text.strip()) // 4)),
    }
    score = sum(checks.values()) / len(checks)
    return ValidationReport(
        accepted=score >= threshold and checks["has_table_rows"] and checks["has_non_empty_cells"],
        score=score,
        checks=checks,
        reasons=[name for name, ok in checks.items() if not ok],
        normalized_text_len=len(normalized),
        output_shape={"rows": len(rows), "max_cells": max(cell_counts or [0])},
    )


def validate_formula(text: str, *, threshold: float = 0.70) -> ValidationReport:
    normalized = text.strip()
    checks = {
        "non_empty": bool(normalized),
        "balanced_braces": normalized.count("{") == normalized.count("}"),
        "not_long_prose": len(re.findall(r"[A-Za-z]{4,}", normalized)) <= 6,
        "not_repetitive": _repeat_ratio(normalized) < 0.25,
    }
    score = sum(checks.values()) / len(checks)
    return ValidationReport(
        accepted=score >= threshold and all(checks.values()),
        score=score,
        checks=checks,
        reasons=[name for name, ok in checks.items() if not ok],
        normalized_text_len=len(normalized),
        output_shape={"chars": len(normalized)},
    )


def validate_for_kind(kind: TargetKind, text: str, markdown: str, *, baseline_text: str = "") -> ValidationReport:
    if kind == TargetKind.TABLE:
        return validate_table(markdown or text, baseline_text=baseline_text)
    if kind == TargetKind.FORMULA:
        return validate_formula(markdown or text)
    return validate_text(text or markdown, baseline_text=baseline_text)

