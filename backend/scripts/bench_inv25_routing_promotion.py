"""Benchmark the invariant 25 held-out routing-promotion gate and write evidence JSON.

Run from repository root::

    python backend/scripts/bench_inv25_routing_promotion.py --write

The evaluation runs twice from identical semantic inputs and refuses to
write the artifact unless both runs agree on the semantic decision
identity; wall-clock runtime is recorded but never participates in any
identity.  ``--evaluated-at`` defaults to the documented first final
evaluation timestamp so artifact regeneration is byte-stable.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.eval.routing_promotion import (  # noqa: E402
    ACTOR_REGISTRY_V1,
    ROUTING_PROMOTION_CONTRACT,
    build_final_holdout_corpus,
    evaluate_promotion,
)

REPO_ROOT = BACKEND.parent
MEASUREMENTS_PATH = REPO_ROOT / "docs" / "reference" / "measurements" / "inv25-routing-promotion-gate.json"
DEFAULT_EVALUATED_AT = "2026-08-26T12:00:00+00:00"

LIMITATIONS = [
    "Single-session procedurally generated population (39 matched, 25 shifted, "
    "3 thin samples): support sits below the frozen promotion floors, so the "
    "correct decision is insufficient_evidence, not a production claim.",
    "The paired comparison itself is recorded (candidate beats both required "
    "comparators on matched utility and catastrophic counts under this "
    "population) but cannot certify the 0.10 catastrophic ceiling at 12 "
    "zero-failure exposure trials; 29 opportunities and 29 exposure trials "
    "are the quantified next requirement.",
    "Evidence expires and must be regenerated whenever the population, the "
    "frozen contract, the actor registry, or the dependency-aware policy "
    "changes; the decision identity binds all four.",
    "The candidate policy remains shadow/offline: this artifact grants no "
    "serving authority (see the offline-containment test).",
]

REPRODUCE = {
    "regenerate": "python backend/scripts/bench_inv25_routing_promotion.py --write",
    "focused_tests": (
        "python -X utf8 -m pytest backend/tests/test_eval_routing_promotion_contract.py "
        "backend/tests/test_eval_routing_promotion_actors.py "
        "backend/tests/test_eval_routing_promotion_population.py "
        "backend/tests/test_eval_routing_promotion_decision.py -q"
    ),
    "conformance": (
        "python -X utf8 -m pytest backend/conformance/test_routing_promotion_conformance.py -q"
    ),
}


def _git_head() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"unavailable: {exc}"


def build_measurements(*, evaluated_at: str) -> dict[str, Any]:
    corpus = build_final_holdout_corpus()

    started = time.perf_counter()
    first = evaluate_promotion(corpus, evaluated_at=evaluated_at)
    elapsed_first = time.perf_counter() - started

    started = time.perf_counter()
    second = evaluate_promotion(corpus, evaluated_at=evaluated_at)
    elapsed_second = time.perf_counter() - started

    if first.semantic_identity != second.semantic_identity:
        raise SystemExit(
            "reproducibility violation: repeated evaluation produced different "
            f"semantic identities {first.semantic_identity} vs {second.semantic_identity}"
        )

    runtime_decision = evaluate_promotion(
        corpus, evaluated_at=evaluated_at, runtime_ms=elapsed_first * 1000
    )
    if runtime_decision.semantic_identity != first.semantic_identity:
        raise SystemExit("runtime metadata leaked into the semantic decision identity")

    payload = first.as_dict()
    return {
        "benchmark": "inv25-routing-promotion-gate",
        "schema_version": "marker.routing_promotion.evidence.v1",
        "git_sha": _git_head(),
        "evaluated_at": evaluated_at,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identities": payload["identities"],
        "decision": payload["decision"],
        "actors": {
            "registry_identity": ACTOR_REGISTRY_V1.semantic_identity,
            "roles": {
                actor.role: {
                    "policy_id": actor.policy_id,
                    "implementation_module": actor.implementation_module,
                    "masterplan_references": list(actor.masterplan_references),
                }
                for actor in ACTOR_REGISTRY_V1.actors
            },
            "containment": (
                "the candidate policy has no import outside app/eval; no serving "
                "behavior is switched by this artifact"
            ),
        },
        "contract": ROUTING_PROMOTION_CONTRACT.as_dict(),
        "population": {
            "name": corpus.name,
            "semantic_identity": corpus.semantic_identity,
            "slice_counts": {
                slice_id: len(corpus.samples_for_slice(slice_id))
                for slice_id in corpus.slice_ids
            },
            "witnesses": sorted(w.witness_id for w in corpus.witnesses),
            "baseline_best_single_witness": corpus.metadata[
                "baseline_best_single_witness"
            ],
        },
        "leakage": payload["leakage"],
        "slices": payload["slices"],
        "candidate_gate_status": payload["candidate_gate_status"],
        "catastrophic": payload["catastrophic"],
        "criteria": payload["criteria"],
        "reproducibility": {
            "semantic_identity_runs": [
                first.semantic_identity,
                second.semantic_identity,
            ],
            "stable": True,
            "runtime_ms": {
                "first": elapsed_first * 1000,
                "second": elapsed_second * 1000,
            },
        },
        "limitations": LIMITATIONS,
        "reproduce": REPRODUCE,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write measurement JSON")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    parser.add_argument(
        "--evaluated-at",
        default=DEFAULT_EVALUATED_AT,
        help="ISO-8601 evaluation timestamp (default: documented first final run)",
    )
    args = parser.parse_args()
    measurements = build_measurements(evaluated_at=args.evaluated_at)
    encoded = json.dumps(measurements, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.write_text(encoded, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
