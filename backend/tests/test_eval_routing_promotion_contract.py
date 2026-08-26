"""Frozen promotion-contract tests (invariant 25 workstream A)."""

from __future__ import annotations

import pytest

from app.eval.routing_promotion.contract import (
    DECISION_VOCABULARY,
    REASON_ALL_CRITERIA_MET,
    REASON_VOCABULARY,
    ROUTING_PROMOTION_CONTRACT,
    ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION,
    PromotionContract,
    catastrophic_exposure_floor,
)
from app.eval.verification_risk.common import VerificationRiskError
from app.eval.verification_risk.identity import _identity


class TestFrozenContract:
    def test_default_contract_validates_and_pins_governing_semantics(self):
        contract = ROUTING_PROMOTION_CONTRACT
        contract.validate()
        assert contract.candidate_policy == "dependency_aware_policy"
        assert contract.fixed_rule_policy == "deterministic_source_native_only"
        assert contract.best_single_policy == "best_single_witness"
        assert contract.required_slices == ("heldout-matched", "heldout-shifted")
        assert contract.control_slice == "heldout-thin"
        # Masterplan 7A.3 / 14C.5 materiality rule.
        assert contract.materiality_fixed_rule_capture_max == 0.98
        # Utility ordering: catastrophic false verification must hurt most.
        assert contract.utility_correct > 0
        assert 0 > contract.utility_false_verified > contract.utility_catastrophic

    def test_identity_is_stable_and_covers_every_threshold(self):
        first = ROUTING_PROMOTION_CONTRACT.semantic_identity
        assert first.startswith("sha256:")
        assert ROUTING_PROMOTION_CONTRACT.semantic_identity == first
        payload = ROUTING_PROMOTION_CONTRACT.semantic_payload()
        assert _identity(payload) == first
        for key in (
            "utility_weights",
            "materiality_fixed_rule_capture_max",
            "catastrophic_ceiling",
            "catastrophic_exposure_floor",
            "support_floors",
        ):
            assert key in payload

    def test_any_threshold_change_produces_a_new_identity(self):
        base = ROUTING_PROMOTION_CONTRACT
        tampered = base.with_policy(catastrophic_ceiling=0.05)
        tampered.validate()
        assert tampered.semantic_identity != base.semantic_identity
        tampered_weights = base.with_policy(utility_false_verified=-3.0)
        assert tampered_weights.semantic_identity != base.semantic_identity
        tampered_support = base.with_policy(min_matched_samples=31)
        assert tampered_support.semantic_identity != base.semantic_identity

    def test_unknown_decision_or_reason_can_never_be_emitted(self):
        assert DECISION_VOCABULARY == (
            "promote",
            "shadow",
            "insufficient_evidence",
            "invalid_evidence",
        )
        assert REASON_ALL_CRITERIA_MET in REASON_VOCABULARY
        assert len(REASON_VOCABULARY) == len(set(REASON_VOCABULARY))


class TestContractValidation:
    def test_unknown_policy_id_fails_closed(self):
        contract = ROUTING_PROMOTION_CONTRACT.with_policy(candidate_policy="learned_router")
        with pytest.raises(VerificationRiskError, match="not an executable baseline policy"):
            contract.validate()

    def test_candidate_colliding_with_comparator_fails_closed(self):
        contract = ROUTING_PROMOTION_CONTRACT.with_policy(
            fixed_rule_policy="dependency_aware_policy"
        )
        with pytest.raises(VerificationRiskError, match="distinct policies"):
            contract.validate()

    def test_non_punishing_utility_weights_fail_closed(self):
        with pytest.raises(VerificationRiskError, match="more negative"):
            ROUTING_PROMOTION_CONTRACT.with_policy(utility_catastrophic=-1.0).validate()
        with pytest.raises(VerificationRiskError, match="utility_false_verified"):
            ROUTING_PROMOTION_CONTRACT.with_policy(utility_false_verified=0.5).validate()

    def test_out_of_range_bounds_fail_closed(self):
        with pytest.raises(VerificationRiskError, match="catastrophic_ceiling"):
            ROUTING_PROMOTION_CONTRACT.with_policy(catastrophic_ceiling=1.5).validate()
        with pytest.raises(VerificationRiskError, match="materiality"):
            ROUTING_PROMOTION_CONTRACT.with_policy(
                materiality_fixed_rule_capture_max=1.0
            ).validate()

    def test_non_integer_or_non_positive_support_floors_fail_closed(self):
        with pytest.raises(VerificationRiskError, match="positive integer"):
            ROUTING_PROMOTION_CONTRACT.with_policy(min_matched_samples=2.5).validate()
        with pytest.raises(VerificationRiskError, match="positive integer"):
            ROUTING_PROMOTION_CONTRACT.with_policy(min_shifted_samples=0).validate()

    def test_unparseable_frozen_at_fails_closed(self):
        contract = ROUTING_PROMOTION_CONTRACT.with_policy(frozen_at="not-a-timestamp")
        with pytest.raises(VerificationRiskError, match="frozen_at"):
            contract.validate()


class TestExposureFloor:
    def test_ceiling_0_10_requires_29_zero_failure_trials(self):
        # ln(0.05)/ln(0.90) = 28.43... -> 29 trials before a bound claim.
        assert catastrophic_exposure_floor(0.10) == 29

    def test_tighter_ceilings_require_proportionally_more_trials(self):
        assert catastrophic_exposure_floor(0.05) == 59
        assert catastrophic_exposure_floor(0.20) == 14
        assert catastrophic_exposure_floor(0.05) > catastrophic_exposure_floor(0.10)

    def test_invalid_ceilings_fail_closed(self):
        from app.eval.verification_risk.common import VerificationRiskError as Err

        with pytest.raises(Err):
            catastrophic_exposure_floor(0.0)
        with pytest.raises(Err):
            catastrophic_exposure_floor(1.0)

    def test_contract_payload_binds_the_derived_floor(self):
        payload = ROUTING_PROMOTION_CONTRACT.semantic_payload()
        assert payload["catastrophic_exposure_floor"] == 29
        assert payload["support_floors"]["catastrophic_opportunities"] == 29

    def test_schema_version_is_pinned(self):
        assert (
            ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION
            == "marker.routing_promotion.contract.v1"
        )
