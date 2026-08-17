"""PR75 verification-risk conformance vectors.

Vectors are recomputed through the real evaluator and kernel constructors.
Semantic identities exclude event ids and benchmark runtime; any drift is a
fixture/code contract change and must be deliberate.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.eval.verification_risk import (
    BASELINE_NAMES,
    evaluate_baselines,
    evaluate_calibration,
    evaluate_pair,
    evaluate_verification_risk,
    load_verification_risk_corpus,
)
from app.kernel.errors import KernelError
from app.kernel.verification_risk import (
    DependencyDisclosureRecord,
    VerificationRiskEvidenceRecord,
)
from app.utils.canonical import (
    CANONICALIZATION_PROFILE,
    record_identity_hash,
    to_json_ready,
)


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "verification_risk_vectors_v1.json"
CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "verification_risk_corpus_v1.json"
CONSTRUCTORS = {
    "dependency_disclosure": DependencyDisclosureRecord,
    "verification_risk_evidence": VerificationRiskEvidenceRecord,
}


def load_vectors() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def identity_hash(record_class: str, payload: dict[str, Any]) -> str:
    record = CONSTRUCTORS[record_class].from_payload(payload, record_id="conformance-record")
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


def _assert_no_float(value: Any) -> None:
    if isinstance(value, float):
        raise AssertionError(f"float leaked into identity payload: {value!r}")
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_float(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_float(item)


def _rate_payload(rate: Any) -> dict[str, Any]:
    return {
        "count": rate.count,
        "denominator": rate.denominator,
        "rate": rate.rate,
        "status": rate.status,
    }


def test_vector_header_and_corpus_identity_are_stable():
    vectors = load_vectors()
    assert vectors["$schema"] == "marker.verification_risk_vectors.v1"
    assert vectors["canonicalization_profile"] == CANONICALIZATION_PROFILE
    corpus = load_verification_risk_corpus(CORPUS_PATH)
    assert vectors["corpus"]["semantic_identity"] == corpus.semantic_identity
    assert vectors["identity_cases"]
    assert vectors["evaluation_cases"]


def test_kernel_identity_vectors_are_drift_free_and_float_free():
    for case in load_vectors()["identity_cases"]:
        base = CONSTRUCTORS[case["record_class"]].from_payload(
            case["payload"], record_id="base-event"
        )
        _assert_no_float(base.identity_payload())
        expected = case["expect"]["identity_hash"]
        assert identity_hash(case["record_class"], case["payload"]) == expected
        # record_id is an event id, never semantic identity.
        same_event = CONSTRUCTORS[case["record_class"]].from_payload(
            case["payload"], record_id="different-event"
        )
        assert identity_hash(case["record_class"], case["payload"]) == record_identity_hash(
            record_type=same_event.record_type,
            schema_version=same_event.schema_version,
            payload=to_json_ready(same_event.identity_payload()),
        )
        for variant in case["variants"]:
            actual = identity_hash(case["record_class"], variant["payload"])
            if case["variant_expectation"] == "same":
                assert actual == expected, variant["id"]
            else:
                assert actual != expected, variant["id"]


def test_kernel_rematerialization_and_fail_closed_unknown_fields():
    vectors = load_vectors()
    for case in vectors["rematerialization_cases"]:
        constructor = CONSTRUCTORS[case["record_class"]]
        original = constructor.from_payload(case["payload"], record_id="first-event")
        rematerialized = constructor.from_payload(
            original.identity_payload(), record_id="rematerialized-event"
        )
        assert identity_hash(case["record_class"], case["payload"]) == case["expect"]["identity_hash"]
        assert record_identity_hash(
            record_type=rematerialized.record_type,
            schema_version=rematerialized.schema_version,
            payload=to_json_ready(rematerialized.identity_payload()),
        ) == case["expect"]["identity_hash"]
        unknown = dict(case["payload"])
        unknown["unrecognised_identity_field"] = "must-not-be-ignored"
        with pytest.raises(KernelError, match="unknown"):
            constructor.from_payload(unknown, record_id="bad-event")


@pytest.mark.parametrize(
    "slice_id", ["calibration-fit", "matched", "shifted", "insufficient"]
)
def test_evaluation_vectors_cover_pair_calibration_and_all_five_baselines(slice_id: str):
    vectors = load_vectors()
    case = next(item for item in vectors["evaluation_cases"] if item["slice_id"] == slice_id)
    corpus = load_verification_risk_corpus(CORPUS_PATH)
    pair = evaluate_pair(corpus, "model-a", "model-b", slice_id=slice_id)
    expected_pair = case["pair"]
    assert [pair.witness_a, pair.witness_b] == expected_pair["witnesses"]
    assert pair.sample_count == expected_pair["sample_count"]
    assert {key: _rate_payload(value) for key, value in sorted(pair.marginal_error.items())} == expected_pair["marginal_error"]
    for name in (
        "joint_error",
        "agreement",
        "disagreement",
        "conditional_error_when_agree",
        "conditional_error_when_disagree",
        "catastrophic_joint_failures",
    ):
        assert _rate_payload(getattr(pair, name)) == expected_pair[name]

    calibration = evaluate_calibration(
        corpus,
        "model-a",
        slice_id=slice_id,
        min_samples=5,
    )
    expected_calibration = case["calibration"]
    assert calibration.witness_id == expected_calibration["witness_id"]
    assert calibration.distribution == expected_calibration["distribution"]
    assert calibration.method_id == expected_calibration["method_id"]
    assert calibration.method_version == expected_calibration["method_version"]
    assert calibration.sample_count == expected_calibration["sample_count"]
    assert calibration.support_required == expected_calibration["support_required"]
    assert calibration.support_sufficient == expected_calibration["support_sufficient"]
    assert calibration.status == expected_calibration["status"]
    assert _rate_payload(calibration.accuracy) == expected_calibration["accuracy"]
    assert calibration.brier_score == expected_calibration["brier_score"]
    assert calibration.expected_calibration_error == expected_calibration["expected_calibration_error"]

    comparison = evaluate_baselines(corpus, slice_id=slice_id)
    assert tuple(comparison.baselines) == tuple(case["baseline_order"]) == BASELINE_NAMES
    for name in BASELINE_NAMES:
        actual = comparison.baselines[name]
        expected = case["baselines"][name]
        assert actual.sample_count == expected["sample_count"]
        assert actual.accepted_count == expected["accepted_count"]
        assert actual.false_verified_count == expected["false_verified_count"]
        assert actual.catastrophic_error_count == expected["catastrophic_error_count"]
        assert actual.disagreement_count == expected["disagreement_count"]
        assert actual.status == expected["status"]
        assert list(actual.selected_witnesses) == expected["selected_witnesses"]
        assert actual.semantic_identity == expected["semantic_identity"]


def test_report_and_runtime_identity_are_separate():
    vectors = load_vectors()
    corpus = load_verification_risk_corpus(CORPUS_PATH)
    report = evaluate_verification_risk(
        corpus, slice_id="matched", calibration_witness_ids=("model-a",), min_calibration_samples=5
    )
    expected = next(item for item in vectors["evaluation_cases"] if item["slice_id"] == "matched")
    assert report.semantic_identity == expected["semantic_identity"]
    changed = replace(report, runtime_ms=999.0)
    assert changed.semantic_identity == report.semantic_identity
    baseline = report.baselines.baselines[BASELINE_NAMES[1]]
    assert replace(baseline, runtime_ms=999.0).semantic_identity == baseline.semantic_identity


def test_promotion_threshold_is_explicit_and_conservative():
    vectors = load_vectors()
    decisions = {
        case["slice_id"]: case["promotion"] for case in vectors["evaluation_cases"]
    }
    assert decisions["matched"]["threshold"]["min_support"] == 5
    assert decisions["matched"]["threshold"]["max_false_verified_count"] == 0
    # Current hand-checkable corpus contains false verification under the
    # dependency-aware report, so it remains shadow-only rather than promoted.
    assert decisions["matched"]["decision"] == "shadow"
    assert decisions["calibration-fit"]["decision"] == "abstain"
    assert decisions["shifted"]["decision"] == "abstain"
    assert decisions["insufficient"]["decision"] == "abstain"
