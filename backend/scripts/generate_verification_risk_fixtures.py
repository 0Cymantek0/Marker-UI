"""Generate deterministic PR75 verification-risk conformance vectors.

Run from repository root::

    python backend/scripts/generate_verification_risk_fixtures.py --write

The generator imports the real evaluator and kernel record constructors.  It
never records wall-clock measurements in semantic vectors; benchmark runtime
belongs in ``docs/reference/measurements/pr75-verification-risk.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.eval.verification_risk import (  # noqa: E402
    BASELINE_NAMES,
    evaluate_baselines,
    evaluate_calibration,
    evaluate_pair,
    evaluate_verification_risk,
    load_verification_risk_corpus,
)
from app.kernel.verification_risk import (  # noqa: E402
    DependencyDisclosureRecord,
    VerificationRiskEvidenceRecord,
)
from app.utils.canonical import (  # noqa: E402
    CANONICALIZATION_PROFILE,
    record_identity_hash,
    to_json_ready,
)


CORPUS_PATH = BACKEND / "conformance" / "fixtures" / "verification_risk_corpus_v1.json"
VECTOR_PATH = BACKEND / "conformance" / "fixtures" / "verification_risk_vectors_v1.json"


def _identity(record: Any) -> str:
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


def _disclosure_payload() -> dict[str, Any]:
    return {
        "witness_ref": "model-a",
        "disclosure_quality": "complete",
        "architecture_family": "transformer",
        "base_model_family": "checkpoint-a",
        "training_sources": ["dataset-b", "dataset-a"],
        "teacher_lineage": ["teacher-v1"],
        "shared_dependency_refs": ["renderer-v1", "detector-v1"],
        "renderer_profile": "renderer-v1",
        "layout_profile": "layout-v1",
        "detector_profile": "detector-v1",
        "preprocessor_profile": "preprocess-v1",
        "postprocessor_profile": "normalize-v1",
        "prompt_template": "prompt-v1",
        "runtime_profile": "cpu-fp32",
        "quantization_profile": "none",
        "profile_version": "profile.v1",
        "metadata": {"owner": "fixture", "labels": ["b", "a"]},
    }


def _evidence_payload() -> dict[str, Any]:
    return {
        "policy": {"policy_id": "policy.high-risk", "revision": "rev-1"},
        "workflow_class": "high-risk.invoice-total.v1",
        "claim_authority_class": "source_native",
        "evaluation_slice_id": "matched",
        "witness_refs": ["model-b", "model-a"],
        "disclosure_refs": ["disclosure-b", "disclosure-a"],
        "sample_count": 100,
        "risk_upper_bound": "0.010",
        "risk_estimate": "0.004",
        "joint_error_rate": "0.002",
        "disagreement_rate": "0.100",
        "marginal_error_rates": {"model-b": "0.020", "model-a": "0.010"},
        "joint_error_rates": {"model-a|model-b": "0.002"},
        "evaluated_at": "2026-08-17T00:00:00Z",
        "expires_at": "2026-09-17T00:00:00Z",
        "shift_status": "matched",
        "dependency_status": "independent_looking",
        "evidence_kind": "source_native",
        "model_only": False,
        "consensus": False,
        "method_id": "wilson-upper-bound",
        "method_version": "marker.risk.wilson.v1",
        "metrics": {"support": 100},
        "metadata": {"runtime_profile": "cpu-fp32"},
    }


def _identity_case(
    case_id: str,
    record_class: str,
    payload: dict[str, Any],
    variants: list[dict[str, Any]],
    expectation: str,
) -> dict[str, Any]:
    constructor = {
        "dependency_disclosure": DependencyDisclosureRecord,
        "verification_risk_evidence": VerificationRiskEvidenceRecord,
    }[record_class]
    record = constructor.from_payload(payload, record_id="fixture-record")
    return {
        "id": case_id,
        "record_class": record_class,
        "payload": payload,
        "variants": variants,
        "variant_expectation": expectation,
        "expect": {"identity_hash": _identity(record)},
    }


def _summary_rate(rate: Any) -> dict[str, Any]:
    """Keep count/denominator/rate and explicit undefined status."""

    return {
        "count": rate.count,
        "denominator": rate.denominator,
        "rate": rate.rate,
        "status": rate.status,
    }


def _evaluation_case(corpus: Any, slice_id: str) -> dict[str, Any]:
    report = evaluate_verification_risk(
        corpus,
        slice_id=slice_id,
        calibration_witness_ids=("model-a",),
        min_calibration_samples=5,
    )
    comparison = evaluate_baselines(corpus, slice_id=slice_id)
    pair = evaluate_pair(corpus, "model-a", "model-b", slice_id=slice_id)
    calibration = evaluate_calibration(
        corpus,
        "model-a",
        slice_id=slice_id,
        min_samples=5,
    )
    dependency = comparison.baselines[BASELINE_NAMES[4]]
    # Promotion is intentionally stricter than report acceptance.  A policy
    # must have matched support, no observed false verification, and an
    # ``ok`` dependency gate.  This fixture demonstrates shadow-only status.
    promotion = {
        "threshold": {
            "min_support": 5,
            "max_false_verified_count": 0,
            "required_distribution": "matched",
            "required_dependency_status": "ok",
        },
        "decision": (
            "promote"
            if (
                slice_id == "matched"
                and dependency.status == "ok"
                and dependency.sample_count >= 5
                and dependency.false_verified_count == 0
            )
            else ("abstain" if dependency.status in {"risk_bound_not_met", "insufficient_support"} else "shadow")
        ),
        "reason": dependency.not_applicable_reason,
    }
    return {
        "slice_id": slice_id,
        "sample_count": report.baselines.baselines[BASELINE_NAMES[1]].sample_count,
        "pair": {
            "witnesses": [pair.witness_a, pair.witness_b],
            "sample_count": pair.sample_count,
            "marginal_error": {
                key: _summary_rate(value) for key, value in sorted(pair.marginal_error.items())
            },
            "joint_error": _summary_rate(pair.joint_error),
            "agreement": _summary_rate(pair.agreement),
            "disagreement": _summary_rate(pair.disagreement),
            "conditional_error_when_agree": _summary_rate(pair.conditional_error_when_agree),
            "conditional_error_when_disagree": _summary_rate(pair.conditional_error_when_disagree),
            "catastrophic_joint_failures": _summary_rate(pair.catastrophic_joint_failures),
        },
        "calibration": {
            "witness_id": calibration.witness_id,
            "distribution": calibration.distribution,
            "method_id": calibration.method_id,
            "method_version": calibration.method_version,
            "sample_count": calibration.sample_count,
            "support_required": calibration.support_required,
            "support_sufficient": calibration.support_sufficient,
            "status": calibration.status,
            "accuracy": _summary_rate(calibration.accuracy),
            "brier_score": calibration.brier_score,
            "expected_calibration_error": calibration.expected_calibration_error,
        },
        "baseline_order": list(BASELINE_NAMES),
        "baselines": {
            name: {
                "sample_count": comparison.baselines[name].sample_count,
                "accepted_count": comparison.baselines[name].accepted_count,
                "false_verified_count": comparison.baselines[name].false_verified_count,
                "catastrophic_error_count": comparison.baselines[name].catastrophic_error_count,
                "disagreement_count": comparison.baselines[name].disagreement_count,
                "status": comparison.baselines[name].status,
                "selected_witnesses": list(comparison.baselines[name].selected_witnesses),
                "semantic_identity": comparison.baselines[name].semantic_identity,
            }
            for name in BASELINE_NAMES
        },
        "promotion": promotion,
        "semantic_identity": report.semantic_identity,
    }


def build_vectors() -> dict[str, Any]:
    corpus = load_verification_risk_corpus(CORPUS_PATH)
    disclosure = _disclosure_payload()
    evidence = _evidence_payload()
    return {
        "$schema": "marker.verification_risk_vectors.v1",
        "description": (
            "PR75 canonical identity, pair/calibration, baseline, and "
            "conservative promotion vectors. Runtime is deliberately absent."
        ),
        "canonicalization_profile": CANONICALIZATION_PROFILE,
        "corpus": {
            "path": "backend/conformance/fixtures/verification_risk_corpus_v1.json",
            "semantic_identity": corpus.semantic_identity,
        },
        "identity_cases": [
            _identity_case(
                "disclosure-set-order",
                "dependency_disclosure",
                disclosure,
                [
                    {
                        "id": "reordered-set-members",
                        "payload": {
                            **disclosure,
                            "training_sources": ["dataset-a", "dataset-b"],
                            "shared_dependency_refs": ["detector-v1", "renderer-v1"],
                            "metadata": {"labels": ["b", "a"], "owner": "fixture"},
                        },
                    }
                ],
                "same",
            ),
            _identity_case(
                "disclosure-material-profile",
                "dependency_disclosure",
                disclosure,
                [
                    {
                        "id": "runtime-profile-changed",
                        "payload": {**disclosure, "runtime_profile": "cuda-fp16"},
                    }
                ],
                "different",
            ),
            _identity_case(
                "evidence-set-order",
                "verification_risk_evidence",
                evidence,
                [
                    {
                        "id": "refs-reordered",
                        "payload": {
                            **evidence,
                            "witness_refs": ["model-a", "model-b"],
                            "disclosure_refs": ["disclosure-a", "disclosure-b"],
                        },
                    }
                ],
                "same",
            ),
            _identity_case(
                "evidence-policy-revision",
                "verification_risk_evidence",
                evidence,
                [
                    {
                        "id": "policy-revision-changed",
                        "payload": {
                            **evidence,
                            "policy": {"policy_id": "policy.high-risk", "revision": "rev-2"},
                        },
                    }
                ],
                "different",
            ),
        ],
        "rematerialization_cases": [
            {
                "id": "disclosure-round-trip",
                "record_class": "dependency_disclosure",
                "payload": disclosure,
                "expect": {"identity_hash": _identity(DependencyDisclosureRecord.from_payload(disclosure, record_id="round-trip"))},
            },
            {
                "id": "evidence-round-trip",
                "record_class": "verification_risk_evidence",
                "payload": evidence,
                "expect": {"identity_hash": _identity(VerificationRiskEvidenceRecord.from_payload(evidence, record_id="round-trip"))},
            },
        ],
        "evaluation_cases": [
            _evaluation_case(corpus, slice_id)
            for slice_id in (
                "calibration-fit",
                "matched",
                "shifted",
                "insufficient",
            )
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write vectors to the fixture path")
    parser.add_argument("--output", type=Path, default=VECTOR_PATH)
    args = parser.parse_args()
    vectors = build_vectors()
    encoded = json.dumps(vectors, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.write_text(encoded, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
