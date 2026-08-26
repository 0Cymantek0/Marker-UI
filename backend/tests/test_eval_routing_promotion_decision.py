"""Fail-closed promotion-decision behavioral tests (invariant 25 workstreams C-E).

Every scenario is a paired evaluation over synthetic or real populations
built from the same four holdout witnesses, so the candidate selection
semantics stay identical and only the scenario being tested varies.
"""

from __future__ import annotations

import pytest

from app.eval.routing_promotion import (
    DECISION_INSUFFICIENT_EVIDENCE,
    DECISION_INVALID_EVIDENCE,
    DECISION_PROMOTE,
    DECISION_SHADOW,
    ROUTING_PROMOTION_CONTRACT,
    build_final_holdout_corpus,
    development_corpora,
    evaluate_promotion,
    holdout_population_document,
)
from app.eval.routing_promotion.population import BEST_SINGLE_WITNESS
from app.eval.verification_risk.baselines import BASELINE_NAMES
from app.eval.verification_risk.loaders import load_verification_risk_corpus

EVALUATED_AT = "2026-09-01T00:00:00+00:00"
VERIFIED = "verified"
REJECTED = "rejected"
ALL_WITNESSES = ("layout-k4", "native-2", "ocr-p7", "pipe-q9")


def _witnesses() -> list[dict]:
    return holdout_population_document()["witnesses"]


def _sample(
    sample_id: str,
    label: str,
    outcomes: dict[str, str],
    *,
    slice_id: str = "heldout-matched",
    distribution: str = "matched",
    catastrophic: bool = False,
    risk_level: str = "normal",
) -> dict:
    return {
        "sample_id": sample_id,
        "label": label,
        "slice": slice_id,
        "case": "synthetic",
        "distribution": distribution,
        "risk_level": risk_level,
        "catastrophic": catastrophic,
        "outcomes": {wid: {"prediction": pred} for wid, pred in outcomes.items()},
    }


def _clean(index: int, *, slice_id: str = "heldout-matched", cat: bool = False,
           prefix: str = "m") -> dict:
    label = VERIFIED if index % 2 == 0 else REJECTED
    return _sample(
        f"{prefix}-clean-{index:03d}",
        label,
        {wid: label for wid in ALL_WITNESSES},
        slice_id=slice_id,
        distribution="shifted" if slice_id == "heldout-shifted" else "matched",
        catastrophic=cat,
    )


def _corpus(samples: list[dict], *, witnesses: list[dict] | None = None) -> object:
    return load_verification_risk_corpus(
        {
            "schema_version": "marker.verification_risk_corpus.v1",
            "name": "synthetic-promotion-corpus",
            "witnesses": witnesses if witnesses is not None else _witnesses(),
            "samples": samples,
            "metadata": {"baseline_best_single_witness": BEST_SINGLE_WITNESS},
        }
    )


def _shifted_clean(count: int) -> list[dict]:
    return [
        _clean(i, slice_id="heldout-shifted", prefix="s") for i in range(count)
    ]


def _promote_capable_matched() -> list[dict]:
    """A matched slice that passes every promotion criterion by itself."""

    samples: list[dict] = []
    samples += [_clean(i) for i in range(20)]
    # 29 catastrophic opportunities with diverse correct evidence.
    samples += [
        _clean(i, cat=True, prefix="m-hsc") for i in range(29)
    ]
    # The deterministic path has bugs the diverse majority survives.
    for i in range(4):
        samples.append(
            _sample(
                f"m-npb-{i:02d}",
                REJECTED,
                {
                    "layout-k4": REJECTED,
                    "native-2": VERIFIED,
                    "ocr-p7": REJECTED,
                    "pipe-q9": REJECTED,
                },
            )
        )
    # High-risk model-only traps: the candidate must abstain; the best
    # single engine is catastrophically wrong.
    for i in range(8):
        samples.append(
            _sample(
                f"m-mhr-{i:02d}",
                REJECTED,
                {"ocr-p7": VERIFIED, "pipe-q9": VERIFIED},
                catastrophic=True,
                risk_level="high",
            )
        )
    # Shared-renderer silent corruption the candidate survives.
    for i in range(2):
        samples.append(
            _sample(
                f"m-src-{i:02d}",
                REJECTED,
                {
                    "layout-k4": REJECTED,
                    "native-2": REJECTED,
                    "ocr-p7": VERIFIED,
                    "pipe-q9": VERIFIED,
                },
                catastrophic=True,
            )
        )
    return samples


class TestPromotePath:
    def test_all_criteria_met_produces_the_only_promote_outcome(self):
        corpus = _corpus([*_promote_capable_matched(), *_shifted_clean(20)])
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_PROMOTE
        assert decision.reasons == ("all_frozen_criteria_met",)
        assert all(item.passed for item in decision.criteria)
        # The gate is not hardcoded to refuse: promotion is reachable when
        # every frozen bar is genuinely cleared.
        catastrophic = decision.catastrophic
        assert catastrophic.exposure_trials == 31
        assert catastrophic.observed_failures == 0
        assert float(catastrophic.upper_bound_95) <= 0.10


class TestComparatorOutcomes:
    def test_best_single_engine_winning_keeps_candidate_unpromoted(self):
        samples = [
            *(_clean(i) for i in range(20)),
            *(_clean(i + 20, prefix="m-xc") for i in range(10)),
        ]
        samples += [
            _sample(
                f"m-npb-real-{i:02d}",
                REJECTED,
                {
                    "layout-k4": REJECTED,
                    "native-2": VERIFIED,
                    "ocr-p7": REJECTED,
                    "pipe-q9": REJECTED,
                },
            )
            for i in range(10)
        ]
        # Model-only samples where the best single engine is right and the
        # candidate must abstain (high risk without authority evidence).
        samples += [
            _sample(
                f"m-right-{i:02d}",
                REJECTED,
                {"ocr-p7": REJECTED, "pipe-q9": REJECTED},
                risk_level="high",
            )
            for i in range(10)
        ]
        samples += [_clean(i, cat=True, prefix="m-hsc") for i in range(29)]
        corpus = _corpus([*samples, *_shifted_clean(20)])
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_SHADOW
        assert "candidate_loses_to_best_single" in decision.reasons
        matched = decision.slices["heldout-matched"]
        assert (
            matched.utilities["best_single_witness"]
            > matched.utilities["dependency_aware_policy"]
        )

    def test_sub_material_gain_over_fixed_rules_keeps_the_simpler_policy(self):
        # Masterplan 7A.3/14C.5: fixed rules capturing >= 98% of candidate
        # utility keep authority.  Here both are flawless and only the best
        # single engine errs.
        samples = [_clean(i) for i in range(68)]
        samples += [_clean(i, cat=True, prefix="m-hsc") for i in range(29)]
        for i in range(3):
            samples.append(
                _sample(
                    f"m-ocr-{i:02d}",
                    REJECTED,
                    {
                        "layout-k4": REJECTED,
                        "native-2": REJECTED,
                        "ocr-p7": VERIFIED,
                        "pipe-q9": REJECTED,
                    },
                )
            )
        corpus = _corpus([*samples, *_shifted_clean(20)])
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_SHADOW
        assert decision.reasons == ("gain_over_fixed_rules_not_material",)
        matched = decision.slices["heldout-matched"]
        capture = (
            matched.utilities["deterministic_source_native_only"]
            / matched.utilities["dependency_aware_policy"]
        )
        assert capture >= 0.98

    def test_matched_win_with_shifted_loss_cannot_promote(self):
        shifted: list[dict] = _shifted_clean(17)
        # Authority-contradicted traps: two selected witnesses outvote the
        # correct native path under shift.
        for i in range(8):
            shifted.append(
                _sample(
                    f"s-trap-{i:02d}",
                    REJECTED,
                    {
                        "layout-k4": VERIFIED,
                        "native-2": REJECTED,
                        "ocr-p7": VERIFIED,
                        "pipe-q9": VERIFIED,
                    },
                    slice_id="heldout-shifted",
                    distribution="shifted",
                )
            )
        corpus = _corpus([*_promote_capable_matched(), *shifted])
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_SHADOW
        assert decision.reasons == ("shift_instability",)
        shifted_eval = decision.slices["heldout-shifted"]
        assert (
            shifted_eval.utilities["dependency_aware_policy"]
            < shifted_eval.utilities["deterministic_source_native_only"]
        )

    def test_missing_comparator_identity_cannot_produce_promotion(self):
        samples = [*_promote_capable_matched(), *_shifted_clean(20)]
        # Fixed-rules comparator has no evidence anywhere: source-native
        # outcomes removed from every sample.
        stripped = []
        for sample in samples:
            sample = dict(sample)
            sample["outcomes"] = {
                wid: outcome
                for wid, outcome in sample["outcomes"].items()
                if wid != "native-2"
            }
            stripped.append(sample)
        corpus = _corpus(stripped)
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert "comparator_not_applicable" in decision.reasons

    def test_undeclared_best_single_fails_closed(self):
        document = holdout_population_document()
        corpus = load_verification_risk_corpus(
            {
                **document,
                "metadata": {
                    "population": document["metadata"]["population"],
                },
            }
        )
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert "comparator_not_applicable" in decision.reasons


class TestCatastrophicControls:
    def test_catastrophic_miss_hidden_by_aggregate_accuracy_is_blocked(self):
        samples = _promote_capable_matched()
        # Two catastrophic samples where the candidate's model majority is
        # wrong while the native path is right: aggregate accuracy stays
        # high, but the gate must refuse promotion.
        for i in range(2):
            samples.append(
                _sample(
                    f"m-fooled-{i:02d}",
                    REJECTED,
                    {
                        "layout-k4": VERIFIED,
                        "native-2": REJECTED,
                        "ocr-p7": VERIFIED,
                        "pipe-q9": VERIFIED,
                    },
                    catastrophic=True,
                )
            )
        corpus = _corpus([*samples, *_shifted_clean(20)])
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_INSUFFICIENT_EVIDENCE
        assert "catastrophic_errors_observed" in decision.reasons
        assert "catastrophic_bound_uncertifiable" in decision.reasons
        matched = decision.slices["heldout-matched"]
        assert matched.comparison.baselines["dependency_aware_policy"].accepted_count >= 55
        assert decision.catastrophic.observed_failures == 2

    def test_zero_observed_catastrophes_on_thin_support_is_not_zero_risk(self):
        samples = [_clean(i) for i in range(8)]
        samples += [_clean(i, cat=True, prefix="m-hsc") for i in range(12)]
        corpus = _corpus([*samples, *_shifted_clean(20)])
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_INSUFFICIENT_EVIDENCE
        assert "support_below_frozen_floor" in decision.reasons
        assert "catastrophic_bound_uncertifiable" in decision.reasons
        assert decision.catastrophic.observed_failures == 0
        assert float(decision.catastrophic.upper_bound_95) > 0.10
        payload = decision.catastrophic.as_dict()
        assert payload["zero_observed_failures_implies_zero_risk"] is False


class TestEvidenceValidity:
    @pytest.fixture(scope="class")
    def dev_corpora(self):
        return development_corpora()

    def test_pr75_development_corpus_cannot_be_its_own_holdout(self, dev_corpora):
        decision = evaluate_promotion(
            dev_corpora[0][1], evaluated_at=EVALUATED_AT, development=dev_corpora
        )
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert "development_evidence_overlap" in decision.reasons

    def test_consumed_pr82a_corpus_cannot_be_its_own_holdout(self, dev_corpora):
        decision = evaluate_promotion(
            dev_corpora[1][1], evaluated_at=EVALUATED_AT, development=dev_corpora
        )
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert "development_evidence_overlap" in decision.reasons

    def test_evaluation_before_contract_freeze_is_invalid(self):
        corpus = build_final_holdout_corpus()
        decision = evaluate_promotion(
            corpus, evaluated_at="2026-08-25T23:59:59+00:00"
        )
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert decision.reasons == ("contract_frozen_after_evaluation",)

    def test_unparseable_evaluation_timestamp_is_invalid(self):
        corpus = build_final_holdout_corpus()
        decision = evaluate_promotion(corpus, evaluated_at="not-a-time")
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert decision.reasons == ("evaluation_timestamp_unparseable",)

    def test_missing_required_slice_is_invalid(self):
        document = holdout_population_document()
        document["samples"] = [
            sample
            for sample in document["samples"]
            if sample["slice"] != "heldout-shifted"
        ]
        corpus = load_verification_risk_corpus(document)
        decision = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert decision.outcome == DECISION_INVALID_EVIDENCE
        assert decision.reasons == ("population_slice_missing",)


class TestDeterminismAndDiscrimination:
    def test_same_semantic_inputs_produce_the_same_identity(self):
        corpus = build_final_holdout_corpus()
        first = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        second = evaluate_promotion(corpus, evaluated_at=EVALUATED_AT)
        assert first.semantic_identity == second.semantic_identity
        runtime_variant = evaluate_promotion(
            corpus, evaluated_at=EVALUATED_AT, runtime_ms=999.0
        )
        assert runtime_variant.semantic_identity == first.semantic_identity

    def test_evaluator_discriminates_against_a_known_bad_policy(self):
        """The gate's evidence must catch an unsafe simpler policy.

        Naive majority voting accepts the model-only consensus traps the
        dependency-aware candidate refuses; a trivially non-discriminating
        evaluator could not show this gap.
        """

        decision = evaluate_promotion(
            build_final_holdout_corpus(), evaluated_at=EVALUATED_AT
        )
        matched = decision.slices["heldout-matched"]
        naive = matched.comparison.baselines["naive_majority_vote"]
        candidate = matched.comparison.baselines["dependency_aware_policy"]
        assert naive.catastrophic_error_count > candidate.catastrophic_error_count == 0
        assert naive.false_verified_count > candidate.false_verified_count == 0

    def test_real_holdout_decision_is_the_frozen_insufficient_evidence(self):
        decision = evaluate_promotion(
            build_final_holdout_corpus(), evaluated_at=EVALUATED_AT
        )
        assert decision.outcome == DECISION_INSUFFICIENT_EVIDENCE
        assert decision.reasons == (
            "support_below_frozen_floor",
            "catastrophic_bound_uncertifiable",
        )
        assert decision.candidate_gate_status == {
            "heldout-matched": "ok",
            "heldout-shifted": "risk_bound_not_met",
            "heldout-thin": "insufficient_support",
        }

    def test_contract_identity_is_bound_into_the_decision(self):
        decision = evaluate_promotion(
            build_final_holdout_corpus(), evaluated_at=EVALUATED_AT
        )
        assert decision.contract_identity == ROUTING_PROMOTION_CONTRACT.semantic_identity
        payload = decision.semantic_payload()
        assert payload["slices"]["heldout-matched"]["baselines"].keys() == set(
            BASELINE_NAMES
        )
