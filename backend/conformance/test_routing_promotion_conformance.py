"""Invariant 25 routing-promotion conformance vectors.

The decision is recomputed end to end from the procedural population
through the real gate and compared against hand-checked constants and the
committed measurement artifact.  Any drift is a contract/population/policy
change and must be deliberate; identities are pinned so silent drift
fails this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.eval.routing_promotion import (
    ACTOR_REGISTRY_V1,
    ROUTING_PROMOTION_CONTRACT,
    build_final_holdout_corpus,
    evaluate_promotion,
)

ARTIFACT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "reference"
    / "measurements"
    / "inv25-routing-promotion-gate.json"
)
EVALUATED_AT = "2026-08-26T12:00:00+00:00"

EXPECTED_CONTRACT_IDENTITY = (
    "sha256:0d1b256589860b98b4f2aca2acf22d8b8f8b2a2939ae1ee59b8e182bf6cc5fd4"
)
EXPECTED_ACTOR_REGISTRY_IDENTITY = (
    "sha256:e666ccdb593d18fe6b3f2842a252f70284e396885e8347f5c1dab59e94efb65a"
)
EXPECTED_POPULATION_IDENTITY = (
    "sha256:bcb1ed0c063be78e71c1e24ba8e929f1dbd30d5db4ee165680c2a02dd7d31252"
)
EXPECTED_DECISION_IDENTITY = (
    "sha256:76256ce7e24a9e6a0d226203367730a80765c635839a5100c45b2eecca42f42c"
)


def _decision():
    return evaluate_promotion(
        build_final_holdout_corpus(), evaluated_at=EVALUATED_AT
    )


def test_frozen_identities_are_pinned():
    assert ROUTING_PROMOTION_CONTRACT.semantic_identity == EXPECTED_CONTRACT_IDENTITY
    assert ACTOR_REGISTRY_V1.semantic_identity == EXPECTED_ACTOR_REGISTRY_IDENTITY
    assert build_final_holdout_corpus().semantic_identity == EXPECTED_POPULATION_IDENTITY


def test_final_holdout_decision_is_insufficient_evidence_not_promotion():
    decision = _decision()
    assert decision.outcome == "insufficient_evidence"
    assert decision.reasons == (
        "support_below_frozen_floor",
        "catastrophic_bound_uncertifiable",
    )
    assert decision.semantic_identity == EXPECTED_DECISION_IDENTITY
    assert decision.contract_identity == EXPECTED_CONTRACT_IDENTITY
    assert decision.actor_registry_identity == EXPECTED_ACTOR_REGISTRY_IDENTITY
    assert decision.population_identity == EXPECTED_POPULATION_IDENTITY


def test_paired_comparison_constants_are_hand_checked():
    decision = _decision()
    matched = decision.slices["heldout-matched"]
    shifted = decision.slices["heldout-shifted"]
    # Hand-derived: 39 matched samples; candidate accepts 33 correctly and
    # abstains on the six high-risk model-only traps; fixed rules accept 33
    # with two ordinary false verifications; best single accepts all 39 with
    # 13 false verifications of which 8 are catastrophic.
    assert matched.sample_count == 39
    assert matched.utilities["dependency_aware_policy"] == 33 / 39
    assert matched.utilities["deterministic_source_native_only"] == 27 / 39
    assert matched.utilities["best_single_witness"] == -64 / 39
    # 25 shifted samples: the empirical gate fails on the shared text-layer
    # joint error and the candidate abstains everywhere; both comparators
    # false-verify 11 of 25 (4 catastrophic).
    assert shifted.sample_count == 25
    assert shifted.utilities["dependency_aware_policy"] == 0.0
    assert shifted.utilities["deterministic_source_native_only"] == -40 / 25
    assert shifted.utilities["best_single_witness"] == -40 / 25
    assert decision.candidate_gate_status == {
        "heldout-matched": "ok",
        "heldout-shifted": "risk_bound_not_met",
        "heldout-thin": "insufficient_support",
    }


def test_catastrophic_bound_constants_are_hand_checked():
    decision = _decision()
    catastrophic = decision.catastrophic
    # 22 catastrophic opportunities (18 matched + 4 shifted); the candidate
    # accepts 12 of them, all correctly, which certifies nothing at the 0.10
    # ceiling: the exact one-sided 95% upper bound stays above it.
    assert catastrophic.opportunities_total == 22
    assert catastrophic.opportunities_per_slice == {
        "heldout-matched": 18,
        "heldout-shifted": 4,
    }
    assert catastrophic.exposure_trials == 12
    assert catastrophic.observed_failures == 0
    assert catastrophic.upper_bound_95 == "0.2209221919"
    assert catastrophic.certifiable is False
    assert catastrophic.comparator_catastrophic == {
        "deterministic_source_native_only": 4,
        "best_single_witness": 12,
    }


def test_committed_artifact_matches_recomputed_evidence():
    artifact = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    decision = _decision()
    assert artifact["schema_version"] == "marker.routing_promotion.evidence.v1"
    assert artifact["decision"]["outcome"] == decision.outcome
    assert artifact["decision"]["reasons"] == list(decision.reasons)
    assert artifact["reproducibility"]["semantic_identity_runs"] == [
        decision.semantic_identity,
        decision.semantic_identity,
    ]
    assert artifact["reproducibility"]["stable"] is True
    assert artifact["identities"]["population"] == EXPECTED_POPULATION_IDENTITY
    assert artifact["leakage"]["clean"] is True
    assert artifact["population"]["slice_counts"] == {
        "heldout-matched": 39,
        "heldout-shifted": 25,
        "heldout-thin": 3,
    }


def test_runtime_variation_never_changes_the_semantic_decision():
    decision = _decision()
    timed = evaluate_promotion(
        build_final_holdout_corpus(),
        evaluated_at=EVALUATED_AT,
        runtime_ms=12345.6,
    )
    assert timed.semantic_identity == decision.semantic_identity
