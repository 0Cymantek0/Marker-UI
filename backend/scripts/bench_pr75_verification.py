"""Benchmark PR75 verification-risk evaluation and write evidence JSON.

Run from repository root::

    python backend/scripts/bench_pr75_verification.py --write

The report keeps semantic results separate from wall-clock measurements.  A
runtime change must never change any ``semantic_identity`` value.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.eval.verification_risk import (  # noqa: E402
    BASELINE_NAMES,
    evaluate_baselines,
    evaluate_calibration,
    evaluate_pair,
    evaluate_verification_risk,
    load_verification_risk_corpus,
)


CORPUS_PATH = BACKEND / "conformance" / "fixtures" / "verification_risk_corpus_v1.json"
MEASUREMENTS_PATH = BACKEND.parent / "docs" / "reference" / "measurements" / "pr75-verification-risk.json"
SLICES = ("calibration-fit", "matched", "shifted", "insufficient")


def best_of(fn: Callable[[], Any], *, repeat: int) -> tuple[Any, float]:
    """Return result of one final call and best-of-N elapsed seconds."""

    if repeat < 1:
        raise ValueError("repeat must be positive")
    best = float("inf")
    result: Any = None
    for _ in range(repeat):
        started = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - started)
    return result, best


def _promotion(dependency: Any, slice_id: str) -> dict[str, Any]:
    threshold = {
        "min_support": 5,
        "max_false_verified_count": 0,
        "required_distribution": "matched",
        "required_dependency_status": "ok",
    }
    if (
        slice_id == "matched"
        and dependency.status == "ok"
        and dependency.sample_count >= threshold["min_support"]
        and dependency.false_verified_count <= threshold["max_false_verified_count"]
    ):
        decision = "promote"
    elif dependency.status in {"risk_bound_not_met", "insufficient_support"}:
        decision = "abstain"
    else:
        decision = "shadow"
    return {
        "threshold": threshold,
        "decision": decision,
        "reason": dependency.not_applicable_reason,
    }


def _rate(rate: Any) -> dict[str, Any]:
    return {
        "count": rate.count,
        "denominator": rate.denominator,
        "rate": rate.rate,
        "lower": rate.lower,
        "upper": rate.upper,
        "status": rate.status,
    }


def _baseline_summary(result: Any) -> dict[str, Any]:
    return {
        "name": result.name,
        "sample_count": result.sample_count,
        "selected_witnesses": list(result.selected_witnesses),
        "status": result.status,
        "accepted_count": result.accepted_count,
        "false_verified_count": result.false_verified_count,
        "catastrophic_error_count": result.catastrophic_error_count,
        "disagreement_count": result.disagreement_count,
        "coverage": _rate(result.coverage),
        "false_verified_fraction": _rate(result.false_verified_fraction),
        "abstention_rate": _rate(result.abstention_rate),
        "catastrophic_error_rate": _rate(result.catastrophic_error_rate),
        "disagreement_rate": _rate(result.disagreement_rate),
        "semantic_identity": result.semantic_identity,
    }


def _pair_summary(pair: Any) -> dict[str, Any]:
    return {
        "witnesses": [pair.witness_a, pair.witness_b],
        "slice_id": pair.slice_id,
        "sample_count": pair.sample_count,
        "marginal_error": {
            key: _rate(value) for key, value in sorted(pair.marginal_error.items())
        },
        "joint_error": _rate(pair.joint_error),
        "agreement": _rate(pair.agreement),
        "disagreement": _rate(pair.disagreement),
        "conditional_error_when_agree": _rate(pair.conditional_error_when_agree),
        "conditional_error_when_disagree": _rate(pair.conditional_error_when_disagree),
        "catastrophic_joint_failures": _rate(pair.catastrophic_joint_failures),
    }


def _calibration_summary(calibration: Any) -> dict[str, Any]:
    return {
        "witness_id": calibration.witness_id,
        "distribution": calibration.distribution,
        "method_id": calibration.method_id,
        "method_version": calibration.method_version,
        "sample_count": calibration.sample_count,
        "support_required": calibration.support_required,
        "support_sufficient": calibration.support_sufficient,
        "status": calibration.status,
        "accuracy": _rate(calibration.accuracy),
        "brier_score": calibration.brier_score,
        "expected_calibration_error": calibration.expected_calibration_error,
    }


def _slice_report(corpus: Any, slice_id: str, *, repeat: int) -> dict[str, Any]:
    report, report_seconds = best_of(
        lambda: evaluate_verification_risk(
            corpus,
            slice_id=slice_id,
            calibration_witness_ids=("model-a",),
            min_calibration_samples=5,
        ),
        repeat=repeat,
    )
    comparison, baseline_seconds = best_of(
        lambda: evaluate_baselines(corpus, slice_id=slice_id), repeat=repeat
    )
    pair, pair_seconds = best_of(
        lambda: evaluate_pair(corpus, "model-a", "model-b", slice_id=slice_id),
        repeat=repeat,
    )
    calibration, calibration_seconds = best_of(
        lambda: evaluate_calibration(
            corpus,
            "model-a",
            slice_id=slice_id,
            min_samples=5,
        ),
        repeat=repeat,
    )
    dependency = comparison.baselines[BASELINE_NAMES[4]]
    # Re-render with runtime metadata only after semantic values are measured.
    runtime_comparison = evaluate_baselines(
        corpus, slice_id=slice_id, runtime_ms=baseline_seconds * 1000
    )
    runtime_report = evaluate_verification_risk(
        corpus,
        slice_id=slice_id,
        calibration_witness_ids=("model-a",),
        min_calibration_samples=5,
        runtime_ms=report_seconds * 1000,
    )
    return {
        "slice_id": slice_id,
        "semantic_identity": report.semantic_identity,
        "report_semantic_identity": runtime_report.semantic_identity,
        "sample_count": report.baselines.baselines[BASELINE_NAMES[1]].sample_count,
        "pair": _pair_summary(pair),
        "calibration": _calibration_summary(calibration),
        "baselines": {
            name: _baseline_summary(runtime_comparison.baselines[name]) for name in BASELINE_NAMES
        },
        "promotion": _promotion(dependency, slice_id),
        "runtime_ms": {
            "report": report_seconds * 1000,
            "baselines": baseline_seconds * 1000,
            "pair": pair_seconds * 1000,
            "calibration": calibration_seconds * 1000,
        },
    }


def build_measurements(*, repeat: int = 5) -> dict[str, Any]:
    corpus = load_verification_risk_corpus(CORPUS_PATH)
    return {
        "benchmark": "pr75-verification-risk",
        "schema_version": "marker.verification_risk_measurements.v1",
        "corpus": {
            "path": "backend/conformance/fixtures/verification_risk_corpus_v1.json",
            "semantic_identity": corpus.semantic_identity,
        },
        "method": f"best-of-{repeat} wall milliseconds via time.perf_counter; runtime metadata is non-semantic",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "slices": {
            slice_id: _slice_report(corpus, slice_id, repeat=repeat) for slice_id in SLICES
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write measurement JSON")
    parser.add_argument("--repeat", type=int, default=5, help="best-of-N repetitions")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    args = parser.parse_args()
    measurements = build_measurements(repeat=args.repeat)
    encoded = json.dumps(measurements, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.write_text(encoded, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
