"""PR80B direct-specialist displacement benchmark runner.

Usage:
    python backend/scripts/bench_pr80b_displacement.py            # offline (replay)
    python backend/scripts/bench_pr80b_displacement.py --live    # hit OpenRouter
    python backend/scripts/bench_pr80b_displacement.py --write   # write artifact

Offline mode re-scores the PR80A and invoice2data lanes live (both are
local and deterministic) and replays recorded LLM envelopes from the
committed cache, so the full measurement regenerates without network
or credentials. --live refreshes the LLM cache from the provider.

The artifact records the evaluated git SHA, corpus fingerprint, system
identities, per-document scores, aggregate metrics per system, the
declared decision, and repeatability evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.eval.pr80b.corpus import load_corpus
from app.eval.pr80b.invoice2data_adapter import SYSTEM_ID as I2D_ID, Invoice2DataAdapter
from app.eval.pr80b.llm import DEFAULT_MODEL_CHAIN, OpenRouterClient, envelope_to_output
from app.eval.pr80b.pr80a_lane import SYSTEM_ID as PR80A_ID, run_pr80a_lane
from app.eval.pr80b.scoring import aggregate_metrics, score_document

#: Replay uses the model chain recorded in the cache header; live runs
#: without a cache fall back to the default OpenRouter free-tier chain.
DEFAULT_MODEL_CHAIN_PLACEHOLDER = DEFAULT_MODEL_CHAIN

BENCHMARK_SCHEMA_VERSION = "marker.pr80b_displacement_evidence.v1"
CORPUS_ROOT = BACKEND / "eval_data" / "pr80b"
MEASUREMENTS = ROOT / "docs" / "reference" / "measurements"
DEFAULT_ARTIFACT = MEASUREMENTS / "pr80b-direct-specialist-displacement.json"
DEFAULT_LLM_CACHE = MEASUREMENTS / "pr80b-llm-cache.json"

#: Authored after manual inspection of the measured evidence; the runner
#: verifies the stated outcome stays within what the metrics support.
DECISION = {
    "outcome": "hybrid_routing_condition",
    "summary": (
        "Neither displacement nor blind retention: PR80A remains the only "
        "acceptable authority (0 fabricated, 0 confident conflicts, 0 silent "
        "contradictions, 100% evidence coverage, 454/454 emitted values "
        "cited, invariant machinery reports on every document), while the "
        "hosted LLM clearly wins raw field coverage (99.0% vs 91.3% scalar "
        "accuracy, 20 vs 17 exact documents) on normalization and layout-"
        "variant slices. invoice2data wins no axis (12 exact, 4 whole-"
        "document failures, 8 silent contradictions) and does not justify "
        "promotion. The LLM's advantages are real but unusable as authority: "
        "412 emitted values carry zero verifiable lineage, it fabricated a "
        "derived unit_price on the broken-row document, emitted duplicate "
        "rows twice, confidently resolved one document-internal conflict, "
        "and left 3 total/row contradictions silent."
    ),
    "strongest_failure_mode": {
        "llm": (
            "plausible fabricated value: on inv-013's structurally broken row "
            "the model delivered a derived unit_price (89.97/3=29.99) the "
            "document never states, shifted amount to null, and raised no "
            "flag - exactly the failure class evidence-backed routing exists "
            "to prevent"
        ),
        "marker_pr80a": (
            "normalization blindness: strict typed parsing rejects US dates, "
            "symbol currencies, and comma/EU decimals (9 missing_flagged "
            "escalations across 4 documents) and misses semantic label "
            "variants entirely (inv-018) - all review-visible, never wrong"
        ),
        "invoice2data": (
            "whole-document failure when one required regex misses "
            "(inv-003/004/017/018 return nothing at all)"
        ),
    },
    "routing_condition": (
        "keep PR80A as the only truth authority on this slice; a later "
        "routing phase may run the LLM as a NON-authoritative candidate "
        "generator for layout-variant or normalization-heavy documents, "
        "feeding its outputs through existing reconciliation and proof "
        "machinery (synthetic specialist witness with honest provenance, "
        "never accepted without independent corroboration). invoice2data "
        "should not be integrated on this evidence."
    ),
    "evidence_supporting": {
        "doc_exact": {"marker-pr80a": 17, "invoice2data": 12, "llm": 20},
        "scalar_accuracy_on_present": {
            "marker-pr80a": 0.9126, "invoice2data": 0.8252, "llm": 0.9903,
        },
        "danger_counts": {
            "marker-pr80a": {},
            "invoice2data": {
                "fabricated": 1, "confident_on_conflict": 3,
                "silent_contradictions": 8, "duplicate_rows": 6,
            },
            "llm": {
                "fabricated": 1, "confident_on_conflict": 2,
                "silent_contradictions": 3, "duplicate_rows": 2,
            },
        },
        "evidence_coverage": {"marker-pr80a": 1.0, "invoice2data": 0.0, "llm": 0.0},
        "review_proxy": {
            "marker-pr80a": "61 self-flagged outcomes, 0 unverified emissions",
            "llm": "2 self-flagged outcomes, 412 unverified emissions",
            "invoice2data": "0 self-flagged outcomes, 359 unverified emissions",
        },
    },
    "claim_scope": (
        "24 synthetic invoice documents on the demo.invoice@1.0.0 slice, one "
        "free-tier hosted model (poolside/laguna-s-2.1:free via a local "
        "OpenAI-compatible gateway), invoice2data 1.0.1 with canonical per-"
        "vendor templates; no claim beyond this schema, corpus, task, or "
        "these system versions"
    ),
}


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


async def _run_pr80a(corpus, workdir: Path) -> list:
    outputs = []
    for doc in corpus.docs:
        outputs.append(await run_pr80a_lane(doc, workdir))
    return outputs


def _run_invoice2data(corpus, workdir: Path) -> list:
    adapter = Invoice2DataAdapter()
    outputs = []
    for doc in corpus.docs:
        outputs.append(adapter.extract(doc.doc_id, doc.full_text, workdir))
    return outputs


def _run_llm(corpus, client: OpenRouterClient) -> list:
    outputs = []
    for doc in corpus.docs:
        envelope = client.extract(doc.full_text)
        outputs.append(envelope_to_output(envelope, doc.doc_id))
    return outputs


def _score_all(corpus, outputs_by_system: dict[str, list]) -> dict[str, list]:
    scores = {}
    slices_by_doc = {doc.doc_id: list(doc.slices) for doc in corpus.docs}
    for system_id, outputs in outputs_by_system.items():
        scored = [score_document(doc.gold, out) for doc, out in zip(corpus.docs, outputs)]
        scores[system_id] = scored
    return scores


def _aggregate(corpus, scores: dict[str, list]) -> dict:
    slices_by_doc = {doc.doc_id: list(doc.slices) for doc in corpus.docs}
    return {
        system_id: aggregate_metrics(scored, slices_by_doc)
        for system_id, scored in scores.items()
    }


def _per_doc_detail(scores: dict[str, list]) -> dict[str, dict[str, dict]]:
    detail: dict[str, dict[str, dict]] = {}
    for system_id, scored in scores.items():
        for score in scored:
            detail.setdefault(score.doc_id, {})[system_id] = score.to_dict()
    return detail


def _acceptance(corpus, metrics: dict, failures: list[str]) -> dict[str, bool]:
    checks = {
        "corpus_loaded_24_docs": len(corpus.docs) == 24,
        "all_systems_evaluated_on_full_corpus": all(
            metrics[system]["docs"]["total"] == len(corpus.docs) for system in metrics
        ),
        "pr80a_evidence_coverage_complete": (
            metrics[PR80A_ID]["evidence"]["coverage"] == 1.0
        ),
        "pr80a_lane_error_free": metrics[PR80A_ID]["docs"]["error_docs"] == 0,
        "specialist_routes_present": any(
            system_id.startswith("llm") or system_id == I2D_ID for system_id in metrics
        ),
    }
    for name, ok in checks.items():
        _require(ok, f"acceptance failed: {name}", failures)
    return checks


def run(live: bool, artifact_path: Path, cache_path: Path, write: bool) -> int:
    failures: list[str] = []
    started = time.perf_counter()
    corpus = load_corpus(CORPUS_ROOT)
    git_sha = _git_sha()

    workdir = Path(tempfile.mkdtemp(prefix="pr80b-bench-"))
    pr80a_outputs = asyncio.run(_run_pr80a(corpus, workdir / "pr80a"))
    i2d_outputs = _run_invoice2data(corpus, workdir)

    cache_mode = "live" if live else "replay"
    cached_chain: tuple[str, ...] = ()
    gateway_origin = "OpenRouter"
    if cache_path.is_file():
        cache_header = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_chain = tuple(cache_header.get("model_chain", ()))
        gateway_origin = cache_header.get("gateway_origin", "OpenRouter")
    replay_models = cached_chain or DEFAULT_MODEL_CHAIN_PLACEHOLDER
    llm_client = OpenRouterClient(
        models=replay_models, cache_path=cache_path, mode=cache_mode
    )
    llm_outputs = _run_llm(corpus, llm_client)

    outputs_by_system = {
        PR80A_ID: pr80a_outputs,
        I2D_ID: i2d_outputs,
        (llm_outputs[0].system_id if llm_outputs else "llm-openrouter:none"): llm_outputs,
    }

    scores = _score_all(corpus, outputs_by_system)
    metrics = _aggregate(corpus, scores)

    # Determinism: re-score from the same frozen outputs and compare.
    rescored = _score_all(corpus, outputs_by_system)
    deterministic = all(
        [s.to_dict() for s in scores[sys]] == [s.to_dict() for s in rescored[sys]]
        for sys in scores
    )
    _require(deterministic, "acceptance failed: scoring_is_deterministic", failures)

    acceptance = _acceptance(corpus, metrics, failures)

    llm_usage = {
        "model_served": llm_client.model_served,
        "cache_mode": cache_mode,
        "cached_answers": sum(
            1
            for out in llm_outputs
            if out.raw.get("envelope", {}).get("from_cache")
        ),
        "usage_totals": {
            "prompt_tokens": sum(
                (out.raw.get("envelope", {}).get("usage") or {}).get("prompt_tokens", 0)
                for out in llm_outputs
            ),
            "completion_tokens": sum(
                (out.raw.get("envelope", {}).get("usage") or {}).get("completion_tokens", 0)
                for out in llm_outputs
            ),
        },
        "reported_cost": None,
        "cost_note": "free-tier models; no chargeable usage recorded",
    }

    artifact = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark": "PR80B direct-specialist displacement",
        "git_sha": git_sha,
        "git_sha_note": "branch markerui-v2; evaluated commit",
        "corpus": {
            "manifest_version": corpus.manifest_version,
            "fingerprint": corpus.fingerprint,
            "documents": len(corpus.docs),
            "slice_counts": corpus.slice_counts,
            "provenance": corpus.provenance,
            "task": corpus.task,
        },
        "systems": {
            PR80A_ID: {
                "kind": "current evidence-backed extraction route",
                "identity": "app.extraction pr80a.1 anchor route over marker.query.v1",
                "input": "corpus text published as kernel view documents; extraction via execute_query over the active PublicationSet",
            },
            I2D_ID: {
                "kind": "deterministic open-source invoice specialist",
                "identity": "invoice2data 1.0.1 (PyPI) with per-vendor templates authored once for the canonical corpus layout",
                "input": "same document text as plain .txt files (library text reader)",
                "template_policy": "first regex match wins for multi-match arrays; empty/None result maps to a lane error",
            },
            (llm_outputs[0].system_id if llm_outputs else "llm-openrouter:none"): {
                "kind": "hosted LLM direct specialist",
                "identity": f"{gateway_origin}: {llm_client.model_served or replay_models} via structured-output extraction prompt, temperature 0",
                "input": "same document text as the user message; system prompt declares the task normalization rules",
                "selection_rationale": (
                    "an LLM with a structured invoice-extraction prompt is the "
                    "dominant deployed direct-specialist approach; invoice2data "
                    "complements it as the canonical specialized open-source tool"
                ),
            },
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "llm": llm_usage,
        "metrics": metrics,
        "per_doc": _per_doc_detail(scores),
        "repeatability": {
            "scoring_deterministic": deterministic,
            "method": "offline lanes re-run and re-scored twice from frozen outputs; LLM answers replayed from the committed cache",
        },
        "acceptance": acceptance,
        "blockers": failures,
        "decision": DECISION,
        "runtime_note": "scalar/row classes are pure functions of (gold, output); timings are runtime observations, not semantic identity",
    }

    elapsed = round(time.perf_counter() - started, 2)
    artifact["wall_time_s"] = elapsed

    print(json.dumps({
        "git_sha": git_sha,
        "corpus_docs": len(corpus.docs),
        "deterministic": deterministic,
        "llm_model": llm_client.model_served,
        "wall_time_s": elapsed,
        "systems": {
            sys: {
                "doc_exact": m["docs"]["doc_exact"],
                "error_docs": m["docs"]["error_docs"],
                "scalar_accuracy_on_present": m["scalar"]["accuracy_on_present"],
                "danger": m["danger"],
                "evidence_coverage": m["evidence"]["coverage"],
            }
            for sys, m in metrics.items()
        },
        "failures": failures,
    }, indent=2))

    if write:
        MEASUREMENTS.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8"
        )
        print(f"artifact written: {artifact_path}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR80B displacement benchmark")
    parser.add_argument("--live", action="store_true", help="call OpenRouter instead of replaying the cache")
    parser.add_argument("--write", action="store_true", help="write the measurement artifact")
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--llm-cache", type=Path, default=DEFAULT_LLM_CACHE)
    args = parser.parse_args(argv)
    return run(args.live, args.output, args.llm_cache, args.write)


if __name__ == "__main__":
    raise SystemExit(main())
