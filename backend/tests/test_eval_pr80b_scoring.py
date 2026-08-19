"""Scoring and failure-taxonomy tests for the PR80B benchmark (matrix S)."""

from __future__ import annotations

import pytest

from app.eval.pr80b.scoring import (
    EmittedField,
    EmittedRow,
    SystemDocOutput,
    aggregate_metrics,
    score_document,
)


def _gold(**overrides):
    base = {
        "doc_id": "doc-1",
        "slices": ["baseline.happy"],
        "fields": {
            "invoice_number": {"status": "present", "value": "INV-1"},
            "invoice_date": {"status": "present", "value": "2026-01-01"},
            "currency": {"status": "present", "value": "USD"},
            "po_number": {"status": "absent"},
            "total_due": {"status": "present", "value": "30.00"},
        },
        "items": {
            "status": "rows",
            "rows": [
                {
                    "sku": "SKU-A",
                    "fields": {
                        "description": {"status": "present", "value": "Thing"},
                        "quantity": {"status": "present", "value": 2},
                        "unit_price": {"status": "present", "value": "10.00"},
                        "amount": {"status": "present", "value": "20.00"},
                    },
                },
                {
                    "sku": "SKU-B",
                    "fields": {
                        "description": {"status": "present", "value": "Other"},
                        "quantity": {"status": "present", "value": 1},
                        "unit_price": {"status": "present", "value": "10.00"},
                        "amount": {"status": "present", "value": "10.00"},
                    },
                },
            ],
        },
        "invariant": {"expected": "satisfied"},
        "notes": "",
    }
    base.update(overrides)
    return base


def _perfect_output(invariant_findings=None, **field_overrides):
    fields = {
        "invoice_number": EmittedField(status="emitted", value="INV-1"),
        "invoice_date": EmittedField(status="emitted", value="2026-01-01"),
        "currency": EmittedField(status="emitted", value="USD"),
        "po_number": EmittedField(status="absent"),
        "total_due": EmittedField(status="emitted", value="30.00"),
    }
    fields.update(field_overrides)
    rows = (
        EmittedRow(
            sku="SKU-A",
            fields={
                "description": EmittedField(status="emitted", value="Thing"),
                "quantity": EmittedField(status="emitted", value=2),
                "unit_price": EmittedField(status="emitted", value="10.00"),
                "amount": EmittedField(status="emitted", value="20.00"),
            },
        ),
        EmittedRow(
            sku="SKU-B",
            fields={
                "description": EmittedField(status="emitted", value="Other"),
                "quantity": EmittedField(status="emitted", value=1),
                "unit_price": EmittedField(status="emitted", value="10.00"),
                "amount": EmittedField(status="emitted", value="10.00"),
            },
        ),
    )
    return SystemDocOutput(
        system_id="test",
        doc_id="doc-1",
        fields=fields,
        rows=rows,
        invariant_findings=(
            {"total_due": "satisfied"} if invariant_findings is None else invariant_findings
        ),
    )


def _score(output, gold=None):
    return score_document(gold or _gold(), output)


def _scalar(score, name):
    return next(r for r in score.scalars if r.field == name)


class TestScalarTaxonomy:
    def test_correct(self):
        score = _score(_perfect_output())
        assert _scalar(score, "invoice_number").outcome == "correct"

    def test_correct_absent(self):
        score = _score(_perfect_output())
        assert _scalar(score, "po_number").outcome == "correct_absent"

    def test_normalization_equivalence_scores_correct(self):
        output = _perfect_output(
            invoice_date=EmittedField(status="emitted", value="01/01/2026"),
            total_due=EmittedField(status="emitted", value="30.0"),
        )
        score = _score(output)
        assert _scalar(score, "invoice_date").outcome == "correct"
        assert _scalar(score, "total_due").outcome == "correct"

    def test_wrong_value(self):
        output = _perfect_output(
            invoice_number=EmittedField(status="emitted", value="INV-2")
        )
        assert _scalar(_score(output), "invoice_number").outcome == "wrong_value"

    def test_invalid_normalization(self):
        output = _perfect_output(
            invoice_date=EmittedField(status="emitted", value="yesterday")
        )
        assert _scalar(_score(output), "invoice_date").outcome == "invalid"

    def test_missing(self):
        output = _perfect_output(invoice_number=EmittedField(status="absent"))
        assert _scalar(_score(output), "invoice_number").outcome == "missing"

    def test_missing_flagged_is_honest(self):
        output = _perfect_output(
            invoice_number=EmittedField(status="absent", self_flagged=True)
        )
        assert _scalar(_score(output), "invoice_number").outcome == "missing_flagged"

    def test_fabricated(self):
        output = _perfect_output(
            po_number=EmittedField(status="emitted", value="PO-999")
        )
        assert _scalar(_score(output), "po_number").outcome == "fabricated"

    def test_flagged_conflict_without_value_counts_absent_ok(self):
        output = _perfect_output(
            po_number=EmittedField(status="flagged_conflict", value=None)
        )
        assert _scalar(_score(output), "po_number").outcome == "correct_absent"

    def test_flagged_conflict_with_value_is_fabricated(self):
        output = _perfect_output(
            po_number=EmittedField(status="flagged_conflict", value="PO-999")
        )
        assert _scalar(_score(output), "po_number").outcome == "fabricated"

    def test_false_conflict_on_single_value(self):
        output = _perfect_output(
            invoice_number=EmittedField(status="flagged_conflict", value=None)
        )
        assert _scalar(_score(output), "invoice_number").outcome == "false_conflict"

    def test_zero_is_not_absent(self):
        gold = _gold()
        gold["items"]["rows"][0]["fields"]["quantity"] = {
            "status": "present",
            "value": 0,
        }
        output = _perfect_output()
        output.rows[0].fields["quantity"] = EmittedField(status="absent")
        score = _score(output, gold)
        row = next(r for r in score.rows if r.sku == "SKU-A")
        assert dict(row.field_outcomes)["quantity"] == "missing"

    def test_absent_gold_zero_emitted_is_fabricated(self):
        output = _perfect_output(po_number=EmittedField(status="emitted", value="0"))
        assert _scalar(_score(output), "po_number").outcome == "fabricated"


class TestConflictHandling:
    def _conflict_gold(self):
        gold = _gold()
        gold["fields"]["total_due"] = {
            "status": "conflicting",
            "values": ["30.00", "31.00"],
        }
        gold["invariant"]["expected"] = "not_evaluable"
        return gold

    def test_honest_conflict(self):
        output = _perfect_output(
            total_due=EmittedField(status="flagged_conflict", value=None)
        )
        assert _scalar(_score(output, self._conflict_gold()), "total_due").outcome == "honest_conflict"

    def test_conflict_value_flagged(self):
        output = _perfect_output(
            total_due=EmittedField(status="emitted", value="30.00", self_flagged=True)
        )
        assert _scalar(_score(output, self._conflict_gold()), "total_due").outcome == "conflict_value_flagged"

    def test_confident_on_conflict(self):
        output = _perfect_output(total_due=EmittedField(status="emitted", value="30.00"))
        assert _scalar(_score(output, self._conflict_gold()), "total_due").outcome == "confident_on_conflict"

    def test_confident_on_conflict_with_outside_value_notes_detail(self):
        output = _perfect_output(total_due=EmittedField(status="emitted", value="55.00"))
        result = _scalar(_score(output, self._conflict_gold()), "total_due")
        assert result.outcome == "confident_on_conflict"
        assert "not even one of the stated candidates" in result.detail

    def test_silent_missing_conflict(self):
        output = _perfect_output(total_due=EmittedField(status="absent"))
        result = _scalar(_score(output, self._conflict_gold()), "total_due").outcome
        assert result == "silent_missing_conflict"


class TestRowTaxonomy:
    def test_exact_row_and_doc_exact(self):
        score = _score(_perfect_output())
        assert all(r.outcome == "exact" for r in score.rows)
        assert score.doc_exact

    def test_row_matching_ignores_order(self):
        output = _perfect_output()
        object.__setattr__(output, "rows", (output.rows[1], output.rows[0]))
        score = _score(output)
        assert all(r.outcome == "exact" for r in score.rows)
        assert score.doc_exact

    def test_partial_row_single_wrong_field(self):
        output = _perfect_output()
        output.rows[0].fields["quantity"] = EmittedField(status="emitted", value=5)
        score = _score(output)
        row = next(r for r in score.rows if r.sku == "SKU-A")
        assert row.outcome == "partial"
        assert dict(row.field_outcomes)["quantity"] == "wrong_value"
        assert not score.doc_exact

    def test_missed_row(self):
        output = _perfect_output()
        object.__setattr__(output, "rows", (output.rows[1],))
        score = _score(output)
        outcomes = {r.sku: r.outcome for r in score.rows}
        assert outcomes["SKU-A"] == "missed"
        assert outcomes["SKU-B"] == "exact"

    def test_hallucinated_row(self):
        output = _perfect_output()
        ghost = EmittedRow(
            sku="SKU-GHOST",
            fields={
                "description": EmittedField(status="emitted", value="Nothing"),
                "quantity": EmittedField(status="emitted", value=1),
                "unit_price": EmittedField(status="emitted", value="1.00"),
                "amount": EmittedField(status="emitted", value="1.00"),
            },
        )
        object.__setattr__(output, "rows", output.rows + (ghost,))
        score = _score(output)
        assert "SKU-GHOST" in score.extra_rows
        assert score.counts.get("row.hallucinated") == 1
        assert not score.doc_exact

    def test_row_without_sku_is_extra(self):
        output = _perfect_output()
        ghost = EmittedRow(sku=None, fields={})
        object.__setattr__(output, "rows", output.rows + (ghost,))
        score = _score(output)
        assert "<no-sku>" in score.extra_rows

    def test_duplicate_row_emission(self):
        output = _perfect_output()
        object.__setattr__(output, "rows", output.rows + (output.rows[0],))
        score = _score(output)
        assert score.counts.get("row.duplicate_emitted") == 1
        assert not score.doc_exact

    def test_cross_row_contamination_detected(self):
        output = _perfect_output()
        # SKU-A quantity 2 moved onto SKU-B's row (both rows otherwise fine).
        output.rows[1].fields["quantity"] = EmittedField(status="emitted", value=2)
        score = _score(output)
        row_b = next(r for r in score.rows if r.sku == "SKU-B")
        assert row_b.cross_row_fields == ("quantity",)

    def test_conflict_row_honest(self):
        gold = _gold()
        gold["items"]["rows"][0]["expected_conflict"] = True
        gold["items"]["rows"][0]["fields"]["amount"] = {
            "status": "conflicting",
            "values": ["20.00", "25.00"],
        }
        output = _perfect_output()
        output.rows[0].fields["amount"] = EmittedField(
            status="flagged_conflict", value=None
        )
        score = _score(output, gold)
        row = next(r for r in score.rows if r.sku == "SKU-A")
        assert row.outcome == "conflict_honest"

    def test_conflict_row_confident(self):
        gold = _gold()
        gold["items"]["rows"][0]["expected_conflict"] = True
        gold["items"]["rows"][0]["fields"]["amount"] = {
            "status": "conflicting",
            "values": ["20.00", "25.00"],
        }
        output = _perfect_output()
        output.rows[0].fields["amount"] = EmittedField(status="emitted", value="20.00")
        score = _score(output, gold)
        row = next(r for r in score.rows if r.sku == "SKU-A")
        assert row.outcome == "conflict_confident"


class TestInvariantScoring:
    def test_reported_match(self):
        score = _score(_perfect_output())
        assert score.invariant["outcome"] == "reported_match"
        assert score.invariant["contradiction_class"] == "none"

    def test_reported_mismatch(self):
        output = _perfect_output(
            invariant_findings={"total_due": "violated"}
        )
        assert _score(output).invariant["outcome"] == "reported_mismatch"

    def test_not_reported_when_lane_has_no_findings(self):
        output = _perfect_output()
        object.__setattr__(output, "invariant_findings", None)
        assert _score(output).invariant["outcome"] == "not_reported"

    def test_silent_contradiction(self):
        output = _perfect_output(total_due=EmittedField(status="emitted", value="99.00"))
        object.__setattr__(output, "invariant_findings", None)
        invariant = _score(output).invariant
        assert invariant["scorer_computed"] == "mismatch"
        assert invariant["contradiction_class"] == "silent_contradiction"

    def test_false_satisfied(self):
        output = _perfect_output(total_due=EmittedField(status="emitted", value="99.00"))
        invariant = _score(output).invariant
        assert invariant["route_reported"] == "satisfied"
        assert invariant["contradiction_class"] == "false_satisfied"

    def test_flagged_mismatch_is_not_silent(self):
        output = _perfect_output(total_due=EmittedField(status="emitted", value="99.00"))
        object.__setattr__(
            output, "invariant_findings", {"total_due": "violated"}
        )
        invariant = _score(output).invariant
        assert invariant["contradiction_class"] == "flagged"

    def test_unevaluable_when_row_amount_missing(self):
        output = _perfect_output()
        output.rows[0].fields["amount"] = EmittedField(status="absent")
        invariant = _score(output).invariant
        assert invariant["scorer_computed"] == "unevaluable"


class TestRobustness:
    def test_unknown_system_fields_ignored(self):
        output = _perfect_output()
        fields = dict(output.fields)
        fields["mystery_field"] = EmittedField(status="emitted", value="x")
        object.__setattr__(
            output,
            "fields",
            fields,
        )
        score = _score(output)
        assert len(score.scalars) == 5
        assert all(r.field != "mystery_field" for r in score.scalars)

    def test_lane_error_blocks_doc_exact(self):
        output = _perfect_output()
        object.__setattr__(output, "error", "provider unavailable")
        score = _score(output)
        assert score.error == "provider unavailable"
        assert not score.doc_exact

    def test_scoring_is_deterministic(self):
        output = _perfect_output(po_number=EmittedField(status="emitted", value="X"))
        first = _score(output).to_dict()
        second = _score(output).to_dict()
        assert first == second

    def test_aggregate_metrics_counts_and_rates(self):
        good = _score(_perfect_output())
        bad_output = _perfect_output(
            invoice_number=EmittedField(status="emitted", value="WRONG"),
            po_number=EmittedField(status="emitted", value="PO-X"),
        )
        bad = _score(bad_output)
        metrics = aggregate_metrics(
            [good, bad], {"doc-1": ["baseline.happy"]}
        )
        assert metrics["docs"]["total"] == 2
        assert metrics["docs"]["doc_exact"] == 1
        assert metrics["scalar"]["counts"]["correct"] == 7
        assert metrics["scalar"]["counts"]["wrong_value"] == 1
        assert metrics["scalar"]["counts"]["fabricated"] == 1
        assert metrics["scalar"]["gold_present"] == 8
        assert metrics["scalar"]["accuracy_on_present"] == pytest.approx(0.875)
        assert metrics["danger"]["fabricated"] == 1
        assert metrics["slices"]["baseline.happy"]["docs"] == 2

    def test_aggregate_error_docs_visible(self):
        output = _perfect_output()
        object.__setattr__(output, "error", "timeout")
        metrics = aggregate_metrics([_score(output)], {"doc-1": []})
        assert metrics["docs"]["error_docs"] == 1

    def test_evidence_coverage_counts_every_emitted_field(self):
        output = _perfect_output()
        score = _score(output)
        emitted_scalars = sum(
            1 for f in output.fields.values() if f.status != "absent" and f.value is not None
        )
        emitted_row_fields = sum(
            1 for r in output.rows for f in r.fields.values() if f.value is not None
        )
        assert score.evidence["emitted"] == emitted_scalars + emitted_row_fields
        assert score.evidence["with_evidence"] == 0
        assert score.review["self_flagged"] == 0
        assert score.review["unverified_emitted"] == score.evidence["emitted"]
