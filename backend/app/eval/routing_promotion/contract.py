"""Frozen promotion decision contract for V3.2 invariant 25.

The contract is versioned and content-hashed BEFORE any final holdout
outcome is consumed.  Thresholds bind:

- the evaluated population and its required slices;
- the required comparators (deterministic fixed rules, best single engine);
- the scalar utility measure and its weights;
- the materiality rule (masterplan 7A.3 / 14C.5: if fixed rules capture at
  least 98% of candidate utility at lower complexity, fixed rules stay
  authoritative);
- the catastrophic-error ceiling and the exposure floor derived from it
  (rule of three / exact Clopper-Pearson: zero observed failures certify a
  ceiling only after enough exposure trials);
- the decision ordering that separates ``invalid_evidence`` (the study is
  not what it claims) from ``insufficient_evidence`` (the study cannot
  support a production claim) from ``shadow`` (the comparison ran and the
  candidate does not deserve promotion) from ``promote``.

Changing any bound here is a new contract version with a new semantic
identity; it must never happen after final outcomes are inspected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from app.eval.verification_risk.baselines import BASELINE_NAMES
from app.eval.verification_risk.common import VerificationRiskError
from app.eval.verification_risk.identity import _canonical_json, _identity

ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION = "marker.routing_promotion.contract.v1"

DECISION_PROMOTE = "promote"
DECISION_SHADOW = "shadow"
DECISION_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
DECISION_INVALID_EVIDENCE = "invalid_evidence"
DECISION_VOCABULARY: tuple[str, ...] = (
    DECISION_PROMOTE,
    DECISION_SHADOW,
    DECISION_INSUFFICIENT_EVIDENCE,
    DECISION_INVALID_EVIDENCE,
)

# Closed reason vocabulary.  Every decision carries at least one reason and
# may carry several; unknown reasons can never be serialized.
REASON_CONTRACT_FROZEN_AFTER_EVALUATION = "contract_frozen_after_evaluation"
REASON_DEVELOPMENT_EVIDENCE_OVERLAP = "development_evidence_overlap"
REASON_EXCLUSION_MANIFEST_STALE = "exclusion_manifest_stale"
REASON_POPULATION_SLICE_MISSING = "population_slice_missing"
REASON_ACTOR_REGISTRY_INVALID = "actor_registry_invalid"
REASON_COMPARATOR_NOT_APPLICABLE = "comparator_not_applicable"
REASON_SUPPORT_BELOW_FROZEN_FLOOR = "support_below_frozen_floor"
REASON_CATASTROPHIC_BOUND_UNCERTIFIABLE = "catastrophic_bound_uncertifiable"
REASON_CATASTROPHIC_ERRORS_OBSERVED = "catastrophic_errors_observed"
REASON_CATASTROPHIC_WORSE_THAN_COMPARATOR = "catastrophic_rate_worse_than_comparator"
REASON_CANDIDATE_UTILITY_NOT_POSITIVE = "candidate_utility_not_positive"
REASON_CANDIDATE_LOSES_TO_BEST_SINGLE = "candidate_loses_to_best_single"
REASON_GAIN_NOT_MATERIAL = "gain_over_fixed_rules_not_material"
REASON_SHIFT_INSTABILITY = "shift_instability"
REASON_ALL_CRITERIA_MET = "all_frozen_criteria_met"
REASON_VOCABULARY: tuple[str, ...] = (
    REASON_CONTRACT_FROZEN_AFTER_EVALUATION,
    REASON_DEVELOPMENT_EVIDENCE_OVERLAP,
    REASON_EXCLUSION_MANIFEST_STALE,
    REASON_POPULATION_SLICE_MISSING,
    REASON_ACTOR_REGISTRY_INVALID,
    REASON_COMPARATOR_NOT_APPLICABLE,
    REASON_SUPPORT_BELOW_FROZEN_FLOOR,
    REASON_CATASTROPHIC_BOUND_UNCERTIFIABLE,
    REASON_CATASTROPHIC_ERRORS_OBSERVED,
    REASON_CATASTROPHIC_WORSE_THAN_COMPARATOR,
    REASON_CANDIDATE_UTILITY_NOT_POSITIVE,
    REASON_CANDIDATE_LOSES_TO_BEST_SINGLE,
    REASON_GAIN_NOT_MATERIAL,
    REASON_SHIFT_INSTABILITY,
    REASON_ALL_CRITERIA_MET,
)


def catastrophic_exposure_floor(ceiling: float, confidence: float = 0.95) -> int:
    """Minimum zero-failure exposure trials for an exact one-sided bound.

    Zero observed catastrophic failures certify ``P(catastrophic) <= ceiling``
    at the given confidence only after ``n >= ln(1 - confidence) / ln(1 - ceiling)``
    trials (rule of three / exact binomial).  With ceiling 0.10 and 95%
    confidence the floor is 29 trials; a flawless smaller sample certifies
    nothing and must not become a zero-risk claim.
    """

    if not 0.0 < ceiling < 1.0:
        raise VerificationRiskError(f"ceiling must lie in (0, 1), got {ceiling!r}")
    if not 0.0 < confidence < 1.0:
        raise VerificationRiskError(f"confidence must lie in (0, 1), got {confidence!r}")
    return math.ceil(math.log(1.0 - confidence) / math.log(1.0 - ceiling))


@dataclass(frozen=True)
class PromotionContract:
    """Immutable, content-hashed promotion decision rules."""

    schema_version: str
    contract_id: str
    frozen_at: str
    candidate_policy: str
    fixed_rule_policy: str
    best_single_policy: str
    required_slices: tuple[str, str]
    control_slice: str
    utility_correct: float
    utility_false_verified: float
    utility_catastrophic: float
    materiality_fixed_rule_capture_max: float
    catastrophic_ceiling: float
    catastrophic_confidence: float
    min_matched_samples: int
    min_shifted_samples: int
    min_catastrophic_opportunities: int

    def validate(self) -> None:
        """Fail closed on any structurally unusable contract."""

        if self.schema_version != ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION:
            raise VerificationRiskError(
                f"unsupported contract schema_version {self.schema_version!r}"
            )
        if not self.contract_id.strip():
            raise VerificationRiskError("contract_id must be non-empty")
        try:
            datetime.fromisoformat(self.frozen_at)
        except ValueError as exc:
            raise VerificationRiskError(f"frozen_at not ISO-8601: {self.frozen_at!r}") from exc
        for name, policy in (
            ("candidate_policy", self.candidate_policy),
            ("fixed_rule_policy", self.fixed_rule_policy),
            ("best_single_policy", self.best_single_policy),
        ):
            if policy not in BASELINE_NAMES:
                raise VerificationRiskError(
                    f"{name} {policy!r} is not an executable baseline policy"
                )
        if len({self.candidate_policy, self.fixed_rule_policy, self.best_single_policy}) != 3:
            raise VerificationRiskError("candidate and comparators must be distinct policies")
        if len(self.required_slices) != 2 or not all(
            isinstance(item, str) and item.strip() for item in self.required_slices
        ):
            raise VerificationRiskError("required_slices must name matched and shifted slices")
        if not self.control_slice.strip():
            raise VerificationRiskError("control_slice must be non-empty")
        if self.utility_correct <= 0:
            raise VerificationRiskError("utility_correct must be positive")
        if self.utility_false_verified >= 0:
            raise VerificationRiskError("utility_false_verified must be negative")
        if self.utility_catastrophic >= self.utility_false_verified:
            raise VerificationRiskError(
                "utility_catastrophic must be more negative than utility_false_verified"
            )
        if not 0.0 < self.materiality_fixed_rule_capture_max < 1.0:
            raise VerificationRiskError("materiality capture bound must lie in (0, 1)")
        if not 0.0 < self.catastrophic_ceiling < 1.0:
            raise VerificationRiskError("catastrophic_ceiling must lie in (0, 1)")
        for name, value in (
            ("min_matched_samples", self.min_matched_samples),
            ("min_shifted_samples", self.min_shifted_samples),
            ("min_catastrophic_opportunities", self.min_catastrophic_opportunities),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise VerificationRiskError(f"{name} must be a positive integer")

    @property
    def exposure_floor(self) -> int:
        return catastrophic_exposure_floor(
            self.catastrophic_ceiling, self.catastrophic_confidence
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "frozen_at": self.frozen_at,
            "candidate_policy": self.candidate_policy,
            "fixed_rule_policy": self.fixed_rule_policy,
            "best_single_policy": self.best_single_policy,
            "required_slices": list(self.required_slices),
            "control_slice": self.control_slice,
            "utility_weights": {
                "correct": self.utility_correct,
                "false_verified": self.utility_false_verified,
                "catastrophic": self.utility_catastrophic,
                "abstain": 0.0,
            },
            "materiality_fixed_rule_capture_max": self.materiality_fixed_rule_capture_max,
            "catastrophic_ceiling": self.catastrophic_ceiling,
            "catastrophic_confidence": self.catastrophic_confidence,
            "catastrophic_exposure_floor": self.exposure_floor,
            "support_floors": {
                "matched": self.min_matched_samples,
                "shifted": self.min_shifted_samples,
                "catastrophic_opportunities": self.min_catastrophic_opportunities,
            },
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "semantic_identity": self.semantic_identity}

    def with_policy(self, **changes: Any) -> PromotionContract:
        """Validated copy used by tests to prove tamper sensitivity."""

        return replace(self, **changes)


#: The frozen contract governing the invariant 25 final holdout decision.
#: ``frozen_at`` predates every evaluation produced against this contract;
#: the decision engine enforces that ordering and refuses newer-looking
#: freezes on old evaluations only by re-freezing a NEW contract version.
ROUTING_PROMOTION_CONTRACT = PromotionContract(
    schema_version=ROUTING_PROMOTION_CONTRACT_SCHEMA_VERSION,
    contract_id="inv25-routing-promotion-v1",
    frozen_at="2026-08-26T00:00:00+00:00",
    candidate_policy="dependency_aware_policy",
    fixed_rule_policy="deterministic_source_native_only",
    best_single_policy="best_single_witness",
    required_slices=("heldout-matched", "heldout-shifted"),
    control_slice="heldout-thin",
    utility_correct=1.0,
    utility_false_verified=-2.0,
    utility_catastrophic=-10.0,
    materiality_fixed_rule_capture_max=0.98,
    catastrophic_ceiling=0.10,
    catastrophic_confidence=0.95,
    min_matched_samples=30,
    min_shifted_samples=20,
    min_catastrophic_opportunities=29,
)


def canonical_contract_json(contract: PromotionContract) -> str:
    """Deterministic canonical rendering (audit helper)."""

    return _canonical_json(contract.semantic_payload())
