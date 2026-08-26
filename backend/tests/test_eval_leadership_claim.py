"""Tests for leadership claim contracts and 9-dimension fail-closed validator (Invariant 60)."""

from __future__ import annotations

import pytest

from app.eval.accountability.leadership_claim import (
    CANONICAL_UNIVERSAL_DISCLAIMER,
    CLAIM_BEATS,
    CLAIM_ROUTES_TO,
    CLAIM_TIES_REDUCING_BURDEN,
    EVIDENCE_CURRENT,
    EVIDENCE_STALE,
    EVIDENCE_SUPERSEDED,
    LEADERSHIP_CLAIM_SCHEMA_VERSION,
    CatastrophicBudget,
    ClaimEvidenceBinding,
    LeadershipClaim,
    ReviewBurden,
    validate_leadership_claim,
)

SAMPLE_SHA = "b" * 64


def _valid_claim() -> LeadershipClaim:
    return LeadershipClaim(
        claim_id="pr80b.invoice_extraction_lineage",
        workflow="invoice_extraction_demo_v1",
        source_profile="synthetic_plain_text_invoices_v1",
        policy_profile="strict_financial_evidence_verification",
        hardware_profile="cpu_local_eval_profile",
        competitors=("invoice2data_1.0.1", "llm_openai_poolside_laguna_s_2.1_free"),
        evidence_date="2026-08-20T00:00:00Z",
        catastrophic_budget=CatastrophicBudget(
            max_acceptable_rate=0.01,
            observed_rate=0.0,
            bound_method="one_sided_95_clopper_pearson_upper_bound",
            upper_bound_95=0.0065,  # positive upper bound despite 0 observed, <= max_acceptable_rate
            trials=454,
            zero_is_not_zero_risk_acknowledged=True,
        ),
        review_burden=ReviewBurden(
            status="measured",
            self_flagged_count=61,
            unverified_emitted_count=0,
            queue_time_ms_p50=12.5,
        ),
        unresolved_limits=(
            "Exact token normalization for comma-decimal EU formats requires explicit locale flag",
            "Scope limited to plain-text single-page invoices",
        ),
        disposition=CLAIM_BEATS,
        evidence_bindings=(
            ClaimEvidenceBinding(
                artifact_path="docs/reference/measurements/pr80b-direct-specialist-displacement.json",
                artifact_sha256=SAMPLE_SHA,
                lifecycle=EVIDENCE_CURRENT,
                workflow_scope="invoice_extraction_demo_v1",
                corpus_scope="pr80b_frozen_corpus_24_docs",
                comparator_scope=("invoice2data_1.0.1", "llm_openai_poolside_laguna_s_2.1_free"),
                metric_pointers={"metrics.marker-pr80a.evidence.coverage": 1.0},
            ),
        ),
        corpus_scope="pr80b_frozen_corpus_24_docs",
    )


def test_valid_leadership_claim_passes():
    claim = _valid_claim()
    errors = validate_leadership_claim(claim, as_of_date="2026-08-26T00:00:00Z")
    assert errors == []


def test_mapping_input_requires_exact_schema_version():
    claim_dict = _valid_claim().to_dict()
    del claim_dict["schema_version"]
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("schema_version must be" in err for err in errors)

    claim_dict2 = _valid_claim().to_dict()
    claim_dict2["schema_version"] = "wrong.claim.v99"
    errors2 = validate_capability_record_or_claim(claim_dict2)
    assert any(f"schema_version must be {LEADERSHIP_CLAIM_SCHEMA_VERSION!r}" in err for err in errors2)


@pytest.mark.parametrize(
    "field_name",
    [
        "workflow",
        "source_profile",
        "policy_profile",
        "hardware_profile",
        "competitors",
        "evidence_date",
        "catastrophic_budget",
        "review_burden",
        "unresolved_limits",
    ],
)
def test_each_mandatory_dimension_removal_fails(field_name: str):
    claim_dict = _valid_claim().to_dict()
    del claim_dict[field_name]
    errors = validate_leadership_claim(claim_dict, as_of_date="2026-08-26T00:00:00Z")
    assert len(errors) >= 1
    assert any(field_name in err for err in errors)


def test_probability_rates_must_be_in_range():
    for rate_field in ("max_acceptable_rate", "observed_rate", "upper_bound_95"):
        claim_dict = _valid_claim().to_dict()
        claim_dict["catastrophic_budget"][rate_field] = 1.5
        errors = validate_capability_record_or_claim(claim_dict)
        assert any("must be in [0.0, 1.0]" in err for err in errors)

        claim_dict2 = _valid_claim().to_dict()
        claim_dict2["catastrophic_budget"][rate_field] = -0.1
        errors2 = validate_capability_record_or_claim(claim_dict2)
        assert any("must be in [0.0, 1.0]" in err for err in errors2)


def validate_capability_record_or_claim(d):
    return validate_leadership_claim(d, as_of_date="2026-08-26T00:00:00Z")


def test_observed_rate_cannot_exceed_upper_bound():
    claim_dict = _valid_claim().to_dict()
    claim_dict["catastrophic_budget"]["observed_rate"] = 0.05
    claim_dict["catastrophic_budget"]["upper_bound_95"] = 0.04
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("observed_rate must be <= upper_bound_95" in err for err in errors)


def test_upper_bound_cannot_exceed_max_acceptable_rate_for_beats_or_ties():
    claim_dict = _valid_claim().to_dict()
    claim_dict["catastrophic_budget"]["max_acceptable_rate"] = 0.005
    claim_dict["catastrophic_budget"]["upper_bound_95"] = 0.0065
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("upper_bound_95 exceeds max_acceptable_rate" in err for err in errors)


def test_reject_unknown_fields_in_leadership_claim():
    claim_dict = _valid_claim().to_dict()
    claim_dict["extra_marketing_spin"] = "we are the fastest"
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("unknown field 'extra_marketing_spin'" in err for err in errors)


def test_beats_ties_routes_require_nonempty_corpus_scope_and_binding_scopes():
    for disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN, CLAIM_ROUTES_TO):
        # 1. Missing claim corpus_scope
        claim_dict = _valid_claim().to_dict()
        claim_dict["disposition"] = disp
        claim_dict["corpus_scope"] = ""
        errors = validate_capability_record_or_claim(claim_dict)
        assert any(f"claim disposition {disp!r} requires non-empty corpus_scope" in err for err in errors)

        # 2. Empty binding workflow_scope
        claim_dict2 = _valid_claim().to_dict()
        claim_dict2["disposition"] = disp
        claim_dict2["evidence_bindings"][0]["workflow_scope"] = ""
        errors2 = validate_capability_record_or_claim(claim_dict2)
        assert any("evidence_binding #0 workflow_scope must be non-empty string" in err for err in errors2)

        # 3. Empty binding corpus_scope
        claim_dict3 = _valid_claim().to_dict()
        claim_dict3["disposition"] = disp
        claim_dict3["evidence_bindings"][0]["corpus_scope"] = ""
        errors3 = validate_capability_record_or_claim(claim_dict3)
        assert any("evidence_binding #0 corpus_scope must be non-empty string" in err for err in errors3)

        # 4. Empty binding comparator_scope
        claim_dict4 = _valid_claim().to_dict()
        claim_dict4["disposition"] = disp
        claim_dict4["evidence_bindings"][0]["comparator_scope"] = []
        errors4 = validate_capability_record_or_claim(claim_dict4)
        assert any("evidence_binding #0 comparator_scope must be a non-empty list" in err for err in errors4)


def test_evidence_scope_mismatches_and_exact_comparator_equality_enforced():
    # 1. Workflow mismatch
    claim_dict = _valid_claim().to_dict()
    claim_dict["evidence_bindings"][0]["workflow_scope"] = "different_workflow"
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("workflow_scope 'different_workflow' does not match claim workflow" in err for err in errors)

    # 2. Corpus scope mismatch
    claim_dict2 = _valid_claim().to_dict()
    claim_dict2["evidence_bindings"][0]["corpus_scope"] = "unrelated_corpus"
    errors2 = validate_capability_record_or_claim(claim_dict2)
    assert any("corpus_scope 'unrelated_corpus' does not match claim corpus_scope" in err for err in errors2)

    # 3. Comparator subset (not exact equality) -> Must fail
    claim_dict3 = _valid_claim().to_dict()
    claim_dict3["evidence_bindings"][0]["comparator_scope"] = ["invoice2data_1.0.1"]  # omitted LLM competitor
    errors3 = validate_capability_record_or_claim(claim_dict3)
    assert any("comparator_scope must match claim competitors exactly" in err for err in errors3)


def test_unresolved_limits_typed_and_nonempty():
    claim_dict = _valid_claim().to_dict()
    claim_dict["unresolved_limits"] = []
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("unresolved_limits is mandatory and must be a non-empty list" in err for err in errors)

    claim_dict2 = _valid_claim().to_dict()
    claim_dict2["unresolved_limits"] = [""]
    errors2 = validate_capability_record_or_claim(claim_dict2)
    assert any("unresolved limit entry must be a non-empty string" in err for err in errors2)


def test_stale_or_superseded_evidence_cannot_support_routes_to():
    for lc in (EVIDENCE_STALE, EVIDENCE_SUPERSEDED):
        claim_dict = _valid_claim().to_dict()
        claim_dict["disposition"] = CLAIM_ROUTES_TO
        claim_dict["evidence_bindings"][0]["lifecycle"] = lc
        errors = validate_capability_record_or_claim(claim_dict)
        assert any(f"cannot rest on {lc} evidence" in err for err in errors)


def test_reject_negative_review_burden_counts_or_queue_times():
    for neg_field in ("self_flagged_count", "unverified_emitted_count", "queue_time_ms_p50"):
        claim_dict = _valid_claim().to_dict()
        claim_dict["review_burden"][neg_field] = -1
        errors = validate_capability_record_or_claim(claim_dict)
        assert any(f"requires non-negative {neg_field}" in err for err in errors)


def test_zero_observed_catastrophes_cannot_have_zero_upper_bound():
    claim_dict = _valid_claim().to_dict()
    claim_dict["catastrophic_budget"]["upper_bound_95"] = 0.0
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("zero observed is not zero risk" in err for err in errors)


def test_zero_observed_must_acknowledge_statistical_risk():
    claim_dict = _valid_claim().to_dict()
    claim_dict["catastrophic_budget"]["zero_is_not_zero_risk_acknowledged"] = False
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("zero_is_not_zero_risk_acknowledged must be True" in err for err in errors)


def test_stale_or_superseded_evidence_cannot_support_superiority_claim():
    for lc in (EVIDENCE_STALE, EVIDENCE_SUPERSEDED):
        for disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN):
            claim_dict = _valid_claim().to_dict()
            claim_dict["disposition"] = disp
            claim_dict["evidence_bindings"][0]["lifecycle"] = lc
            errors = validate_capability_record_or_claim(claim_dict)
            assert any(f"cannot rest on {lc} evidence" in err for err in errors)


def test_review_burden_unavailable_requires_explicit_reason():
    claim_dict = _valid_claim().to_dict()
    claim_dict["review_burden"] = {"status": "unavailable"}
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("requires non-empty reason explaining absence" in err for err in errors)


def test_future_evidence_date_fails():
    claim_dict = _valid_claim().to_dict()
    claim_dict["evidence_date"] = "2029-01-01T00:00:00Z"
    errors = validate_leadership_claim(claim_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("is in the future" in err for err in errors)


def test_anti_universalization_disclaimer_enforced():
    # 1. Missing disclaimer
    claim_dict = _valid_claim().to_dict()
    del claim_dict["universal_disclaimer"]
    errors = validate_capability_record_or_claim(claim_dict)
    assert any("universal_disclaimer is mandatory" in err for err in errors)

    # 2. Universal / global / best-overall overstatement in disclaimer
    for bad_word in ("global superiority", "best overall extraction engine", "universal standard"):
        claim_dict2 = _valid_claim().to_dict()
        claim_dict2["universal_disclaimer"] = f"Marker is {bad_word} across all benchmarks"
        errors2 = validate_capability_record_or_claim(claim_dict2)
        assert any("universal_disclaimer cannot make un-scoped universal/global superiority assertions" in err for err in errors2)

    # 3. Exact canonical disclaimer passes
    claim_dict3 = _valid_claim().to_dict()
    claim_dict3["universal_disclaimer"] = CANONICAL_UNIVERSAL_DISCLAIMER
    errors3 = validate_capability_record_or_claim(claim_dict3)
    assert errors3 == []
