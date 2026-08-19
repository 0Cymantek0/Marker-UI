"""Deterministic scoring and failure taxonomy for the PR80B benchmark.

Scoring is a pure function of (gold, system output): no clocks, no
randomness, no ambient state. Running it twice over frozen inputs
yields byte-identical results, which the benchmark runner asserts.

The taxonomy separates dangerous failures (fabricated values,
confident resolutions of document-internal conflicts, cross-row
contamination, silent contradictions) from ordinary errors (wrong
value, missed row) and from honest abstentions (missing with a flag),
so an aggregate accuracy number can never hide them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.eval.pr80b.corpus import (
    ENUM_VALUES,
    ROW_FIELDS,
    SCALAR_FIELDS,
)
from app.eval.pr80b.normalize import NormResult, normalize_by_type

#: All scoreable field names (scalars + row members) to their task type.
_FIELD_TYPES = {**SCALAR_FIELDS, **ROW_FIELDS}

#: System-side emission statuses.
EMITTED = "emitted"
ABSENT = "absent"
FLAGGED_CONFLICT = "flagged_conflict"

#: Scalar outcome classes.
OUTCOME_CORRECT = "correct"
OUTCOME_CORRECT_ABSENT = "correct_absent"
OUTCOME_WRONG_VALUE = "wrong_value"
OUTCOME_FABRICATED = "fabricated"
OUTCOME_MISSING = "missing"
OUTCOME_MISSING_FLAGGED = "missing_flagged"
OUTCOME_INVALID = "invalid"
OUTCOME_FALSE_CONFLICT = "false_conflict"
OUTCOME_HONEST_CONFLICT = "honest_conflict"
OUTCOME_CONFLICT_VALUE_FLAGGED = "conflict_value_flagged"
OUTCOME_CONFLICT_CONFIDENT = "confident_on_conflict"
OUTCOME_SILENT_MISSING_CONFLICT = "silent_missing_conflict"

#: Row outcome classes.
ROW_EXACT = "exact"
ROW_PARTIAL = "partial"
ROW_MISSED = "missed"
ROW_HALLUCINATED = "hallucinated"
ROW_DUPLICATE = "duplicate_emitted"
ROW_CONFLICT_HONEST = "conflict_honest"
ROW_CONFLICT_CONFIDENT = "conflict_confident"

#: Row field outcomes that keep a matched row "exact".
ROW_OK_OUTCOMES = {OUTCOME_CORRECT, OUTCOME_CORRECT_ABSENT}


@dataclass(frozen=True)
class EmittedField:
    """One field as a system reported it (pre-normalization)."""

    status: str
    value: Any = None
    has_evidence: bool = False
    self_flagged: bool = False
    note: str = ""


@dataclass(frozen=True)
class EmittedRow:
    """One line-item row as a system reported it."""

    sku: str | None
    fields: Mapping[str, EmittedField]
    status: str = EMITTED
    self_flagged: bool = False


@dataclass(frozen=True)
class SystemDocOutput:
    """One system's output for one document, lane-normalized."""

    system_id: str
    doc_id: str
    fields: Mapping[str, EmittedField]
    rows: tuple[EmittedRow, ...]
    run_status: str | None = None
    invariant_findings: Mapping[str, str] | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScalarResult:
    field: str
    gold_status: str
    system_status: str
    outcome: str
    normalized_gold: str
    normalized_system: str
    detail: str


@dataclass(frozen=True)
class RowResult:
    sku: str
    outcome: str
    field_outcomes: tuple[tuple[str, str], ...]
    cross_row_fields: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class DocScore:
    doc_id: str
    system_id: str
    error: str | None
    scalars: tuple[ScalarResult, ...]
    rows: tuple[RowResult, ...]
    extra_rows: tuple[str, ...]
    invariant: dict[str, Any]
    doc_exact: bool
    counts: dict[str, int]
    evidence: dict[str, int]
    review: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "system_id": self.system_id,
            "error": self.error,
            "scalars": [
                {
                    "field": r.field,
                    "gold_status": r.gold_status,
                    "system_status": r.system_status,
                    "outcome": r.outcome,
                    "normalized_gold": r.normalized_gold,
                    "normalized_system": r.normalized_system,
                    "detail": r.detail,
                }
                for r in self.scalars
            ],
            "rows": [
                {
                    "sku": r.sku,
                    "outcome": r.outcome,
                    "field_outcomes": dict(r.field_outcomes),
                    "cross_row_fields": list(r.cross_row_fields),
                    "detail": r.detail,
                }
                for r in self.rows
            ],
            "extra_rows": list(self.extra_rows),
            "invariant": dict(self.invariant),
            "doc_exact": self.doc_exact,
            "counts": dict(self.counts),
            "evidence": dict(self.evidence),
            "review": dict(self.review),
        }


def _absent_field() -> EmittedField:
    return EmittedField(status=ABSENT)


def _normalize(field_type: str, value: Any) -> NormResult:
    return normalize_by_type(field_type, value, ENUM_VALUES)


def _norm_equal(field_type: str, gold_value: Any, system_value: Any) -> tuple[bool, str, str]:
    gold_norm = _normalize(field_type, gold_value)
    sys_norm = _normalize(field_type, system_value)
    gold_text = gold_norm.canonical() if gold_norm.ok else str(gold_value)
    sys_text = sys_norm.canonical() if sys_norm.ok else str(system_value)
    if not gold_norm.ok or not sys_norm.ok:
        return False, gold_text, sys_text
    return gold_norm.value == sys_norm.value, gold_text, sys_text


def _score_scalar(
    name: str,
    gold_entry: Mapping[str, Any],
    system_field: EmittedField,
) -> ScalarResult:
    field_type = _FIELD_TYPES[name]
    gold_status = gold_entry.get("status")

    if gold_status == "conflicting":
        gold_values = list(gold_entry.get("values", []))
        if system_field.status == FLAGGED_CONFLICT or (
            system_field.status == ABSENT and system_field.self_flagged
        ):
            outcome = OUTCOME_HONEST_CONFLICT
            detail = "document conflict flagged honestly"
        elif system_field.status == ABSENT:
            outcome = OUTCOME_SILENT_MISSING_CONFLICT
            detail = f"document conflicts on {gold_values}; system silently emitted nothing"
        else:
            in_values = any(
                _norm_equal(field_type, value, system_field.value)[0] for value in gold_values
            )
            if system_field.self_flagged:
                outcome = OUTCOME_CONFLICT_VALUE_FLAGGED
                detail = f"emitted a conflicting candidate {system_field.value!r} but flagged it"
            else:
                outcome = OUTCOME_CONFLICT_CONFIDENT
                detail = (
                    f"confidently emitted {system_field.value!r} for a conflicting field "
                    f"(document states {gold_values})"
                )
            if not in_values:
                detail += "; emitted value is not even one of the stated candidates"
        return ScalarResult(
            field=name,
            gold_status=gold_status,
            system_status=system_field.status,
            outcome=outcome,
            normalized_gold="|".join(str(v) for v in gold_values),
            normalized_system=str(system_field.value) if system_field.value is not None else "",
            detail=detail,
        )

    if gold_status == "absent":
        reported_absent = system_field.status == ABSENT or (
            system_field.status == FLAGGED_CONFLICT and system_field.value is None
        )
        if reported_absent:
            outcome = OUTCOME_CORRECT_ABSENT
            detail = "correctly reported absent"
            if system_field.status == FLAGGED_CONFLICT:
                detail = "reported absent via conflict flag (conservative)"
        else:
            outcome = OUTCOME_FABRICATED
            detail = f"document does not state this field; system emitted {system_field.value!r}"
        return ScalarResult(
            field=name,
            gold_status=gold_status,
            system_status=system_field.status,
            outcome=outcome,
            normalized_gold="",
            normalized_system=str(system_field.value) if system_field.value is not None else "",
            detail=detail,
        )

    # gold present
    gold_value = gold_entry.get("value")
    if system_field.status == FLAGGED_CONFLICT and system_field.value is None:
        outcome = OUTCOME_FALSE_CONFLICT
        detail = "document states a single value; system flagged a conflict"
        sys_text = ""
    elif system_field.status == ABSENT:
        if system_field.self_flagged:
            outcome = OUTCOME_MISSING_FLAGGED
            detail = "honest abstention/flag; value present in document"
        else:
            outcome = OUTCOME_MISSING
            detail = "value present in document; system emitted nothing"
        sys_text = ""
    else:
        equal, gold_text, sys_text = _norm_equal(field_type, gold_value, system_field.value)
        if not equal:
            sys_norm = _normalize(field_type, system_field.value)
            if not sys_norm.ok:
                outcome = OUTCOME_INVALID
                detail = f"emitted value fails task normalization: {sys_norm.error}"
            else:
                outcome = OUTCOME_WRONG_VALUE
                detail = f"expected {gold_text}, got {sys_text}"
        else:
            outcome = OUTCOME_CORRECT
            detail = "normalized values agree"
        return ScalarResult(
            field=name,
            gold_status=gold_status,
            system_status=system_field.status,
            outcome=outcome,
            normalized_gold=gold_text,
            normalized_system=sys_text,
            detail=detail,
        )
    return ScalarResult(
        field=name,
        gold_status=gold_status,
        system_status=system_field.status,
        outcome=outcome,
        normalized_gold=str(gold_value),
        normalized_system=sys_text,
        detail=detail,
    )


def _score_row(
    gold_row: Mapping[str, Any],
    system_row: EmittedRow,
    gold_rows_by_sku: Mapping[str, Mapping[str, Any]],
) -> tuple[RowResult, list[ScalarResult]]:
    sku = gold_row.get("sku", "")
    expected_conflict = bool(gold_row.get("expected_conflict", False))
    field_results: list[ScalarResult] = []
    cross_row: list[str] = []
    sys_fields = system_row.fields
    for name in sorted(ROW_FIELDS):
        gold_entry = gold_row["fields"][name]
        system_field = sys_fields.get(name, _absent_field())
        result = _score_scalar(name, gold_entry, system_field)
        field_results.append(result)
        if result.outcome != OUTCOME_WRONG_VALUE:
            continue
        for other_sku, other_row in gold_rows_by_sku.items():
            if other_sku == sku:
                continue
            other_value = other_row["fields"][name].get("value")
            if other_value is None:
                continue
            if _norm_equal(ROW_FIELDS[name], other_value, system_field.value)[0]:
                cross_row.append(name)
                break

    outcomes = {r.outcome for r in field_results}
    conflict_classes = {
        OUTCOME_HONEST_CONFLICT,
        OUTCOME_CONFLICT_VALUE_FLAGGED,
        OUTCOME_CONFLICT_CONFIDENT,
        OUTCOME_SILENT_MISSING_CONFLICT,
    }
    if expected_conflict and outcomes & conflict_classes:
        confident = OUTCOME_CONFLICT_CONFIDENT in outcomes or (
            OUTCOME_SILENT_MISSING_CONFLICT in outcomes
        )
        outcome = ROW_CONFLICT_CONFIDENT if confident else ROW_CONFLICT_HONEST
        detail = "gold row is document-internal conflict; handling scored above"
    elif outcomes <= ROW_OK_OUTCOMES:
        outcome = ROW_EXACT
        detail = ""
    else:
        outcome = ROW_PARTIAL
        wrong = [r.field for r in field_results if r.outcome not in ROW_OK_OUTCOMES]
        detail = f"wrong fields: {wrong}"
    row_result = RowResult(
        sku=sku,
        outcome=outcome,
        field_outcomes=tuple((r.field, r.outcome) for r in field_results),
        cross_row_fields=tuple(sorted(set(cross_row))),
        detail=detail,
    )
    return row_result, field_results


def _invariant_score(
    gold: Mapping[str, Any],
    out: SystemDocOutput,
) -> dict[str, Any]:
    expected = gold["invariant"]["expected"]
    route_reported = None
    if out.invariant_findings:
        route_reported = out.invariant_findings.get("total_due")
    if route_reported is None:
        invariant_outcome = "not_reported"
    elif route_reported == expected:
        invariant_outcome = "reported_match"
    else:
        invariant_outcome = "reported_mismatch"

    # Recompute the sum relationship from the system's OWN emitted data.
    emitted_total = out.fields.get("total_due", _absent_field())
    total_norm = (
        _normalize("decimal", emitted_total.value)
        if emitted_total.status != ABSENT and emitted_total.value is not None
        else None
    )
    row_sum: Decimal | None = None
    evaluable = total_norm is not None and total_norm.ok
    if evaluable:
        row_sum = Decimal(0)
        for row in out.rows:
            amount = row.fields.get("amount", _absent_field())
            if amount.status == ABSENT or amount.value is None:
                evaluable = False
                break
            amount_norm = _normalize("decimal", amount.value)
            if not amount_norm.ok or not isinstance(amount_norm.value, Decimal):
                evaluable = False
                break
            row_sum += amount_norm.value
    if not evaluable or row_sum is None:
        scorer_computed = "unevaluable"
        contradiction = "none"
    elif abs(total_norm.value - row_sum) <= Decimal("0.01"):  # type: ignore[operator]
        scorer_computed = "consistent"
        contradiction = "none"
    elif route_reported is None:
        scorer_computed = "mismatch"
        contradiction = "silent_contradiction"
    elif route_reported == "satisfied":
        scorer_computed = "mismatch"
        contradiction = "false_satisfied"
    else:
        scorer_computed = "mismatch"
        contradiction = "flagged"
    return {
        "expected": expected,
        "route_reported": route_reported,
        "outcome": invariant_outcome,
        "scorer_computed": scorer_computed,
        "contradiction_class": contradiction,
    }


def score_document(gold: Mapping[str, Any], out: SystemDocOutput) -> DocScore:
    """Score one system output against one validated gold document."""
    scalar_results: list[ScalarResult] = []
    for name in sorted(SCALAR_FIELDS):
        gold_entry = gold["fields"][name]
        system_field = out.fields.get(name, _absent_field())
        scalar_results.append(_score_scalar(name, gold_entry, system_field))

    gold_rows = list(gold["items"]["rows"])
    gold_rows_by_sku = {row["sku"]: row for row in gold_rows}
    row_results: list[RowResult] = []
    all_field_results: list[ScalarResult] = list(scalar_results)
    for gold_row in gold_rows:
        sku = gold_row["sku"]
        system_row = None
        for candidate in out.rows:
            if candidate.sku == sku:
                system_row = candidate
                break
        if system_row is None:
            expected_conflict = bool(gold_row.get("expected_conflict", False))
            outcome = ROW_MISSED
            detail = "gold row not extracted"
            field_outcomes: tuple[tuple[str, str], ...] = tuple(
                (name, OUTCOME_MISSING) for name in sorted(ROW_FIELDS)
            )
            if expected_conflict:
                outcome = ROW_CONFLICT_HONEST
                detail = "conflicting gold row; system emitted no row (conservative)"
            row_results.append(
                RowResult(
                    sku=sku,
                    outcome=outcome,
                    field_outcomes=field_outcomes,
                    cross_row_fields=(),
                    detail=detail,
                )
            )
            continue
        row_result, field_results = _score_row(gold_row, system_row, gold_rows_by_sku)
        row_results.append(row_result)
        all_field_results.extend(field_results)

    seen_system_skus: set[str] = set()
    extra_rows: list[str] = []
    duplicate_rows: list[str] = []
    for system_row in out.rows:
        label = system_row.sku if system_row.sku is not None else "<no-sku>"
        if system_row.sku is None or system_row.sku not in gold_rows_by_sku:
            extra_rows.append(label)
        elif system_row.sku in seen_system_skus:
            duplicate_rows.append(label)
        seen_system_skus.add(system_row.sku or "")
    for dup in duplicate_rows:
        row_results.append(
            RowResult(
                sku=dup,
                outcome=ROW_DUPLICATE,
                field_outcomes=(),
                cross_row_fields=(),
                detail="system emitted the same sku more than once",
            )
        )

    invariant = _invariant_score(gold, out)

    scalar_outcomes = [r.outcome for r in scalar_results]
    field_outcomes_flat = [
        outcome for result in row_results for _, outcome in result.field_outcomes
    ]
    row_outcomes = [r.outcome for r in row_results]
    ok_scalar = {
        OUTCOME_CORRECT,
        OUTCOME_CORRECT_ABSENT,
        OUTCOME_HONEST_CONFLICT,
        OUTCOME_CONFLICT_VALUE_FLAGGED,
    }
    ok_rows = {ROW_EXACT, ROW_CONFLICT_HONEST}
    # doc_exact measures extraction exactness only; invariant honesty and
    # contradiction surfacing stay visible in the invariant/danger metrics
    # so systems without invariant machinery are not double-penalized here.
    doc_exact = (
        out.error is None
        and not extra_rows
        and not duplicate_rows
        and all(o in ok_scalar for o in scalar_outcomes)
        and all(o in ok_rows for o in row_outcomes)
    )

    counts: dict[str, int] = {}
    for outcome in scalar_outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    for outcome in field_outcomes_flat:
        counts[outcome] = counts.get(outcome, 0) + 1
    for outcome in row_outcomes:
        counts[f"row.{outcome}"] = counts.get(f"row.{outcome}", 0) + 1
    if extra_rows:
        counts[f"row.{ROW_HALLUCINATED}"] = (
            counts.get(f"row.{ROW_HALLUCINATED}", 0) + len(extra_rows)
        )

    emitted_fields = [
        f
        for f in list(out.fields.values()) + [rf for r in out.rows for rf in r.fields.values()]
        if f.status != ABSENT and f.value is not None
    ]
    evidence = {
        "emitted": len(emitted_fields),
        "with_evidence": sum(1 for f in emitted_fields if f.has_evidence),
    }
    review = {
        "self_flagged": sum(
            1
            for f in list(out.fields.values())
            + [rf for r in out.rows for rf in r.fields.values()]
            if f.self_flagged
        ),
        "unverified_emitted": sum(1 for f in emitted_fields if not f.has_evidence),
    }
    return DocScore(
        doc_id=out.doc_id,
        system_id=out.system_id,
        error=out.error,
        scalars=tuple(scalar_results),
        rows=tuple(row_results),
        extra_rows=tuple(extra_rows),
        invariant=invariant,
        doc_exact=doc_exact,
        counts=counts,
        evidence=evidence,
        review=review,
    )


_DANGER_CLASSES = (
    OUTCOME_FABRICATED,
    OUTCOME_CONFLICT_CONFIDENT,
    OUTCOME_SILENT_MISSING_CONFLICT,
    "row." + ROW_HALLUCINATED,
    "row." + ROW_DUPLICATE,
)


def aggregate_metrics(
    doc_scores: Sequence[DocScore],
    slices_by_doc: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Aggregate per-document scores into per-system metrics."""
    scalar_counts: dict[str, int] = {}
    row_counts: dict[str, int] = {}
    row_field_counts: dict[str, int] = {}
    for score in doc_scores:
        for result in score.scalars:
            scalar_counts[result.outcome] = scalar_counts.get(result.outcome, 0) + 1
        for key, value in score.counts.items():
            if key.startswith("row."):
                row_counts[key[len("row."):]] = row_counts.get(key[len("row."):], 0) + value
        for row in score.rows:
            for _, outcome in row.field_outcomes:
                row_field_counts[outcome] = row_field_counts.get(outcome, 0) + 1
    gold_present = sum(
        1 for score in doc_scores for r in score.scalars if r.gold_status == "present"
    )
    gold_absent = sum(
        1 for score in doc_scores for r in score.scalars if r.gold_status == "absent"
    )
    correct = scalar_counts.get(OUTCOME_CORRECT, 0)
    correct_absent = scalar_counts.get(OUTCOME_CORRECT_ABSENT, 0)
    matched_fields = sum(
        len([o for _, o in r.field_outcomes])
        for score in doc_scores
        for r in score.rows
        if r.outcome in {ROW_EXACT, ROW_PARTIAL}
    )
    matched_ok = sum(
        1
        for score in doc_scores
        for r in score.rows
        if r.outcome in {ROW_EXACT, ROW_PARTIAL}
        for _, o in r.field_outcomes
        if o in ROW_OK_OUTCOMES
    )
    invariant_counts = {"reported_match": 0, "reported_mismatch": 0, "not_reported": 0}
    contradiction_counts: dict[str, int] = {}
    evidence = {"emitted": 0, "with_evidence": 0}
    review = {"self_flagged": 0, "unverified_emitted": 0}
    danger: dict[str, int] = {}
    for score in doc_scores:
        invariant_counts[score.invariant["outcome"]] += 1
        contradiction_counts[score.invariant["contradiction_class"]] = (
            contradiction_counts.get(score.invariant["contradiction_class"], 0) + 1
        )
        evidence["emitted"] += score.evidence["emitted"]
        evidence["with_evidence"] += score.evidence["with_evidence"]
        review["self_flagged"] += score.review["self_flagged"]
        review["unverified_emitted"] += score.review["unverified_emitted"]
        cross_row = sum(len(r.cross_row_fields) for r in score.rows)
        danger["cross_row_contamination_fields"] = (
            danger.get("cross_row_contamination_fields", 0) + cross_row
        )
        for key, value in score.counts.items():
            if key in _DANGER_CLASSES:
                danger[key] = danger.get(key, 0) + value
    for klass in contradiction_counts:
        if klass in {"silent_contradiction", "false_satisfied"}:
            danger[f"invariant.{klass}"] = contradiction_counts[klass]

    slices: dict[str, dict[str, Any]] = {}
    for score in doc_scores:
        for tag in slices_by_doc.get(score.doc_id, ()):  # slices declared per doc
            bucket = slices.setdefault(tag, {"docs": 0, "doc_exact": 0})
            bucket["docs"] += 1
            if score.doc_exact:
                bucket["doc_exact"] += 1
    total_docs = len(doc_scores)
    error_docs = sum(1 for score in doc_scores if score.error is not None)
    return {
        "docs": {
            "total": total_docs,
            "error_docs": error_docs,
            "doc_exact": sum(1 for score in doc_scores if score.doc_exact),
        },
        "scalar": {
            "counts": dict(sorted(scalar_counts.items())),
            "gold_present": gold_present,
            "gold_absent": gold_absent,
            "accuracy_on_present": round(correct / gold_present, 4) if gold_present else None,
            "absent_rejection_rate": round(correct_absent / gold_absent, 4) if gold_absent else None,
        },
        "rows": {
            "counts": dict(sorted(row_counts.items())),
            "field_counts": dict(sorted(row_field_counts.items())),
            "field_accuracy_inside_matched_rows": (
                round(matched_ok / matched_fields, 4) if matched_fields else None
            ),
        },
        "invariant": {
            "counts": invariant_counts,
            "contradiction_classes": dict(sorted(contradiction_counts.items())),
        },
        "danger": dict(sorted(danger.items())),
        "evidence": {
            **evidence,
            "coverage": round(evidence["with_evidence"] / evidence["emitted"], 4)
            if evidence["emitted"]
            else None,
        },
        "review": dict(review),
        "slices": {
            tag: {**bucket, "doc_exact_rate": round(bucket["doc_exact"] / bucket["docs"], 4)}
            for tag, bucket in sorted(slices.items())
        },
    }
