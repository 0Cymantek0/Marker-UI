"""Fail-closed paired routing-promotion decision (invariant 25, workstreams C/D).

Turns the frozen contract, the declared actors, and the independent final
holdout into one auditable decision.  The gate evaluates the candidate and
both required comparators on identical samples, derives scalar utility
under the frozen weights, bounds catastrophic risk with the exact
Clopper-Pearson upper bound, and classifies along the frozen ordering:

1. ``invalid_evidence``  — the study is not what it claims (temporal
   ordering violated, contamination detected, stale manifest, missing
   slice, comparator not applicable).  Never an exception operators can
   ignore: the decision itself records why the evidence is invalid.
2. ``insufficient_evidence`` — the study cannot support a production
   claim (support floors unmet, or catastrophic risk not certifiable at
   the frozen ceiling; zero observed failures on thin exposure certify
   nothing).
3. ``shadow`` — the comparison ran and the candidate does not deserve
   promotion (loses to the best single engine, gain over fixed rules not
   material per the masterplan 98% rule, shift instability, catastrophic
   behavior worse than a comparator, or non-positive utility).
4. ``promote`` — every frozen criterion is met.

All criteria are recorded with numeric detail even when a higher tier
short-circuits the outcome, so the artifact always shows exactly which
bar was or was not cleared and by how much.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.eval.verification_risk.applicability import clopper_pearson_upper_95
from app.eval.verification_risk.baselines import (
    BASELINE_NAMES,
    BaselineComparison,
    BaselineResult,
    dependency_aware_evaluation,
    evaluate_baselines,
)
from app.eval.verification_risk.common import VerificationRiskError
from app.eval.verification_risk.identity import _identity
from app.eval.verification_risk.models import VerificationRiskCorpus

from .actors import ActorRegistry, ROLE_BEST_SINGLE, ROLE_CANDIDATE, ROLE_FIXED_RULES
from .contract import (
    DECISION_INSUFFICIENT_EVIDENCE,
    DECISION_INVALID_EVIDENCE,
    DECISION_PROMOTE,
    DECISION_SHADOW,
    PromotionContract,
    REASON_ACTOR_REGISTRY_INVALID,
    REASON_ALL_CRITERIA_MET,
    REASON_CANDIDATE_LOSES_TO_BEST_SINGLE,
    REASON_CANDIDATE_UTILITY_NOT_POSITIVE,
    REASON_CATASTROPHIC_BOUND_UNCERTIFIABLE,
    REASON_CATASTROPHIC_ERRORS_OBSERVED,
    REASON_CATASTROPHIC_WORSE_THAN_COMPARATOR,
    REASON_COMPARATOR_NOT_APPLICABLE,
    REASON_CONTRACT_FROZEN_AFTER_EVALUATION,
    REASON_DEVELOPMENT_EVIDENCE_OVERLAP,
    REASON_EVALUATION_TIMESTAMP_UNPARSEABLE,
    REASON_EXCLUSION_MANIFEST_STALE,
    REASON_GAIN_NOT_MATERIAL,
    REASON_POPULATION_SLICE_MISSING,
    REASON_SHIFT_INSTABILITY,
    REASON_SUPPORT_BELOW_FROZEN_FLOOR,
    ROUTING_PROMOTION_CONTRACT,
)
from .population import LeakageReport, evaluate_leakage

PROMOTION_EVIDENCE_SCHEMA_VERSION = "marker.routing_promotion.evidence.v1"


@dataclass(frozen=True)
class CriterionResult:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class SliceEvaluation:
    """All five policies on identical samples of one slice, with utility."""

    slice_id: str
    sample_count: int
    catastrophic_opportunities: int
    comparison: BaselineComparison
    utilities: Mapping[str, float]

    def as_dict(self) -> dict[str, Any]:
        baselines: dict[str, Any] = {}
        for name in BASELINE_NAMES:
            result: BaselineResult = self.comparison.baselines[name]
            baselines[name] = {
                "status": result.status,
                "not_applicable_reason": result.not_applicable_reason,
                "sample_count": result.sample_count,
                "accepted_count": result.accepted_count,
                "false_verified_count": result.false_verified_count,
                "catastrophic_error_count": result.catastrophic_error_count,
                "coverage": result.coverage.as_dict(),
                "utility": self.utilities[name],
                "semantic_identity": result.semantic_identity,
            }
        return {
            "slice_id": self.slice_id,
            "sample_count": self.sample_count,
            "catastrophic_opportunities": self.catastrophic_opportunities,
            "baselines": baselines,
        }


@dataclass(frozen=True)
class CatastrophicAssessment:
    """Exact one-sided bound on the candidate's catastrophic-accept risk."""

    opportunities_total: int
    opportunities_per_slice: Mapping[str, int]
    exposure_trials: int
    observed_failures: int
    exposure_floor: int
    ceiling: float
    upper_bound_95: str | None
    evaluable: bool
    certifiable: bool
    comparator_catastrophic: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunities_total": self.opportunities_total,
            "opportunities_per_slice": dict(self.opportunities_per_slice),
            "exposure_trials": self.exposure_trials,
            "observed_failures": self.observed_failures,
            "exposure_floor": self.exposure_floor,
            "ceiling": self.ceiling,
            "upper_bound_95": self.upper_bound_95,
            "evaluable": self.evaluable,
            "certifiable": self.certifiable,
            "comparator_catastrophic": dict(self.comparator_catastrophic),
            "zero_observed_failures_implies_zero_risk": False,
        }


@dataclass(frozen=True)
class PromotionDecision:
    outcome: str
    reasons: tuple[str, ...]
    reason_details: tuple[str, ...]
    evaluated_at: str
    contract_identity: str
    actor_registry_identity: str
    population_identity: str
    slices: Mapping[str, SliceEvaluation]
    catastrophic: CatastrophicAssessment | None
    criteria: tuple[CriterionResult, ...]
    leakage: LeakageReport
    candidate_gate_status: Mapping[str, str] = field(default_factory=dict)
    runtime_ms: float | None = None

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PROMOTION_EVIDENCE_SCHEMA_VERSION,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "reason_details": list(self.reason_details),
            "evaluated_at": self.evaluated_at,
            "identities": {
                "contract": self.contract_identity,
                "actor_registry": self.actor_registry_identity,
                "population": self.population_identity,
            },
            "slices": {
                slice_id: evaluation.as_dict()
                for slice_id, evaluation in self.slices.items()
            },
            "catastrophic": self.catastrophic.as_dict() if self.catastrophic else None,
            "criteria": [item.as_dict() for item in self.criteria],
            "leakage": self.leakage.as_dict(),
            "candidate_gate_status": dict(self.candidate_gate_status),
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "decision": {
                "outcome": self.outcome,
                "reasons": list(self.reasons),
                "reason_details": list(self.reason_details),
            },
            "runtime_ms": self.runtime_ms,
            "semantic_identity": self.semantic_identity,
        }

    def criterion(self, name: str) -> CriterionResult:
        for item in self.criteria:
            if item.name == name:
                return item
        raise KeyError(name)


def _utility(
    result: BaselineResult, contract: PromotionContract
) -> float:
    """Frozen scalar utility: mean per-sample outcome value over the slice."""

    if result.sample_count == 0:
        return 0.0
    correct = result.accepted_count - result.false_verified_count
    ordinary_false = result.false_verified_count - result.catastrophic_error_count
    total = (
        correct * contract.utility_correct
        + ordinary_false * contract.utility_false_verified
        + result.catastrophic_error_count * contract.utility_catastrophic
    )
    return total / result.sample_count


def _invalid(
    outcome_reasons: tuple[str, ...],
    details: tuple[str, ...],
    *,
    evaluated_at: str,
    contract: PromotionContract,
    actors: ActorRegistry,
    corpus: VerificationRiskCorpus,
    leakage: LeakageReport,
) -> PromotionDecision:
    return PromotionDecision(
        outcome=DECISION_INVALID_EVIDENCE,
        reasons=outcome_reasons,
        reason_details=details,
        evaluated_at=evaluated_at,
        contract_identity=contract.semantic_identity,
        actor_registry_identity=actors.semantic_identity,
        population_identity=corpus.semantic_identity,
        slices={},
        catastrophic=None,
        criteria=(),
        leakage=leakage,
    )


def evaluate_promotion(
    corpus: VerificationRiskCorpus,
    *,
    evaluated_at: str,
    contract: PromotionContract = ROUTING_PROMOTION_CONTRACT,
    actors: ActorRegistry | None = None,
    development: tuple[tuple[dict[str, str], VerificationRiskCorpus], ...] | None = None,
    runtime_ms: float | None = None,
) -> PromotionDecision:
    """Decide promote / shadow / insufficient / invalid under frozen rules."""

    from .actors import ACTOR_REGISTRY_V1

    if actors is None:
        actors = ACTOR_REGISTRY_V1
    if not isinstance(corpus, VerificationRiskCorpus):
        raise TypeError("evaluate_promotion requires a VerificationRiskCorpus")

    leakage = evaluate_leakage(corpus, development=development)

    invalid_reasons: list[str] = []
    invalid_details: list[str] = []

    for registry in (contract, actors):
        try:
            registry.validate()
        except VerificationRiskError as exc:
            invalid_reasons.append(REASON_ACTOR_REGISTRY_INVALID)
            invalid_details.append(f"registry/contract invalid: {exc}")
    if invalid_reasons:
        return _invalid(
            tuple(invalid_reasons),
            tuple(invalid_details),
            evaluated_at=evaluated_at,
            contract=contract,
            actors=actors,
            corpus=corpus,
            leakage=leakage,
        )

    # 1. Temporal ordering: outcomes may only be consumed after the freeze.
    try:
        evaluated_ts = datetime.fromisoformat(evaluated_at)
        frozen_ts = datetime.fromisoformat(contract.frozen_at)
    except ValueError as exc:
        return _invalid(
            (REASON_EVALUATION_TIMESTAMP_UNPARSEABLE,),
            (f"evaluated_at {evaluated_at!r}: {exc}",),
            evaluated_at=evaluated_at,
            contract=contract,
            actors=actors,
            corpus=corpus,
            leakage=leakage,
        )
    if evaluated_ts <= frozen_ts:
        return _invalid(
            (REASON_CONTRACT_FROZEN_AFTER_EVALUATION,),
            (
                f"evaluation {evaluated_at} does not postdate the contract "
                f"freeze {contract.frozen_at}",
            ),
            evaluated_at=evaluated_at,
            contract=contract,
            actors=actors,
            corpus=corpus,
            leakage=leakage,
        )

    # 2. Contamination: the study must be what it claims.
    if leakage.manifest_mismatches:
        invalid_reasons.append(REASON_EXCLUSION_MANIFEST_STALE)
        invalid_details.extend(leakage.manifest_mismatches)
    if (
        leakage.sample_id_overlaps
        or leakage.sample_content_overlaps
        or leakage.witness_dependency_overlaps
    ):
        invalid_reasons.append(REASON_DEVELOPMENT_EVIDENCE_OVERLAP)
        invalid_details.extend(
            [
                *leakage.sample_id_overlaps,
                *leakage.sample_content_overlaps,
                *leakage.witness_dependency_overlaps,
            ]
        )
    if invalid_reasons:
        return _invalid(
            tuple(invalid_reasons),
            tuple(invalid_details),
            evaluated_at=evaluated_at,
            contract=contract,
            actors=actors,
            corpus=corpus,
            leakage=leakage,
        )

    # 3. Population shape.
    missing = [
        slice_id
        for slice_id in contract.required_slices
        if slice_id not in corpus.slice_ids
    ]
    if missing:
        return _invalid(
            (REASON_POPULATION_SLICE_MISSING,),
            (f"required slices absent from population: {missing}",),
            evaluated_at=evaluated_at,
            contract=contract,
            actors=actors,
            corpus=corpus,
            leakage=leakage,
        )

    # 4. Paired evaluation on identical samples.
    evaluated_slices = [
        slice_id
        for slice_id in (*contract.required_slices, contract.control_slice)
        if slice_id in corpus.slice_ids
    ]
    try:
        comparisons = {
            slice_id: evaluate_baselines(corpus, slice_id=slice_id)
            for slice_id in evaluated_slices
        }
    except VerificationRiskError as exc:
        return _invalid(
            (REASON_COMPARATOR_NOT_APPLICABLE,),
            (f"paired evaluation failed closed: {exc}",),
            evaluated_at=evaluated_at,
            contract=contract,
            actors=actors,
            corpus=corpus,
            leakage=leakage,
        )

    fixed_policy = actors.actor_for(ROLE_FIXED_RULES).policy_id
    best_policy = actors.actor_for(ROLE_BEST_SINGLE).policy_id
    candidate_policy = actors.actor_for(ROLE_CANDIDATE).policy_id
    for slice_id in contract.required_slices:
        for role, policy_id in (
            (ROLE_FIXED_RULES, fixed_policy),
            (ROLE_BEST_SINGLE, best_policy),
        ):
            status = comparisons[slice_id].baselines[policy_id].status
            if status != "ok":
                return _invalid(
                    (REASON_COMPARATOR_NOT_APPLICABLE,),
                    (
                        f"{role} comparator ({policy_id}) status {status!r} on "
                        f"slice {slice_id!r}; a missing comparator forbids "
                        "promotion rather than shrinking the denominator",
                    ),
                    evaluated_at=evaluated_at,
                    contract=contract,
                    actors=actors,
                    corpus=corpus,
                    leakage=leakage,
                )

    slices: dict[str, SliceEvaluation] = {}
    gate_status: dict[str, str] = {}
    candidate_evaluations = {}
    for slice_id in evaluated_slices:
        comparison = comparisons[slice_id]
        evaluation = dependency_aware_evaluation(corpus, slice_id=slice_id)
        candidate_evaluations[slice_id] = evaluation
        gate_status[slice_id] = evaluation.gate_status
        slices[slice_id] = SliceEvaluation(
            slice_id=slice_id,
            sample_count=len(corpus.samples_for_slice(slice_id)),
            catastrophic_opportunities=sum(
                1
                for sample in corpus.samples_for_slice(slice_id)
                if sample.catastrophic
            ),
            comparison=comparison,
            utilities={
                name: _utility(comparison.baselines[name], contract)
                for name in BASELINE_NAMES
            },
        )

    # 5. Catastrophic bound over the required slices' opportunities.
    opportunities_per_slice: dict[str, int] = {}
    exposure = 0
    failures = 0
    for slice_id in contract.required_slices:
        samples = corpus.samples_for_slice(slice_id)
        decision_map = candidate_evaluations[slice_id].decisions
        for sample in samples:
            if not sample.catastrophic:
                continue
            opportunities_per_slice[slice_id] = (
                opportunities_per_slice.get(slice_id, 0) + 1
            )
            vote, accepted = decision_map.get(sample.sample_id, (None, False))
            if accepted:
                exposure += 1
                if vote != sample.label:
                    failures += 1
    opportunities_total = sum(opportunities_per_slice.values())
    if exposure > 0:
        upper = clopper_pearson_upper_95(failures, exposure)
        evaluable = True
        certifiable = failures == 0 and float(upper) <= contract.catastrophic_ceiling
    else:
        upper = None
        evaluable = False
        certifiable = False
    comparator_catastrophic = {
        policy_id: sum(
            comparisons[slice_id].baselines[policy_id].catastrophic_error_count
            for slice_id in contract.required_slices
        )
        for policy_id in (fixed_policy, best_policy)
    }
    catastrophic = CatastrophicAssessment(
        opportunities_total=opportunities_total,
        opportunities_per_slice=opportunities_per_slice,
        exposure_trials=exposure,
        observed_failures=failures,
        exposure_floor=contract.exposure_floor,
        ceiling=contract.catastrophic_ceiling,
        upper_bound_95=upper,
        evaluable=evaluable,
        certifiable=certifiable,
        comparator_catastrophic=comparator_catastrophic,
    )

    # 6. Frozen criteria, all recorded with numeric detail.
    matched, shifted = contract.required_slices
    u = slices[matched].utilities
    u_shift = slices[shifted].utilities
    candidate_utility = u[candidate_policy]
    fixed_capture = (
        u[fixed_policy] / candidate_utility if candidate_utility > 0 else None
    )
    criteria: list[CriterionResult] = []

    def criterion(name: str, passed: bool, detail: str) -> None:
        criteria.append(CriterionResult(name=name, passed=passed, detail=detail))

    criterion(
        "support_matched",
        slices[matched].sample_count >= contract.min_matched_samples,
        f"{slices[matched].sample_count} samples vs required "
        f"{contract.min_matched_samples}",
    )
    criterion(
        "support_shifted",
        slices[shifted].sample_count >= contract.min_shifted_samples,
        f"{slices[shifted].sample_count} samples vs required "
        f"{contract.min_shifted_samples}",
    )
    criterion(
        "support_catastrophic_opportunities",
        opportunities_total >= contract.min_catastrophic_opportunities,
        f"{opportunities_total} catastrophic-opportunity samples vs required "
        f"{contract.min_catastrophic_opportunities}",
    )
    criterion(
        "catastrophic_failures_zero",
        failures == 0,
        f"candidate accepted {failures} wrong catastrophic-opportunity samples",
    )
    criterion(
        "catastrophic_bound_certifiable",
        certifiable,
        f"exposure {exposure} trials (floor {contract.exposure_floor}), "
        f"upper_95 {upper!r} vs ceiling {contract.catastrophic_ceiling}",
    )
    candidate_cat = sum(
        comparisons[slice_id].baselines[candidate_policy].catastrophic_error_count
        for slice_id in contract.required_slices
    )
    criterion(
        "catastrophic_not_worse_than_comparators",
        candidate_cat
        <= min(comparator_catastrophic[fixed_policy], comparator_catastrophic[best_policy]),
        f"candidate catastrophic errors {candidate_cat} vs comparators "
        f"{comparator_catastrophic[fixed_policy]} (fixed rules) / "
        f"{comparator_catastrophic[best_policy]} (best single)",
    )
    criterion(
        "candidate_utility_positive",
        candidate_utility > 0,
        f"matched utility {candidate_utility!r}",
    )
    criterion(
        "beats_best_single",
        candidate_utility > u[best_policy],
        f"candidate {candidate_utility!r} vs best single {u[best_policy]!r} "
        "on matched slice",
    )
    material = fixed_capture is not None and fixed_capture < (
        contract.materiality_fixed_rule_capture_max
    )
    criterion(
        "material_gain_over_fixed_rules",
        material,
        f"fixed-rules capture {fixed_capture!r} of candidate utility vs "
        f"materiality bound {contract.materiality_fixed_rule_capture_max}",
    )
    criterion(
        "shift_not_worse",
        u_shift[candidate_policy] >= u_shift[fixed_policy]
        and u_shift[candidate_policy] >= u_shift[best_policy],
        f"shifted utility candidate {u_shift[candidate_policy]!r} vs fixed "
        f"{u_shift[fixed_policy]!r} / best single {u_shift[best_policy]!r}",
    )

    by_name = {item.name: item for item in criteria}

    # 7. Classification along the frozen ordering.
    reasons: list[str] = []
    details: list[str] = []
    support_failed = [
        item
        for item in (
            by_name["support_matched"],
            by_name["support_shifted"],
            by_name["support_catastrophic_opportunities"],
        )
        if not item.passed
    ]
    bound_failed = [
        item
        for item in (
            by_name["catastrophic_failures_zero"],
            by_name["catastrophic_bound_certifiable"],
        )
        if not item.passed
    ]
    if support_failed or bound_failed:
        outcome = DECISION_INSUFFICIENT_EVIDENCE
        if support_failed:
            reasons.append(REASON_SUPPORT_BELOW_FROZEN_FLOOR)
            details.extend(item.detail for item in support_failed)
        if by_name["catastrophic_failures_zero"].passed is False:
            reasons.append(REASON_CATASTROPHIC_ERRORS_OBSERVED)
            details.append(by_name["catastrophic_failures_zero"].detail)
        if not by_name["catastrophic_bound_certifiable"].passed:
            reasons.append(REASON_CATASTROPHIC_BOUND_UNCERTIFIABLE)
            details.append(by_name["catastrophic_bound_certifiable"].detail)
        details.append(
            "quantified next requirement: at least "
            f"{contract.min_catastrophic_opportunities} catastrophic-opportunity "
            f"samples and {contract.exposure_floor} zero-failure candidate "
            f"exposure trials before the {contract.catastrophic_ceiling} ceiling "
            "can be certified"
        )
    else:
        shadow_map = (
            ("candidate_utility_positive", REASON_CANDIDATE_UTILITY_NOT_POSITIVE),
            ("beats_best_single", REASON_CANDIDATE_LOSES_TO_BEST_SINGLE),
            ("material_gain_over_fixed_rules", REASON_GAIN_NOT_MATERIAL),
            ("shift_not_worse", REASON_SHIFT_INSTABILITY),
            (
                "catastrophic_not_worse_than_comparators",
                REASON_CATASTROPHIC_WORSE_THAN_COMPARATOR,
            ),
        )
        failed = [
            (by_name[name], reason) for name, reason in shadow_map if not by_name[name].passed
        ]
        if failed:
            outcome = DECISION_SHADOW
            for item, reason in failed:
                reasons.append(reason)
                details.append(item.detail)
        else:
            outcome = DECISION_PROMOTE
            reasons.append(REASON_ALL_CRITERIA_MET)
            details.append("every frozen criterion passed on the final holdout")

    return PromotionDecision(
        outcome=outcome,
        reasons=tuple(reasons),
        reason_details=tuple(details),
        evaluated_at=evaluated_at,
        contract_identity=contract.semantic_identity,
        actor_registry_identity=actors.semantic_identity,
        population_identity=corpus.semantic_identity,
        slices=slices,
        catastrophic=catastrophic,
        criteria=tuple(criteria),
        leakage=leakage,
        candidate_gate_status=gate_status,
        runtime_ms=runtime_ms,
    )
