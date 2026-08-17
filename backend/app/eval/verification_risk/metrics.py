"""Pairwise risk metrics and deterministic rate estimates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .common import WILSON_Z_95, VerificationRiskError
from .models import VerificationRiskCorpus

@dataclass(frozen=True)
class RateEstimate:
    """Count/rate plus deterministic Wilson 95% interval.

    ``rate``, ``lower``, and ``upper`` are ``None`` when denominator is zero;
    no NaN-shaped uncertainty is emitted.
    """

    count: int
    denominator: int
    rate: float | None
    lower: float | None
    upper: float | None
    status: str = "defined"

    @classmethod
    def from_counts(cls, count: int, denominator: int) -> "RateEstimate":
        if count < 0 or denominator < 0 or count > denominator:
            raise VerificationRiskError(
                f"invalid rate counts count={count}, denominator={denominator}"
            )
        if denominator == 0:
            return cls(
                count=count,
                denominator=0,
                rate=None,
                lower=None,
                upper=None,
                status="undefined_zero_denominator",
            )
        proportion = count / denominator
        z = WILSON_Z_95
        z_squared = z * z
        centre = proportion + z_squared / (2 * denominator)
        scale = 1 + z_squared / denominator
        spread = z * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z_squared / (4 * denominator * denominator)
        )
        return cls(
            count=count,
            denominator=denominator,
            rate=proportion,
            lower=max(0.0, (centre - spread) / scale),
            upper=min(1.0, (centre + spread) / scale),
        )

    @property
    def wilson_95(self) -> tuple[float, float] | None:
        if self.lower is None or self.upper is None:
            return None
        return (self.lower, self.upper)

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "denominator": self.denominator,
            "rate": self.rate,
            "lower": self.lower,
            "upper": self.upper,
            "wilson_95": list(self.wilson_95) if self.wilson_95 is not None else None,
            "status": self.status,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def _rate(count: int, denominator: int) -> RateEstimate:
    return RateEstimate.from_counts(count, denominator)


@dataclass(frozen=True)
class PairRiskMetrics:
    witness_a: str
    witness_b: str
    slice_id: str | None
    sample_count: int
    marginal_error: Mapping[str, RateEstimate]
    joint_error: RateEstimate
    agreement: RateEstimate
    disagreement: RateEstimate
    conditional_error_when_agree: RateEstimate
    conditional_error_when_disagree: RateEstimate
    conditional_joint_error_when_disagree: RateEstimate
    per_witness_disagreement_accuracy: Mapping[str, RateEstimate]
    catastrophic_joint_failures: RateEstimate
    catastrophic_sample_count: int
    disagreement_case_count: int
    agreement_case_count: int

    @property
    def pair(self) -> tuple[str, str]:
        return (self.witness_a, self.witness_b)

    @property
    def marginal_errors(self) -> Mapping[str, RateEstimate]:
        return self.marginal_error

    @property
    def double_fault(self) -> RateEstimate:
        return self.joint_error

    @property
    def joint_error_rate(self) -> float | None:
        return self.joint_error.rate

    def as_dict(self) -> dict[str, Any]:
        marginal = {
            witness_id: result.as_dict()
            for witness_id, result in sorted(self.marginal_error.items())
        }
        disagreement_accuracy = {
            witness_id: result.as_dict()
            for witness_id, result in sorted(self.per_witness_disagreement_accuracy.items())
        }
        return {
            "pair": [self.witness_a, self.witness_b],
            "witness_a": self.witness_a,
            "witness_b": self.witness_b,
            "slice_id": self.slice_id,
            "sample_count": self.sample_count,
            "marginal_error": marginal,
            "marginal_errors": marginal,
            "joint_error": self.joint_error.as_dict(),
            "double_fault": self.joint_error.as_dict(),
            "agreement": self.agreement.as_dict(),
            "disagreement": self.disagreement.as_dict(),
            "conditional_error_when_agree": self.conditional_error_when_agree.as_dict(),
            "conditional_error_when_disagree": self.conditional_error_when_disagree.as_dict(),
            "conditional_joint_error_when_disagree": self.conditional_joint_error_when_disagree.as_dict(),
            "per_witness_disagreement_accuracy": disagreement_accuracy,
            "catastrophic_joint_failures": self.catastrophic_joint_failures.as_dict(),
            "catastrophic_joint_error": self.catastrophic_joint_failures.as_dict(),
            "catastrophic_sample_count": self.catastrophic_sample_count,
            "agreement_case_count": self.agreement_case_count,
            "disagreement_case_count": self.disagreement_case_count,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def _resolve_pair(
    witness_a: str | Sequence[str],
    witness_b: str | None,
) -> tuple[str, str]:
    if witness_b is None:
        if isinstance(witness_a, str) or len(witness_a) != 2:
            raise VerificationRiskError("pair must contain exactly two witness ids")
        first, second = (str(item).strip() for item in witness_a)
    else:
        if not isinstance(witness_a, str):
            raise VerificationRiskError("witness_a must be a witness id when witness_b is supplied")
        first, second = witness_a.strip(), witness_b.strip()
    if not first or not second or first == second:
        raise VerificationRiskError("pair requires two distinct non-empty witness ids")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


def evaluate_pair(
    corpus: VerificationRiskCorpus,
    witness_a: str | Sequence[str],
    witness_b: str | None = None,
    *,
    slice_id: str | None = None,
) -> PairRiskMetrics:
    """Evaluate exact pair statistics for one declared slice."""

    first, second = _resolve_pair(witness_a, witness_b)
    witness_ids = corpus.witness_by_id
    if first not in witness_ids or second not in witness_ids:
        missing = first if first not in witness_ids else second
        raise VerificationRiskError(f"unknown witness {missing!r}")
    samples = [
        sample
        for sample in corpus.samples_for_slice(slice_id)
        if first in sample.outcomes and second in sample.outcomes
    ]
    total = len(samples)
    first_errors = second_errors = joint_errors = 0
    agreement_count = disagreement_count = 0
    agreement_errors = disagreement_errors = disagreement_joint_errors = 0
    first_disagreement_correct = second_disagreement_correct = 0
    catastrophic_joint = catastrophic_samples = 0
    for sample in samples:
        outcome_a = sample.outcomes[first]
        outcome_b = sample.outcomes[second]
        first_error = outcome_a.is_error(sample.label)
        second_error = outcome_b.is_error(sample.label)
        if first_error:
            first_errors += 1
        if second_error:
            second_errors += 1
        if first_error and second_error:
            joint_errors += 1
        if outcome_a.prediction == outcome_b.prediction:
            agreement_count += 1
            if first_error or second_error:
                agreement_errors += 1
        else:
            disagreement_count += 1
            if first_error or second_error:
                disagreement_errors += 1
            if first_error and second_error:
                disagreement_joint_errors += 1
            if not first_error:
                first_disagreement_correct += 1
            if not second_error:
                second_disagreement_correct += 1
        if sample.catastrophic or outcome_a.catastrophic or outcome_b.catastrophic:
            catastrophic_samples += 1
            if first_error and second_error:
                catastrophic_joint += 1
    return PairRiskMetrics(
        witness_a=first,
        witness_b=second,
        slice_id=slice_id,
        sample_count=total,
        marginal_error={
            first: _rate(first_errors, total),
            second: _rate(second_errors, total),
        },
        joint_error=_rate(joint_errors, total),
        agreement=_rate(agreement_count, total),
        disagreement=_rate(disagreement_count, total),
        conditional_error_when_agree=_rate(agreement_errors, agreement_count),
        conditional_error_when_disagree=_rate(disagreement_errors, disagreement_count),
        conditional_joint_error_when_disagree=_rate(
            disagreement_joint_errors,
            disagreement_count,
        ),
        per_witness_disagreement_accuracy={
            first: _rate(first_disagreement_correct, disagreement_count),
            second: _rate(second_disagreement_correct, disagreement_count),
        },
        catastrophic_joint_failures=_rate(catastrophic_joint, catastrophic_samples),
        catastrophic_sample_count=catastrophic_samples,
        disagreement_case_count=disagreement_count,
        agreement_case_count=agreement_count,
    )


def evaluate_pairs(
    corpus: VerificationRiskCorpus,
    *,
    slice_id: str | None = None,
) -> dict[tuple[str, str], PairRiskMetrics]:
    """Evaluate every lexicographically ordered witness pair."""

    witness_ids = sorted(witness.witness_id for witness in corpus.witnesses)
    return {
        (first, second): evaluate_pair(corpus, first, second, slice_id=slice_id)
        for index, first in enumerate(witness_ids)
        for second in witness_ids[index + 1 :]
    }
