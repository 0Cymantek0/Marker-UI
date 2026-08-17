"""Focused deterministic PR75 verification-risk evaluator tests."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.eval.verification_risk import (
    BASELINE_NAMES,
    VerificationRiskError,
    evaluate_baselines,
    evaluate_calibration,
    evaluate_pair,
    evaluate_verification_risk,
    load_verification_risk_corpus,
    semantic_artifact_identity,
)


FIXTURE = Path(__file__).parents[1] / "conformance" / "fixtures" / "verification_risk_corpus_v1.json"


@pytest.fixture(scope="module")
def corpus():
    return load_verification_risk_corpus(FIXTURE)


def test_fixture_loads_required_cases_and_identity_ignores_runtime(corpus):
    assert {sample.case for sample in corpus.samples} >= {
        "correlated",
        "shared-dependency",
        "unknown-lineage",
        "model-consensus",
        "matched",
        "shifted",
        "insufficient",
    }
    assert corpus.witness_by_id["unknown-model"].disclosure == "unknown"
    assert corpus.witness_by_id["model-a-int8"].base_lineage == "checkpoint-a"
    assert corpus.witness_by_id["model-a-int8"].quantization == "int8"
    assert corpus.witness_by_id["model-a-alias"].alias_of == "model-a"
    assert (
        corpus.witness_by_id["teacher-child-a"].teacher_lineage
        == corpus.witness_by_id["teacher-child-b"].teacher_lineage
    )
    assert corpus.witness_by_id["model-a"].prompt_identity != corpus.witness_by_id[
        "model-a-prompt-v2"
    ].prompt_identity
    first_identity = corpus.semantic_identity
    decoded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    decoded["metadata"]["runtime"]["elapsed_ms"] = 999999
    assert load_verification_risk_corpus(decoded).semantic_identity == first_identity


def test_duplicate_samples_and_witnesses_fail_closed(corpus):
    decoded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    duplicate_sample = copy.deepcopy(decoded)
    duplicate_sample["samples"].append(copy.deepcopy(duplicate_sample["samples"][0]))
    with pytest.raises(VerificationRiskError, match="duplicate sample id"):
        load_verification_risk_corpus(duplicate_sample)

    duplicate_witness = copy.deepcopy(decoded)
    duplicate_witness["witnesses"].append(copy.deepcopy(duplicate_witness["witnesses"][0]))
    with pytest.raises(VerificationRiskError, match="duplicate witness id"):
        load_verification_risk_corpus(duplicate_witness)


def test_unknown_profile_fields_and_non_boolean_flags_fail_closed():
    decoded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    unknown_witness = copy.deepcopy(decoded)
    unknown_witness["witnesses"][0]["mystery"] = "ignored-before"
    with pytest.raises(VerificationRiskError, match="unknown fields: mystery"):
        load_verification_risk_corpus(unknown_witness)

    unknown_dependency = copy.deepcopy(decoded)
    unknown_dependency["witnesses"][0]["dependency_profile"]["mystery"] = "ignored-before"
    with pytest.raises(VerificationRiskError, match="unknown fields: mystery"):
        load_verification_risk_corpus(unknown_dependency)

    bad_source_native = copy.deepcopy(decoded)
    bad_source_native["witnesses"][0]["source_native"] = "true"
    with pytest.raises(VerificationRiskError, match="source_native must be boolean"):
        load_verification_risk_corpus(bad_source_native)

    bad_sample_catastrophic = copy.deepcopy(decoded)
    bad_sample_catastrophic["samples"][0]["catastrophic"] = "false"
    with pytest.raises(VerificationRiskError, match="catastrophic must be boolean"):
        load_verification_risk_corpus(bad_sample_catastrophic)

    bad_outcome_catastrophic = copy.deepcopy(decoded)
    bad_outcome_catastrophic["samples"][0]["outcomes"]["model-a"]["catastrophic"] = 1
    with pytest.raises(VerificationRiskError, match="catastrophic must be boolean"):
        load_verification_risk_corpus(bad_outcome_catastrophic)


def test_unknown_identity_fields_fail_closed_at_every_corpus_level():
    decoded = json.loads(FIXTURE.read_text(encoding="utf-8"))

    unknown_root = copy.deepcopy(decoded)
    unknown_root["future_identity"] = "v2"
    with pytest.raises(VerificationRiskError, match="unknown fields: future_identity"):
        load_verification_risk_corpus(unknown_root)

    unknown_sample = copy.deepcopy(decoded)
    unknown_sample["samples"][0]["observation_id"] = "obs-1"
    with pytest.raises(VerificationRiskError, match="unknown fields: observation_id"):
        load_verification_risk_corpus(unknown_sample)

    unknown_mapping_outcome = copy.deepcopy(decoded)
    unknown_mapping_outcome["samples"][0]["outcomes"]["model-a"][
        "calibrated_confidence"
    ] = 0.9
    with pytest.raises(
        VerificationRiskError,
        match="unknown fields: calibrated_confidence",
    ):
        load_verification_risk_corpus(unknown_mapping_outcome)

    unknown_list_outcome = copy.deepcopy(decoded)
    unknown_list_outcome["samples"][0]["outcomes"] = [
        {"witness_id": witness_id, **outcome}
        for witness_id, outcome in unknown_list_outcome["samples"][0]["outcomes"].items()
    ]
    unknown_list_outcome["samples"][0]["outcomes"][0]["calibrated_confidence"] = 0.9
    with pytest.raises(
        VerificationRiskError,
        match="unknown fields: calibrated_confidence",
    ):
        load_verification_risk_corpus(unknown_list_outcome)

    unknown_dependency = copy.deepcopy(decoded)
    unknown_dependency["witnesses"][0]["dependency_profile"]["future_identity"] = "v2"
    with pytest.raises(VerificationRiskError, match="unknown fields: future_identity"):
        load_verification_risk_corpus(unknown_dependency)


def test_documented_aliases_and_list_outcomes_remain_supported():
    decoded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    decoded.pop("schema_version")
    decoded["metadata"]["vendor_extension"] = {"future_identity": "metadata-v2"}
    decoded["witnesses"][0]["metadata"] = {"vendor_extension": "witness-v2"}
    decoded["samples"] = [copy.deepcopy(decoded["samples"][0])]
    sample = decoded["samples"][0]
    sample["metadata"] = {"vendor_extension": "sample-v2"}
    sample["truth"] = sample.pop("label")
    sample["slice_id"] = sample.pop("slice")
    sample["witnesses"] = [
        {"id": witness_id, **outcome}
        for witness_id, outcome in sample.pop("outcomes").items()
    ]
    sample["witnesses"][0]["metadata"] = {"vendor_extension": "outcome-v2"}

    corpus = load_verification_risk_corpus(decoded)

    assert corpus.schema_version == "marker.verification_risk_corpus.v1"
    assert corpus.samples[0].label is True
    assert corpus.samples[0].slice_id == "matched"
    assert corpus.metadata["vendor_extension"] == {"future_identity": "metadata-v2"}
    assert corpus.witnesses[0].metadata["vendor_extension"] == "witness-v2"
    assert corpus.samples[0].metadata["vendor_extension"] == "sample-v2"
    assert corpus.samples[0].outcomes["model-a"].metadata["vendor_extension"] == "outcome-v2"
    assert set(corpus.samples[0].outcomes) == {
        "model-a",
        "model-b",
        "model-c",
        "unknown-model",
        "source-native",
    }


def test_slice_and_distribution_filters_do_not_leak():
    decoded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    decoded["samples"][0]["slice"] = "fit-only"
    decoded["samples"][0]["distribution"] = "matched"
    corpus = load_verification_risk_corpus(decoded)
    assert len(corpus.samples_for_slice("matched")) == 9
    assert len(corpus.samples_for_distribution("matched")) == 12
    assert evaluate_calibration(corpus, "model-a", slice_id="matched").sample_count == 9
    assert evaluate_calibration(corpus, "model-a", distribution="matched").sample_count == 12


def test_pair_math_is_exact_and_symmetric(corpus):
    pair = evaluate_pair(corpus, "model-a", "model-b", slice_id="matched")
    assert pair.sample_count == 10
    assert pair.marginal_error["model-a"].count == 2
    assert pair.marginal_error["model-a"].denominator == 10
    assert pair.marginal_error["model-a"].rate == pytest.approx(0.2)
    assert pair.marginal_error["model-b"].count == 4
    assert pair.joint_error.count == 2
    assert pair.joint_error.rate == pytest.approx(0.2)
    assert pair.agreement.count == 8
    assert pair.disagreement.count == 2
    assert pair.conditional_error_when_agree.count == 2
    assert pair.conditional_error_when_agree.denominator == 8
    assert pair.conditional_error_when_disagree.count == 2
    assert pair.conditional_error_when_disagree.denominator == 2
    assert pair.per_witness_disagreement_accuracy["model-a"].rate == pytest.approx(1.0)
    assert pair.per_witness_disagreement_accuracy["model-b"].rate == pytest.approx(0.0)
    assert pair.catastrophic_joint_failures.count == 1
    assert pair.catastrophic_joint_failures.denominator == 1
    reverse = evaluate_pair(corpus, ("model-b", "model-a"), slice_id="matched")
    assert reverse.as_dict()["agreement"] == pair.as_dict()["agreement"]
    assert reverse.as_dict()["joint_error"] == pair.as_dict()["joint_error"]


def test_zero_denominators_are_explicit(corpus):
    pair = evaluate_pair(corpus, "model-a", "model-b", slice_id="does-not-exist")
    assert pair.sample_count == 0
    assert pair.joint_error.rate is None
    assert pair.joint_error.status == "undefined_zero_denominator"
    assert pair.per_witness_disagreement_accuracy["model-a"].wilson_95 is None
    assert pair.catastrophic_joint_failures.denominator == 0


def test_wilson_bounds_are_deterministic(corpus):
    first = evaluate_pair(corpus, "model-a", "model-b", slice_id="matched")
    second = evaluate_pair(corpus, "model-a", "model-b", slice_id="matched")
    assert first.joint_error.wilson_95 == second.joint_error.wilson_95
    assert first.joint_error.lower < first.joint_error.rate < first.joint_error.upper


def test_calibration_reports_matched_shift_and_insufficient_support(corpus):
    matched = evaluate_calibration(corpus, "model-a", distribution="matched", min_samples=5)
    shifted = evaluate_calibration(corpus, "model-a", distribution="shifted", min_samples=5)
    insufficient = evaluate_calibration(
        corpus,
        "model-a",
        distribution="insufficient",
        min_samples=5,
    )
    assert matched.status == "ok"
    assert matched.method_id == "equal_width_ece_and_brier"
    assert matched.method_version == "marker.calibration.ece_brier.v1"
    assert matched.target_event == "witness_prediction_correct"
    assert matched.split_definition["fit_slice"] == "calibration-fit"
    assert matched.split_definition["shift_slice"] == "shifted"
    assert matched.support_uncertainty_method == "wilson_score_95_accuracy"
    assert matched.accuracy.wilson_95 is not None
    assert matched.sample_count == 12
    assert matched.expected_calibration_error == pytest.approx(0.0)
    assert shifted.status == "ok"
    assert shifted.sample_count == 5
    assert shifted.expected_calibration_error == pytest.approx(0.75)
    assert insufficient.status == "insufficient_support"
    assert insufficient.sample_count == 2
    assert insufficient.brier_score is not None


def test_all_five_baselines_share_slice_and_source_native_na(corpus):
    comparison = evaluate_baselines(corpus, slice_id="shifted", runtime_ms=12.5)
    assert tuple(comparison.baselines) == BASELINE_NAMES
    sample_ids = comparison.baselines[BASELINE_NAMES[1]].evaluated_sample_ids
    assert len(sample_ids) == 5
    for result in comparison.baselines.values():
        assert result.evaluated_sample_ids == sample_ids
        assert result.slice_id == "shifted"
    source_native = comparison.baselines[BASELINE_NAMES[0]]
    assert source_native.status == "not_applicable"
    assert source_native.not_applicable_reason
    assert source_native.coverage.rate == 0.0
    assert source_native.false_verified_rate.rate is None

    dependency = comparison.baselines[BASELINE_NAMES[4]]
    assert dependency.status == "risk_bound_not_met"
    assert dependency.not_applicable_reason
    assert dependency.accepted_count == 0


def test_deterministic_and_weighted_baselines_are_exercised_and_distinct(corpus):
    comparison = evaluate_baselines(corpus, slice_id="matched")
    deterministic = comparison.baselines[BASELINE_NAMES[0]]
    assert deterministic.status == "ok"
    assert deterministic.selected_witnesses == ("source-native",)
    assert deterministic.accepted_count == 10
    assert deterministic.false_verified_count == 0
    assert deterministic.coverage.rate == pytest.approx(1.0)

    best_single = comparison.baselines[BASELINE_NAMES[1]]
    assert best_single.selected_witnesses == ("model-a",)
    assert best_single.false_verified_count == 2

    naive = comparison.baselines[BASELINE_NAMES[2]]
    weighted = comparison.baselines[BASELINE_NAMES[3]]
    assert naive.false_verified_count == 1
    assert weighted.false_verified_count == 0
    assert naive.semantic_identity != weighted.semantic_identity

    dependency = comparison.baselines[BASELINE_NAMES[4]]
    assert dependency.status == "ok"
    assert dependency.not_applicable_reason is None


def test_dependency_gate_checks_worst_of_all_selected_pairs():
    samples = []
    for index in range(10):
        a_wrong = index < 4
        samples.append(
            {
                "id": f"sample-{index}",
                "slice": "evaluation",
                "distribution": "matched",
                "label": False,
                "outcomes": {
                    "a": {"prediction": a_wrong, "confidence": 0.8},
                    "b": {"prediction": False, "confidence": 0.8},
                    "c": {"prediction": a_wrong, "confidence": 0.8},
                },
            }
        )
    corpus = load_verification_risk_corpus(
        {
            "$schema": "marker.verification_risk_corpus.v1",
            "name": "three-witness-worst-pair",
            "metadata": {"baseline_best_single_witness": "a"},
            "witnesses": [
                {
                    "id": witness_id,
                    "model_family": f"family-{witness_id}",
                    "dependency_profile": {
                        "disclosure": "complete",
                        "base_lineage": f"checkpoint-{witness_id}",
                    },
                }
                for witness_id in ("a", "b", "c")
            ],
            "samples": samples,
        }
    )
    assert evaluate_pair(corpus, "a", "b", slice_id="evaluation").joint_error.count == 0
    assert evaluate_pair(corpus, "a", "c", slice_id="evaluation").joint_error.count == 4
    dependency = evaluate_baselines(corpus, slice_id="evaluation").baselines[BASELINE_NAMES[4]]
    assert dependency.status == "risk_bound_not_met"
    assert "('a', 'c')" in (dependency.not_applicable_reason or "")
    assert dependency.accepted_count == 0


def test_runtime_is_not_semantic_report_identity(corpus):
    report = evaluate_verification_risk(
        corpus,
        slice_id="matched",
        calibration_witness_ids=("model-a",),
        runtime_ms=1.0,
    )
    changed = replace(report, runtime_ms=999.0)
    assert semantic_artifact_identity(report) == semantic_artifact_identity(changed)
    baseline = report.baselines.baselines[BASELINE_NAMES[1]]
    assert semantic_artifact_identity(baseline) == semantic_artifact_identity(
        replace(baseline, runtime_ms=999.0)
    )
