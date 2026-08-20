"""Baseline policies and dependency-aware acceptance gates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .common import VERIFICATION_RISK_REPORT_SCHEMA_VERSION, VerificationRiskError
from .identity import _canonical_json, _identity
from .metrics import RateEstimate, _rate, evaluate_pair
from .models import (
    _DEPENDENCY_DIMENSIONS,
    LabeledSample,
    VerificationRiskCorpus,
    WitnessProfile,
)


def _witness_group_keys(witness: WitnessProfile) -> tuple[tuple[Any, ...], ...]:
    """Every dependency dimension a witness occupies.

    Two witnesses correlate when their key sets INTERSECT. Sharing a
    renderer, cropper, detector or model family must dedupe exactly
    like sharing a teacher — a differing base lineage can no longer
    mask a shared pipeline stage (on the PR75 corpus this masked
    model-b/model-c sharing renderer/cropper/detector).
    """
    keys: list[tuple[Any, ...]] = []
    if witness.shared_dependency_group:
        keys.append(("shared", witness.shared_dependency_group))
    if witness.teacher_lineage:
        keys.append(("teacher", witness.teacher_lineage))
    if witness.base_lineage:
        keys.append(("lineage", witness.base_lineage))
    for name in _DEPENDENCY_DIMENSIONS:
        value = getattr(witness, name)
        if value is not None:
            keys.append(("dim", name, value))
    return tuple(keys)


def _dependency_aware_ids(corpus: VerificationRiskCorpus) -> tuple[str, ...]:
    selected: list[WitnessProfile] = []
    seen_groups: set[tuple[Any, ...]] = set()
    for witness in sorted(corpus.witnesses, key=lambda item: item.witness_id):
        if witness.disclosure != "complete" or witness.alias_of:
            continue
        if not witness.has_known_lineage:
            continue
        # Unknown dependency key cannot prove diversity; retain only when all
        # known fields establish a distinct complete lineage.
        keys = _witness_group_keys(witness)
        if any(key in seen_groups for key in keys):
            continue
        seen_groups.update(keys)
        selected.append(witness)
    return tuple(witness.witness_id for witness in selected)

@dataclass(frozen=True)
class BaselineResult:
    name: str
    slice_id: str | None
    sample_count: int
    evaluated_sample_ids: tuple[str, ...]
    selected_witnesses: tuple[str, ...]
    status: str
    not_applicable_reason: str | None
    accepted_count: int
    false_verified_count: int
    catastrophic_error_count: int
    disagreement_count: int
    coverage: RateEstimate
    false_verified_rate: RateEstimate
    false_verified_fraction: RateEstimate
    abstention_rate: RateEstimate
    catastrophic_error_rate: RateEstimate
    disagreement_rate: RateEstimate
    brier_score: float | None
    runtime_ms: float | None = None

    @property
    def accepted_fraction(self) -> RateEstimate:
        return self.coverage

    @property
    def abstention(self) -> RateEstimate:
        return self.abstention_rate

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slice_id": self.slice_id,
            "sample_count": self.sample_count,
            "evaluated_sample_ids": list(self.evaluated_sample_ids),
            "selected_witnesses": list(self.selected_witnesses),
            "status": self.status,
            "not_applicable_reason": self.not_applicable_reason,
            "accepted_count": self.accepted_count,
            "false_verified_count": self.false_verified_count,
            "catastrophic_error_count": self.catastrophic_error_count,
            "disagreement_count": self.disagreement_count,
            "coverage": self.coverage.as_dict(),
            "false_verified_rate": self.false_verified_rate.as_dict(),
            "false_verified_fraction": self.false_verified_fraction.as_dict(),
            "abstention_rate": self.abstention_rate.as_dict(),
            "catastrophic_error_rate": self.catastrophic_error_rate.as_dict(),
            "disagreement_rate": self.disagreement_rate.as_dict(),
            "brier_score": self.brier_score,
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "runtime_ms": self.runtime_ms,
            "semantic_identity": self.semantic_identity,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


BASELINE_NAMES: tuple[str, ...] = (
    "deterministic_source_native_only",
    "best_single_witness",
    "naive_majority_vote",
    "correlation_blind_ensemble",
    "dependency_aware_policy",
)


@dataclass(frozen=True)
class BaselineComparison:
    """Five baseline results, evaluated over same sample slice."""

    corpus_identity: str
    slice_id: str | None
    baselines: Mapping[str, BaselineResult]
    baseline_order: tuple[str, ...] = BASELINE_NAMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VERIFICATION_RISK_REPORT_SCHEMA_VERSION,
            "corpus_identity": self.corpus_identity,
            "slice_id": self.slice_id,
            "baseline_order": list(self.baseline_order),
            "baselines": {
                name: self.baselines[name].as_dict() for name in self.baseline_order
            },
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

def _sample_prediction(
    sample: LabeledSample,
    witness_ids: Sequence[str],
) -> tuple[Any | None, bool]:
    """Return vote and whether it is accepted by a deterministic vote rule."""

    votes = [sample.outcomes[item].prediction for item in witness_ids if item in sample.outcomes]
    if not votes:
        return None, False
    counts: dict[str, tuple[Any, int]] = {}
    for vote in votes:
        # JSON labels are expected; repr fallback keeps unusual hashability out
        # of the public contract while preserving deterministic tie ordering.
        key = _canonical_json(vote)
        value, count = counts.get(key, (vote, 0))
        counts[key] = (value, count + 1)
    ordered = sorted(counts.values(), key=lambda item: (-item[1], _canonical_json(item[0])))
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return None, False
    return ordered[0][0], True


def _confidence_weighted_prediction(
    sample: LabeledSample,
    witness_ids: Sequence[str],
) -> tuple[Any | None, bool]:
    """Return confidence-weighted vote without consulting dependency metadata."""

    weights: dict[str, tuple[Any, float]] = {}
    for witness_id in witness_ids:
        outcome = sample.outcomes.get(witness_id)
        if outcome is None:
            continue
        key = _canonical_json(outcome.prediction)
        prediction, total = weights.get(key, (outcome.prediction, 0.0))
        weight = outcome.confidence if outcome.confidence is not None else 1.0
        weights[key] = (prediction, total + weight)
    if not weights:
        return None, False
    ordered = sorted(weights.values(), key=lambda item: (-item[1], _canonical_json(item[0])))
    if len(ordered) > 1 and math.isclose(ordered[0][1], ordered[1][1], rel_tol=0.0, abs_tol=1e-12):
        return None, False
    return ordered[0][0], True


def _baseline_result(
    name: str,
    samples: Sequence[LabeledSample],
    selected_witnesses: Sequence[str],
    decisions: Mapping[str, tuple[Any | None, bool]],
    *,
    status: str = "ok",
    not_applicable_reason: str | None = None,
    runtime_ms: float | None = None,
) -> BaselineResult:
    sample_count = len(samples)
    accepted_count = false_verified = catastrophic_error = disagreement_count = 0
    brier_values: list[float] = []
    for sample in samples:
        vote, accepted = decisions.get(sample.sample_id, (None, False))
        witness_votes = [
            sample.outcomes[item].prediction
            for item in selected_witnesses
            if item in sample.outcomes
        ]
        if len(witness_votes) > 1 and len({_canonical_json(value) for value in witness_votes}) > 1:
            disagreement_count += 1
        if accepted:
            accepted_count += 1
            error = vote != sample.label
            if error:
                false_verified += 1
                if sample.catastrophic:
                    catastrophic_error += 1
        if accepted and selected_witnesses:
            confidences = [
                sample.outcomes[item].confidence
                for item in selected_witnesses
                if item in sample.outcomes and sample.outcomes[item].confidence is not None
            ]
            if confidences:
                brier_values.append(
                    (math.fsum(confidences) / len(confidences) - int(vote == sample.label)) ** 2
                )
    brier = math.fsum(brier_values) / len(brier_values) if brier_values else None
    return BaselineResult(
        name=name,
        slice_id=None,
        sample_count=sample_count,
        evaluated_sample_ids=tuple(sample.sample_id for sample in samples),
        selected_witnesses=tuple(selected_witnesses),
        status=status,
        not_applicable_reason=not_applicable_reason,
        accepted_count=accepted_count,
        false_verified_count=false_verified,
        catastrophic_error_count=catastrophic_error,
        disagreement_count=disagreement_count,
        coverage=_rate(accepted_count, sample_count),
        false_verified_rate=_rate(false_verified, accepted_count),
        false_verified_fraction=_rate(false_verified, sample_count),
        abstention_rate=_rate(sample_count - accepted_count, sample_count),
        catastrophic_error_rate=_rate(catastrophic_error, accepted_count),
        disagreement_rate=_rate(disagreement_count, sample_count),
        brier_score=brier,
        runtime_ms=runtime_ms,
    )


def _source_native_ids(corpus: VerificationRiskCorpus) -> tuple[str, ...]:
    return tuple(
        sorted(
            witness.witness_id
            for witness in corpus.witnesses
            if witness.source_native or witness.kind in {"source_native", "deterministic"}
        )
    )


def _dependency_aware_decisions(
    corpus: VerificationRiskCorpus,
    samples: Sequence[LabeledSample],
    selected: Sequence[str],
    *,
    empirical_gate_passed: bool,
) -> dict[str, tuple[Any | None, bool]]:
    source_native = set(_source_native_ids(corpus))
    decisions: dict[str, tuple[Any | None, bool]] = {}
    for sample in samples:
        if not empirical_gate_passed:
            decisions[sample.sample_id] = (None, False)
            continue
        # High-risk model consensus alone can never become verified.  Authority
        # bearing source-native/human/deterministic evidence is separate.
        authority_present = any(
            witness_id in sample.outcomes for witness_id in set(selected) & source_native
        )
        if sample.risk_level == "high" and not authority_present:
            decisions[sample.sample_id] = (None, False)
            continue
        vote, accepted = _sample_prediction(sample, selected)
        if len(selected) < 2 and not (set(selected) & source_native):
            accepted = False
        decisions[sample.sample_id] = (vote, accepted)
    return decisions


def _dependency_empirical_gate(
    corpus: VerificationRiskCorpus,
    selected: Sequence[str],
    *,
    slice_id: str | None,
    min_samples: int = 5,
    max_joint_error_upper: float = 0.6,
) -> tuple[bool, str, str | None]:
    """Require measured pair support and bounded double-fault uncertainty."""

    if len(selected) < 2:
        return False, "insufficient_support", "fewer than two dependency-diverse witnesses"
    pair_metrics = [
        evaluate_pair(corpus, first, second, slice_id=slice_id)
        for index, first in enumerate(selected)
        for second in selected[index + 1 :]
    ]
    least_supported = min(pair_metrics, key=lambda metrics: metrics.sample_count)
    if least_supported.sample_count < min_samples:
        return (
            False,
            "insufficient_support",
            f"pair {least_supported.pair!r} support {least_supported.sample_count} "
            f"is below required {min_samples}",
        )
    worst = max(
        pair_metrics,
        key=lambda metrics: metrics.joint_error.upper
        if metrics.joint_error.upper is not None
        else float("inf"),
    )
    upper = worst.joint_error.upper
    if upper is None or upper > max_joint_error_upper:
        return (
            False,
            "risk_bound_not_met",
            f"pair {worst.pair!r} joint-error Wilson upper {upper!r} "
            f"exceeds policy bound {max_joint_error_upper}",
        )
    return True, "ok", None


def evaluate_baselines(
    corpus: VerificationRiskCorpus,
    *,
    slice_id: str | None = None,
    best_single_witness_id: str | None = None,
    runtime_ms: float | None = None,
) -> BaselineComparison:
    """Compare all five required policies over identical labeled samples."""

    samples = corpus.samples_for_slice(slice_id)
    witness_ids = tuple(sorted(witness.witness_id for witness in corpus.witnesses))
    declared_source_native = _source_native_ids(corpus)
    source_native = tuple(
        witness_id
        for witness_id in declared_source_native
        if any(witness_id in sample.outcomes for sample in samples)
    )
    source_decisions = {
        sample.sample_id: _sample_prediction(sample, source_native) for sample in samples
    }
    source_result = _baseline_result(
        BASELINE_NAMES[0],
        samples,
        source_native,
        source_decisions,
        status="ok" if source_native else "not_applicable",
        not_applicable_reason=None
        if source_native
        else "corpus has no source-native/deterministic witness",
        runtime_ms=runtime_ms,
    )

    declared_best = best_single_witness_id or corpus.metadata.get("baseline_best_single_witness")
    if not isinstance(declared_best, str) or not declared_best.strip():
        raise VerificationRiskError(
            "best-single baseline requires corpus metadata baseline_best_single_witness "
            "or explicit best_single_witness_id"
        )
    declared_best = declared_best.strip()
    if declared_best not in corpus.witness_by_id:
        raise VerificationRiskError(f"declared best-single witness {declared_best!r} is unknown")
    best_decisions = {
        sample.sample_id: (
            (sample.outcomes[declared_best].prediction, True)
            if declared_best in sample.outcomes
            else (None, False)
        )
        for sample in samples
    }
    best_single = _baseline_result(
        BASELINE_NAMES[1],
        samples,
        (declared_best,),
        best_decisions,
        status="ok"
        if any(declared_best in sample.outcomes for sample in samples)
        else "not_applicable",
        not_applicable_reason=None
        if any(declared_best in sample.outcomes for sample in samples)
        else f"declared witness {declared_best!r} has no outcomes on selected slice",
        runtime_ms=runtime_ms,
    )

    naive_decisions = {
        sample.sample_id: _sample_prediction(sample, witness_ids) for sample in samples
    }
    naive = _baseline_result(
        BASELINE_NAMES[2],
        samples,
        witness_ids,
        naive_decisions,
        runtime_ms=runtime_ms,
    )

    # Correlation-blind ensemble weights votes by source confidence while
    # intentionally ignoring dependency disclosures.  This is distinct from
    # the unweighted naive majority baseline.
    ensemble_decisions = {
        sample.sample_id: (
            _confidence_weighted_prediction(sample, witness_ids)[0],
            len(
                [item for item in witness_ids if item in sample.outcomes]
            ) >= 2
            and _confidence_weighted_prediction(sample, witness_ids)[1],
        )
        for sample in samples
    }
    ensemble = _baseline_result(
        BASELINE_NAMES[3],
        samples,
        witness_ids,
        ensemble_decisions,
        runtime_ms=runtime_ms,
    )

    dependency_ids = tuple(
        witness_id
        for witness_id in _dependency_aware_ids(corpus)
        if any(witness_id in sample.outcomes for sample in samples)
    )
    empirical_passed, dependency_status, dependency_reason = _dependency_empirical_gate(
        corpus,
        dependency_ids,
        slice_id=slice_id,
    )
    dependency_decisions = _dependency_aware_decisions(
        corpus,
        samples,
        dependency_ids,
        empirical_gate_passed=empirical_passed,
    )
    dependency = _baseline_result(
        BASELINE_NAMES[4],
        samples,
        dependency_ids,
        dependency_decisions,
        status=dependency_status,
        not_applicable_reason=dependency_reason,
        runtime_ms=runtime_ms,
    )
    results = {
        BASELINE_NAMES[0]: source_result,
        BASELINE_NAMES[1]: best_single,
        BASELINE_NAMES[2]: naive,
        BASELINE_NAMES[3]: ensemble,
        BASELINE_NAMES[4]: dependency,
    }
    # Dataclass stores requested slice for every result; preserve semantic
    # identity independent of runtime measurement.
    results = {
        name: BaselineResult(**{**result.__dict__, "slice_id": slice_id})
        for name, result in results.items()
    }
    return BaselineComparison(
        corpus_identity=corpus.semantic_identity,
        slice_id=slice_id,
        baselines=results,
    )


def evaluate_all_baselines(
    corpus: VerificationRiskCorpus,
    *,
    runtime_ms: float | None = None,
) -> dict[str | None, BaselineComparison]:
    """Return one five-baseline comparison for all and each corpus slice."""

    return {
        None: evaluate_baselines(corpus, runtime_ms=runtime_ms),
        **{
            slice_name: evaluate_baselines(corpus, slice_id=slice_name, runtime_ms=runtime_ms)
            for slice_name in corpus.slice_ids
        },
    }
