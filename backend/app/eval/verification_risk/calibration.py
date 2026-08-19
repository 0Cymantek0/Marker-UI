"""Confidence calibration metrics over declared corpus slices."""

from __future__ import annotations

import math

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .common import VerificationRiskError
from .metrics import RateEstimate, _rate
from .models import VerificationRiskCorpus

@dataclass(frozen=True)
class CalibrationResult:
    witness_id: str
    corpus_identity: str
    slice_id: str | None
    distribution: str | None
    method_id: str
    method_version: str
    target_event: str
    split_definition: Mapping[str, Any]
    support_uncertainty_method: str
    sample_count: int
    missing_confidence_count: int
    support_required: int
    support_sufficient: bool
    status: str
    brier_score: float | None
    expected_calibration_error: float | None
    maximum_calibration_error: float | None
    accuracy: RateEstimate
    bins: tuple[Mapping[str, Any], ...] = ()

    @property
    def ece(self) -> float | None:
        return self.expected_calibration_error

    @property
    def metric(self) -> str:
        return "brier_score_and_expected_calibration_error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "witness_id": self.witness_id,
            "corpus_identity": self.corpus_identity,
            "slice_id": self.slice_id,
            "distribution": self.distribution,
            "method_id": self.method_id,
            "method_version": self.method_version,
            "target_event": self.target_event,
            "split_definition": dict(self.split_definition),
            "support_uncertainty_method": self.support_uncertainty_method,
            "sample_count": self.sample_count,
            "missing_confidence_count": self.missing_confidence_count,
            "support_required": self.support_required,
            "support_sufficient": self.support_sufficient,
            "status": self.status,
            "metric": self.metric,
            "brier_score": self.brier_score,
            "expected_calibration_error": self.expected_calibration_error,
            "ece": self.ece,
            "maximum_calibration_error": self.maximum_calibration_error,
            "accuracy": self.accuracy.as_dict(),
            "support_uncertainty": self.accuracy.as_dict(),
            "bins": [dict(item) for item in self.bins],
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def evaluate_calibration(
    corpus: VerificationRiskCorpus,
    witness_id: str,
    *,
    slice_id: str | None = None,
    distribution: str | None = None,
    min_samples: int = 5,
    bin_count: int = 10,
) -> CalibrationResult:
    """Calculate deterministic Brier/ECE calibration on one scoped slice."""

    if min_samples < 1:
        raise VerificationRiskError("min_samples must be positive")
    if bin_count < 1:
        raise VerificationRiskError("bin_count must be positive")
    if witness_id not in corpus.witness_by_id:
        raise VerificationRiskError(f"unknown witness {witness_id!r}")
    selected = list(corpus.samples_for_slice(slice_id))
    if distribution is not None:
        selected = [sample for sample in selected if sample.distribution == distribution]
    scoped = [sample for sample in selected if witness_id in sample.outcomes]
    scored = [sample for sample in scoped if sample.outcomes[witness_id].confidence is not None]
    missing = len(scoped) - len(scored)
    support = len(scored)
    support_sufficient = support >= min_samples
    if support == 0:
        return CalibrationResult(
            witness_id=witness_id,
            corpus_identity=corpus.semantic_identity,
            slice_id=slice_id,
            distribution=distribution,
            method_id="equal_width_ece_and_brier",
            method_version="marker.calibration.ece_brier.v1",
            target_event="witness_prediction_correct",
            split_definition=dict(corpus.metadata.get("calibration_split", {})),
            support_uncertainty_method="wilson_score_95_accuracy",
            sample_count=0,
            missing_confidence_count=missing,
            support_required=min_samples,
            support_sufficient=False,
            status="insufficient_support" if missing or not support_sufficient else "ok",
            brier_score=None,
            expected_calibration_error=None,
            maximum_calibration_error=None,
            accuracy=_rate(0, 0),
            bins=(),
        )
    correct_values = [
        0 if sample.outcomes[witness_id].is_error(sample.label) else 1 for sample in scored
    ]
    confidence_values = [
        sample.outcomes[witness_id].confidence for sample in scored
    ]
    # Type narrowing for confidence after filtering above.
    confidence_numbers = [float(value) for value in confidence_values if value is not None]
    brier = math.fsum(
        (confidence - correct) ** 2
        for confidence, correct in zip(confidence_numbers, correct_values, strict=True)
    ) / support
    bins: list[dict[str, Any]] = []
    weighted_gaps: list[float] = []
    max_gap = 0.0
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        members = [
            position
            for position, confidence in enumerate(confidence_numbers)
            if (lower <= confidence < upper) or (index == bin_count - 1 and confidence == upper)
        ]
        if not members:
            continue
        mean_confidence = math.fsum(confidence_numbers[position] for position in members) / len(members)
        empirical_accuracy = sum(correct_values[position] for position in members) / len(members)
        gap = abs(mean_confidence - empirical_accuracy)
        weighted_gaps.append(len(members) / support * gap)
        max_gap = max(max_gap, gap)
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(members),
                "mean_confidence": mean_confidence,
                "empirical_accuracy": empirical_accuracy,
                "gap": gap,
            }
        )
    return CalibrationResult(
        witness_id=witness_id,
        corpus_identity=corpus.semantic_identity,
        slice_id=slice_id,
        distribution=distribution,
        method_id="equal_width_ece_and_brier",
        method_version="marker.calibration.ece_brier.v1",
        target_event="witness_prediction_correct",
        split_definition=dict(corpus.metadata.get("calibration_split", {})),
        support_uncertainty_method="wilson_score_95_accuracy",
        sample_count=support,
        missing_confidence_count=missing,
        support_required=min_samples,
        support_sufficient=support_sufficient,
        status="ok" if support_sufficient else "insufficient_support",
        brier_score=brier,
        expected_calibration_error=math.fsum(weighted_gaps),
        maximum_calibration_error=max_gap,
        accuracy=_rate(sum(correct_values), support),
        bins=tuple(bins),
    )


def evaluate_calibration_slices(
    corpus: VerificationRiskCorpus,
    witness_id: str,
    *,
    slices: Sequence[str] = ("matched", "shifted", "insufficient"),
    min_samples: int = 5,
    bin_count: int = 10,
) -> dict[str, CalibrationResult]:
    """Evaluate named matched/shifted/insufficient slices independently."""

    return {
        slice_name: evaluate_calibration(
            corpus,
            witness_id,
            distribution=slice_name,
            min_samples=min_samples,
            bin_count=bin_count,
        )
        for slice_name in slices
    }
