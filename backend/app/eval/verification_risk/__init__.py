"""Public verification-risk evaluator API.

Implementation split by concern; imports here preserve historical module paths.
"""

from __future__ import annotations

from typing import Any

from .applicability import (
    CALIBRATION_APPLICABILITY_SCHEMA_VERSION,
    ASSUMPTION_KEYS,
    RETEST_TRIGGERS,
    CalibrationApplicability,
    CatastrophicFailureInterpretation,
    build_applicability,
    clopper_pearson_upper_95,
)
from .baselines import (
    BASELINE_NAMES,
    BaselineComparison,
    BaselineResult,
    evaluate_all_baselines,
    evaluate_baselines,
)
from .calibration import (
    CalibrationResult,
    evaluate_calibration,
    evaluate_calibration_slices,
)
from .common import (
    VERIFICATION_RISK_CORPUS_SCHEMA_VERSION,
    VERIFICATION_RISK_REPORT_SCHEMA_VERSION,
    VerificationRiskError,
)
from .identity import _identity
from .loaders import load_corpus, load_verification_risk_corpus
from .metrics import (
    PairRiskMetrics,
    RateEstimate,
    evaluate_pair,
    evaluate_pairs,
)
from .models import (
    LabeledSample,
    VerificationRiskCorpus,
    WitnessOutcome,
    WitnessProfile,
)
from .report import VerificationRiskReport, evaluate_verification_risk

# Friendly aliases for benchmark callers and older naming conventions.
RiskWitness = WitnessProfile
RiskSample = LabeledSample
RiskCorpus = VerificationRiskCorpus
PairEvaluation = PairRiskMetrics
CalibrationEvaluation = CalibrationResult
BaselineReport = BaselineComparison
evaluate_risk_corpus = evaluate_verification_risk
run_verification_risk_benchmark = evaluate_verification_risk


def semantic_artifact_identity(value: Any) -> str:
    """Hash semantic content while excluding operational runtime metadata."""

    if isinstance(value, VerificationRiskCorpus):
        return value.semantic_identity
    if isinstance(value, BaselineResult):
        return value.semantic_identity
    if isinstance(value, VerificationRiskReport):
        return value.semantic_identity
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    return _identity(value)


__all__ = [
    "ASSUMPTION_KEYS",
    "BASELINE_NAMES",
    "CALIBRATION_APPLICABILITY_SCHEMA_VERSION",
    "BaselineComparison",
    "BaselineReport",
    "BaselineResult",
    "CalibrationApplicability",
    "CatastrophicFailureInterpretation",
    "CalibrationEvaluation",
    "CalibrationResult",
    "LabeledSample",
    "PairEvaluation",
    "PairRiskMetrics",
    "RateEstimate",
    "RETEST_TRIGGERS",
    "RiskCorpus",
    "RiskSample",
    "RiskWitness",
    "VERIFICATION_RISK_CORPUS_SCHEMA_VERSION",
    "VERIFICATION_RISK_REPORT_SCHEMA_VERSION",
    "VerificationRiskCorpus",
    "VerificationRiskError",
    "VerificationRiskReport",
    "WitnessOutcome",
    "WitnessProfile",
    "build_applicability",
    "clopper_pearson_upper_95",
    "evaluate_all_baselines",
    "evaluate_baselines",
    "evaluate_calibration",
    "evaluate_calibration_slices",
    "evaluate_pair",
    "evaluate_pairs",
    "evaluate_risk_corpus",
    "evaluate_verification_risk",
    "load_corpus",
    "load_verification_risk_corpus",
    "run_verification_risk_benchmark",
    "semantic_artifact_identity",
]
