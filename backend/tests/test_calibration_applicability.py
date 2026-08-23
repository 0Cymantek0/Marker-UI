"""Calibration applicability artifact contract tests (PR88, invariant 23).

A v2 applicability artifact must answer, from its own serialized shape
and with no repository knowledge: what was calibrated, against which
population, under which assumptions, with how many usable observations,
which uncertainty method applies, whether distribution shift was
observed, when/why the evidence expires, and what zero observed
catastrophic failures actually means.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.eval.verification_risk import (
    CALIBRATION_APPLICABILITY_SCHEMA_VERSION,
    CalibrationApplicability,
    CatastrophicFailureInterpretation,
    VerificationRiskError,
    build_applicability,
    clopper_pearson_upper_95,
    evaluate_calibration,
    load_verification_risk_corpus,
)

FIXTURE = (
    Path(__file__).parents[1] / "conformance" / "fixtures" / "verification_risk_corpus_v1.json"
)

ASSUMPTIONS = {
    "label_definition": "witness prediction differs from the labeled truth",
    "sampling_frame": "all samples of the matched slice in the v1 corpus",
    "policy_id": "marker.high_risk.source_native",
    "policy_revision": "1",
    "workflow_class": "high_risk.source_native.v1",
    "distribution_class": "matched",
}


def _artifact(corpus, *, distribution: str = "matched") -> CalibrationApplicability:
    result = evaluate_calibration(
        corpus, "model-a", slice_id="matched", distribution=distribution
    )
    return build_applicability(
        result,
        population_name="invoice-total/en/matched/v1",
        sampling_frame="all samples of the matched slice in the v1 corpus",
        assumptions=ASSUMPTIONS,
        evaluated_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        retest_triggers=frozenset(
            {"time_expiry", "policy_revision_change", "population_shift"}
        ),
        catastrophic_failures=0,
        catastrophic_trials=result.sample_count,
    )


@pytest.fixture(scope="module")
def corpus():
    return load_verification_risk_corpus(FIXTURE)


# ---------------------------------------------------------------------------
# self-description
# ---------------------------------------------------------------------------


def test_artifact_names_population_assumptions_and_expiry_first_class(corpus):
    artifact = _artifact(corpus)
    data = artifact.as_dict()

    assert data["schema_version"] == CALIBRATION_APPLICABILITY_SCHEMA_VERSION
    # Population: enough identity to distinguish a shifted population.
    assert data["population"]["name"] == "invoice-total/en/matched/v1"
    assert data["population"]["corpus_identity"].startswith("sha256:")
    assert data["population"]["slice_id"] == "matched"
    assert data["population"]["distribution"] == "matched"
    # Assumptions: structured, inspectable, closed vocabulary.
    assert data["assumptions"]["policy_revision"] == "1"
    assert data["assumptions"]["workflow_class"] == "high_risk.source_native.v1"
    # Expiry: machine-evaluable window + retest triggers.
    assert data["validity"]["evaluated_at"] == "2026-08-01T00:00:00Z"
    assert data["validity"]["expires_at"] == "2026-09-01T00:00:00Z"
    assert data["validity"]["retest_triggers"] == [
        "policy_revision_change",
        "population_shift",
        "time_expiry",
    ]
    # Support and uncertainty method remain explicit.
    assert data["support"]["sample_count"] > 0
    assert data["support"]["status"] == "ok"
    assert data["uncertainty_method"] == "wilson_score_95_accuracy"
    # Shift state is explicit.
    assert data["shift_status"] == "matched"


def test_shifted_and_insufficient_slices_stay_honest(corpus):
    shifted = _artifact(corpus, distribution="shifted")
    assert shifted.shift_status == "shifted"
    assert shifted.as_dict()["shift_status"] == "shifted"

    insufficient = build_applicability(
        evaluate_calibration(
            corpus, "model-a", slice_id="insufficient", distribution="insufficient"
        ),
        population_name="invoice-total/en/insufficient/v1",
        sampling_frame="insufficient slice",
        assumptions=ASSUMPTIONS,
        evaluated_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        retest_triggers=frozenset({"support_below_minimum"}),
        catastrophic_failures=0,
        catastrophic_trials=0,
    )
    data = insufficient.as_dict()
    assert data["support"]["status"] == "insufficient_support"
    assert data["support"]["support_sufficient"] is False
    # Zero trials: no estimate at all, never an invented zero risk.
    assert data["catastrophic_failures"]["status"] == "not_evaluable"
    assert data["catastrophic_failures"]["upper_bound_95"] is None


# ---------------------------------------------------------------------------
# zero-catastrophe honesty
# ---------------------------------------------------------------------------


def test_zero_observed_catastrophes_is_not_zero_risk(corpus):
    artifact = _artifact(corpus)
    catastrophic = artifact.as_dict()["catastrophic_failures"]

    assert catastrophic["observed_failures"] == 0
    assert catastrophic["trials"] > 0
    # The bound exists, is strictly positive, and is a decimal string.
    assert catastrophic["upper_bound_95"] == clopper_pearson_upper_95(
        0, catastrophic["trials"]
    )
    assert float(catastrophic["upper_bound_95"]) > 0
    # Machine-checkable refusal to equate zero failures with zero risk.
    assert catastrophic["zero_failures_observed"] is True
    assert catastrophic["zero_failures_implies_zero_risk"] is False
    assert "not establish zero risk" in catastrophic["statement"]


def test_rule_of_three_bounds_are_exact_and_conservative():
    def binomial_cdf(k: int, n: int, p: float) -> float:
        return math.fsum(
            math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1)
        )

    for n in (5, 10, 50, 100):
        bound = float(clopper_pearson_upper_95(0, n))
        # Defining property: P(X=0 | p=bound) equals 0.05 exactly.
        assert math.isclose(binomial_cdf(0, n, bound), 0.05, abs_tol=1e-9)
        # The 3/n rule-of-three approximation must not undercut the
        # exact bound (it is always slightly larger than the bound).
        assert 0 < bound < 3.0 / n + 1e-12
    assert clopper_pearson_upper_95(10, 10) == "1"


def test_positive_failure_bounds_solve_the_exact_interval():
    def binomial_cdf(k: int, n: int, p: float) -> float:
        return math.fsum(
            math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1)
        )

    for failures, trials in ((1, 10), (3, 20), (7, 20), (9, 10)):
        bound = float(clopper_pearson_upper_95(failures, trials))
        assert 0.0 < bound <= 1.0
        assert math.isclose(binomial_cdf(failures, trials, bound), 0.05, abs_tol=1e-6)


def test_catastrophic_interpretation_fails_closed():
    # Positive trials without a bound cannot be constructed.
    with pytest.raises(VerificationRiskError, match="explicit upper bound"):
        CatastrophicFailureInterpretation(
            observed_failures=0, trials=10, upper_bound_95=None, status="bounded"
        )
    # A zero bound would serialize "zero failures = zero risk".
    with pytest.raises(VerificationRiskError, match="never serialize"):
        CatastrophicFailureInterpretation(
            observed_failures=0, trials=10, upper_bound_95="0", status="bounded"
        )
    with pytest.raises(VerificationRiskError, match=r"\(0, 1\]"):
        CatastrophicFailureInterpretation(
            observed_failures=0, trials=10, upper_bound_95="1.5", status="bounded"
        )
    # Failures cannot exceed trials; zero trials admit nothing.
    with pytest.raises(VerificationRiskError, match="exceed trials"):
        CatastrophicFailureInterpretation(
            observed_failures=3, trials=2, upper_bound_95="0.5", status="bounded"
        )
    # Zero trials admit no estimate; a claimed failure count on zero
    # trials is a count/denominator contradiction.
    with pytest.raises(VerificationRiskError, match="exceed trials"):
        CatastrophicFailureInterpretation(
            observed_failures=1, trials=0, upper_bound_95="0.5", status="bounded"
        )


# ---------------------------------------------------------------------------
# applicability and expiry
# ---------------------------------------------------------------------------


def test_applies_to_matches_only_named_dimensions(corpus):
    artifact = _artifact(corpus)
    assert artifact.applies_to(
        policy_id="marker.high_risk.source_native",
        policy_revision="1",
        workflow_class="high_risk.source_native.v1",
        slice_id="matched",
    )
    # Population/scope mismatch refuses applicability.
    assert not artifact.applies_to(policy_revision="2")
    assert not artifact.applies_to(policy_id="marker.other")
    assert not artifact.applies_to(workflow_class="standard.v1")
    assert not artifact.applies_to(slice_id="shifted")


def test_expiry_is_machine_evaluable_with_exact_boundary(corpus):
    artifact = _artifact(corpus)
    assert not artifact.is_expired("2026-08-31T23:59:59Z")
    assert not artifact.is_expired("2026-09-01T00:00:00Z")  # boundary: not expired
    assert artifact.is_expired("2026-09-01T00:00:01Z")
    assert artifact.retest_required_for(frozenset({"policy_revision_change"}))
    assert artifact.retest_required_for(frozenset({"model_or_operator_change", "population_shift"}))
    assert not artifact.retest_required_for(frozenset({"support_below_minimum"}))


# ---------------------------------------------------------------------------
# fail-closed construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"population_name": ""},
        {"sampling_frame": "  "},
        {"assumptions": {"policy_id": "p"}},
        {"assumptions": {**ASSUMPTIONS, "mystery_key": "x"}},
        {"assumptions": {**ASSUMPTIONS, "policy_revision": ""}},
        {"expires_at": "2026-07-01T00:00:00Z"},
        {"retest_triggers": frozenset()},
        {"retest_triggers": frozenset({"whenever"})},
        {"evaluated_at": "not-a-timestamp"},
    ],
)
def test_invalid_applicability_fails_closed(corpus, changes):
    result = evaluate_calibration(
        corpus, "model-a", slice_id="matched", distribution="matched"
    )
    base = dict(
        population_name="invoice-total/en/matched/v1",
        sampling_frame="all samples of the matched slice in the v1 corpus",
        assumptions=ASSUMPTIONS,
        evaluated_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        retest_triggers=frozenset({"time_expiry"}),
        catastrophic_failures=0,
        catastrophic_trials=result.sample_count,
    )
    base.update(changes)
    with pytest.raises(VerificationRiskError):
        build_applicability(result, **base)


def test_catastrophic_interpretation_is_mandatory():
    with pytest.raises(VerificationRiskError, match="catastrophic"):
        CalibrationApplicability(
            population_name="p",
            corpus_identity="sha256:" + "0" * 64,
            slice_id="matched",
            distribution="matched",
            sampling_frame="frame",
            assumptions=ASSUMPTIONS,
            shift_status="matched",
            evaluated_at="2026-08-01T00:00:00Z",
            expires_at="2026-09-01T00:00:00Z",
            retest_triggers=frozenset({"time_expiry"}),
            uncertainty_method="wilson_score_95_accuracy",
            method_id="equal_width_ece_and_brier",
            method_version="marker.calibration.ece_brier.v1",
            target_event="witness_prediction_correct",
            catastrophic=None,
        )


def test_invalid_shift_status_fails_closed():
    with pytest.raises(VerificationRiskError, match="shift_status"):
        CalibrationApplicability(
            population_name="p",
            corpus_identity="sha256:" + "0" * 64,
            slice_id="matched",
            distribution="matched",
            sampling_frame="frame",
            assumptions=ASSUMPTIONS,
            shift_status="sometimes",
            evaluated_at="2026-08-01T00:00:00Z",
            expires_at="2026-09-01T00:00:00Z",
            retest_triggers=frozenset({"time_expiry"}),
            uncertainty_method="wilson_score_95_accuracy",
            method_id="equal_width_ece_and_brier",
            method_version="marker.calibration.ece_brier.v1",
            target_event="witness_prediction_correct",
            catastrophic=CatastrophicFailureInterpretation.from_counts(0, 10),
        )


# ---------------------------------------------------------------------------
# serialization round-trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_semantics_without_drift(corpus):
    artifact = _artifact(corpus)
    rematerialized = CalibrationApplicability.from_dict(artifact.as_dict())
    assert rematerialized.as_dict() == artifact.as_dict()
    # Deterministic rebuild: identical inputs, identical serialization.
    assert _artifact(corpus).as_dict() == artifact.as_dict()


def test_from_dict_fails_closed_on_unknown_or_wrong_version(corpus):
    data = _artifact(corpus).as_dict()
    corrupted = dict(data)
    corrupted["suddenly_new"] = True
    with pytest.raises(VerificationRiskError, match="unknown applicability fields"):
        CalibrationApplicability.from_dict(corrupted)

    wrong_version = dict(data)
    wrong_version["schema_version"] = "marker.calibration.applicability.v1"
    with pytest.raises(VerificationRiskError, match="unsupported"):
        CalibrationApplicability.from_dict(wrong_version)

    missing = dict(data)
    del missing["validity"]
    with pytest.raises(VerificationRiskError, match="missing"):
        CalibrationApplicability.from_dict(missing)


def test_runtime_observations_stay_outside_applicability_content(corpus):
    result = evaluate_calibration(
        corpus, "model-a", slice_id="matched", distribution="matched"
    )
    with_runtime = build_applicability(
        result,
        population_name="invoice-total/en/matched/v1",
        sampling_frame="all samples of the matched slice in the v1 corpus",
        assumptions=ASSUMPTIONS,
        evaluated_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        retest_triggers=frozenset({"time_expiry", "policy_revision_change", "population_shift"}),
        catastrophic_failures=0,
        catastrophic_trials=result.sample_count,
        runtime_metrics={"elapsed_ms": 12345},
    ).as_dict()
    # Runtime lives nested under metrics.runtime; the applicability
    # dimensions (population/assumptions/validity) are untouched by it.
    assert with_runtime["metrics"]["runtime"] == {"elapsed_ms": 12345}
    assert with_runtime["population"] == _artifact(corpus).as_dict()["population"]
    assert with_runtime["validity"] == _artifact(corpus).as_dict()["validity"]
