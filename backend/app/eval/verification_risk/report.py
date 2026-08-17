"""Combined verification-risk report assembly."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .baselines import BaselineComparison, evaluate_baselines
from .calibration import CalibrationResult, evaluate_calibration
from .common import VERIFICATION_RISK_REPORT_SCHEMA_VERSION
from .identity import _identity
from .metrics import PairRiskMetrics, evaluate_pairs
from .models import VerificationRiskCorpus

@dataclass(frozen=True)
class VerificationRiskReport:
    """Combined deterministic report suitable for benchmark serialization."""

    corpus_identity: str
    slice_id: str | None
    pairs: Mapping[tuple[str, str], PairRiskMetrics]
    calibration: Mapping[str, CalibrationResult]
    baselines: BaselineComparison
    runtime_ms: float | None = None

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    def semantic_payload(self) -> dict[str, Any]:
        pair_entries = [
            self.pairs[key].as_dict() for key in sorted(self.pairs)
        ]
        return {
            "schema_version": VERIFICATION_RISK_REPORT_SCHEMA_VERSION,
            "corpus_identity": self.corpus_identity,
            "slice_id": self.slice_id,
            "pairs": pair_entries,
            "calibration": {
                key: self.calibration[key].as_dict() for key in sorted(self.calibration)
            },
            "baselines": self.baselines.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "runtime_ms": self.runtime_ms,
            "semantic_identity": self.semantic_identity,
        }

def evaluate_verification_risk(
    corpus: VerificationRiskCorpus,
    *,
    slice_id: str | None = None,
    calibration_witness_ids: Sequence[str] | None = None,
    min_calibration_samples: int = 5,
    runtime_ms: float | None = None,
) -> VerificationRiskReport:
    """Build pair, calibration, and five-baseline report for benchmark use."""

    calibration_ids = tuple(
        calibration_witness_ids
        if calibration_witness_ids is not None
        else sorted(witness.witness_id for witness in corpus.witnesses)
    )
    calibration = {
        witness_id: evaluate_calibration(
            corpus,
            witness_id,
            slice_id=slice_id,
            min_samples=min_calibration_samples,
        )
        for witness_id in calibration_ids
    }
    return VerificationRiskReport(
        corpus_identity=corpus.semantic_identity,
        slice_id=slice_id,
        pairs=evaluate_pairs(corpus, slice_id=slice_id),
        calibration=calibration,
        baselines=evaluate_baselines(corpus, slice_id=slice_id, runtime_ms=runtime_ms),
        runtime_ms=runtime_ms,
    )
