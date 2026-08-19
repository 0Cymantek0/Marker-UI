"""Deterministic typed parsing and invariant evaluation (PR80A).

Parsing is strict: a raw string either parses cleanly under the field's
declared type or yields a precise error. Nothing is coerced, trimmed
into correctness, or silently defaulted — an invalid candidate stays
visible to reconciliation as invalid rather than disappearing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping

from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FieldOutcome,
    InvariantFinding,
    ItemOutcome,
)
from app.extraction.schema import ExtractionSchema, FieldSpec

_INTEGER_RE = re.compile(r"[+-]?\d+")


@dataclass(frozen=True)
class ParsedValue:
    """A typed parse result; exactly one of value/error is set."""

    value: str | int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_typed(raw: str, spec: FieldSpec) -> ParsedValue:
    """Parse ``raw`` under the field's declared type, fail-closed."""
    text = raw.strip()
    if not text:
        return ParsedValue(error="empty value")
    if spec.type == "string":
        if spec.pattern is not None and re.fullmatch(spec.pattern, text) is None:
            return ParsedValue(error=f"value does not match pattern {spec.pattern!r}")
        return ParsedValue(value=text)
    if spec.type == "integer":
        if not _INTEGER_RE.fullmatch(text):
            return ParsedValue(error="not a base-10 integer")
        return ParsedValue(value=int(text))
    if spec.type == "decimal":
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return ParsedValue(error="not a decimal number")
        if not parsed.is_finite():
            return ParsedValue(error="non-finite decimal")
        # Canonical decimal string keeps trailing-zero fidelity of the
        # source (``9.90`` stays ``9.90``) — identity must not normalize
        # away what the document actually said.
        return ParsedValue(value=text)
    if spec.type == "date":
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return ParsedValue(error="not an ISO-8601 date (YYYY-MM-DD)")
        return ParsedValue(value=parsed_date.isoformat())
    if spec.type == "enum":
        if text not in spec.enum_values:
            return ParsedValue(
                error=f"value not in enum {sorted(spec.enum_values)}"
            )
        return ParsedValue(value=text)
    return ParsedValue(error=f"unsupported field type {spec.type!r}")


def decimal_of(value: str | int) -> Decimal:
    """Convert an accepted typed value to Decimal for invariant math."""
    return Decimal(str(value))


def evaluate_invariants(
    schema: ExtractionSchema,
    fields: Mapping[str, object],
    line_items: Mapping[str, tuple[ItemOutcome, ...]],
) -> tuple[InvariantFinding, ...]:
    """Evaluate schema invariants over accepted output values.

    Honesty rules baked in:

    * a violated invariant downgrades nothing silently here — the
      finding is reported and the caller decides escalation;
    * an invariant is ``not_evaluable`` (never ``satisfied``) when any
      value it needs is missing, unresolved, or any required row is not
      accepted: a document-level total must never masquerade as proof
      that incomplete row evidence was fine.
    """
    findings: list[InvariantFinding] = []
    for invariant in schema.invariants:
        target_outcome = fields.get(invariant.target)
        rows = line_items.get(invariant.items, ())
        if not isinstance(target_outcome, FieldOutcome) or not rows:
            findings.append(
                InvariantFinding(
                    kind=invariant.kind,
                    target=invariant.target,
                    finding="not_evaluable",
                    detail="target value or item rows unavailable",
                )
            )
            continue
        usable_rows = [row for row in rows if row.status == FIELD_OUTCOME_ACCEPTED]
        if target_outcome.status != FIELD_OUTCOME_ACCEPTED or len(usable_rows) != len(rows):
            findings.append(
                InvariantFinding(
                    kind=invariant.kind,
                    target=invariant.target,
                    finding="not_evaluable",
                    detail=(
                        "target or row values are not fully accepted; a "
                        "document-level total cannot prove incomplete rows"
                    ),
                )
            )
            continue
        try:
            target_value = decimal_of(target_outcome.value)  # type: ignore[arg-type]
            row_sum = sum(
                (
                    decimal_of(row.fields[invariant.item_field].value)  # type: ignore[arg-type]
                    for row in usable_rows
                ),
                Decimal(0),
            )
            tolerance = Decimal(invariant.tolerance)
        except (KeyError, InvalidOperation):
            findings.append(
                InvariantFinding(
                    kind=invariant.kind,
                    target=invariant.target,
                    finding="not_evaluable",
                    detail="accepted values are not decimal-comparable",
                )
            )
            continue
        if abs(target_value - row_sum) <= tolerance:
            findings.append(
                InvariantFinding(
                    kind=invariant.kind,
                    target=invariant.target,
                    finding="satisfied",
                    detail=f"target {target_value} equals row sum {row_sum}",
                )
            )
        else:
            findings.append(
                InvariantFinding(
                    kind=invariant.kind,
                    target=invariant.target,
                    finding="violated",
                    detail=f"target {target_value} != row sum {row_sum}",
                )
            )
    return tuple(findings)
