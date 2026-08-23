"""Specialist-bridge hybrid benchmark (deterministic offline rerun).

Compares the deterministic PR80A extraction against the hybrid
candidate path (PR80A + trained specialist as non-authoritative
proposer + deterministic corroboration) over the committed PR80B
24-document corpus, replaying the committed recorded provider
responses through the production ReplayProvider. Nothing contacts a
provider; no credentials are involved.

Usage (from backend/ or repository root):

    python backend/scripts/bench_specialist_bridge.py            # summary
    python backend/scripts/bench_specialist_bridge.py --write    # artifact
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.eval.bridge.runner import (  # noqa: E402
    hybrid_system_id,
    result_authority_metrics,
    run_bridge_lane,
)
from app.eval.bridge.translate import build_corpus_lookup  # noqa: E402
from app.eval.pr80b.corpus import load_corpus  # noqa: E402
from app.eval.pr80b.pr80a_lane import run_pr80a_lane  # noqa: E402
from app.eval.pr80b.scoring import aggregate_metrics, score_document  # noqa: E402

ARTIFACT_SCHEMA = "marker.specialist_bridge_evidence.v1"
DEFAULT_CORPUS_ROOT = BACKEND_ROOT / "eval_data" / "pr80b"
DEFAULT_CACHE = ROOT / "docs" / "reference" / "measurements" / "pr80b-llm-cache.json"
DEFAULT_OUTPUT = ROOT / "docs" / "reference" / "measurements" / "specialist-bridge-hybrid.json"
PR80A_ID = "marker-pr80a"


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _file_fingerprint(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _fabricated_to_authority(score, accepted: set[str]) -> list[str]:
    """Values that reached authority (accepted by the hybrid policy)
    but gold marks fabricated for the system's emitted value."""
    events: list[str] = []
    for result in score.scalars:
        if result.outcome == "fabricated" and result.field in accepted:
            events.append(result.field)
    for row in score.rows:
        for name, outcome in row.field_outcomes:
            path = f"items[sku={row.sku}].{name}"
            if outcome == "fabricated" and path in accepted:
                events.append(path)
    return events


async def run(
    *,
    corpus_root: Path,
    cache_path: Path,
    output_path: Path,
    workdir: Path | None,
    write: bool,
) -> int:
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="bridge-bench-"))
    corpus = load_corpus(corpus_root)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    model = cache["model_chain"][0]
    served_models = sorted(
        {
            str(env.get("model_served"))
            for env in cache["responses"].values()
            if env.get("model_served")
        }
    )
    lookup = build_corpus_lookup(corpus, cache["responses"], model=model)

    pr80a_outputs = []
    hybrid_outputs = []
    hybrid_metrics: dict[str, dict] = {}
    for doc in corpus.docs:
        pr80a_outputs.append(await run_pr80a_lane(doc, workdir / "pr80a"))
        output, metrics = await run_bridge_lane(
            doc, workdir / "hybrid", lookup, model=model
        )
        hybrid_outputs.append(output)
        hybrid_metrics[doc.doc_id] = metrics

    slices_by_doc = {doc.doc_id: list(doc.slices) for doc in corpus.docs}
    pr80a_scores = [
        score_document(doc.gold, out)
        for doc, out in zip(corpus.docs, pr80a_outputs)
    ]
    hybrid_scores = [
        score_document(doc.gold, out)
        for doc, out in zip(corpus.docs, hybrid_outputs)
    ]
    # determinism: re-score frozen outputs; identical results required
    hybrid_rescored = [
        score_document(doc.gold, out)
        for doc, out in zip(corpus.docs, hybrid_outputs)
    ]
    assert [s.to_dict() for s in hybrid_scores] == [
        s.to_dict() for s in hybrid_rescored
    ]

    fabricated_events: dict[str, list[str]] = {}
    for doc, score in zip(corpus.docs, hybrid_scores):
        accepted = set(hybrid_metrics[doc.doc_id]["accepted_fields"])
        events = _fabricated_to_authority(score, accepted)
        if events:
            fabricated_events[doc.doc_id] = events

    totals = {
        "docs": len(corpus.docs),
        "authoritative_accepted_fields": sum(
            len(m["accepted_fields"]) for m in hybrid_metrics.values()
        ),
        "corroborated_fields": sum(
            len(m["corroborated_fields"]) for m in hybrid_metrics.values()
        ),
        "proposal_review_fields": sum(
            len(m["proposal_review_fields"]) for m in hybrid_metrics.values()
        ),
        "proposal_only_fields": sum(
            len(m["proposal_only_fields"]) for m in hybrid_metrics.values()
        ),
        "fields_with_any_proposal": sum(
            len(m["proposal_fields"]) for m in hybrid_metrics.values()
        ),
        "false_authority_events": sum(
            len(m["false_authority_events"]) for m in hybrid_metrics.values()
        ),
        "conflicts_preserved": sum(
            len(m["conflicts_preserved"]) for m in hybrid_metrics.values()
        ),
        "hybrid_review_required_fields": sum(
            len(m["review_required_fields"]) for m in hybrid_metrics.values()
        ),
        "accepted_fields_without_lineage": sum(
            len(m["accepted_without_lineage"]) for m in hybrid_metrics.values()
        ),
        "fabricated_to_authority_events": sum(
            len(v) for v in fabricated_events.values()
        ),
        "lane_failures": sum(
            1 for m in hybrid_metrics.values() if m["lane_status"] != "ok"
        ),
    }
    accepted_total = totals["authoritative_accepted_fields"]
    without_lineage = totals["accepted_fields_without_lineage"]
    lineage_coverage = (
        (accepted_total - without_lineage) / accepted_total if accepted_total else 1.0
    )

    # No-regression: every doc PR80A extracted exactly must remain an
    # exact doc on the hybrid path (authority never revoked).
    pr80a_exact = sum(1 for score in pr80a_scores if score.doc_exact)
    hybrid_exact = sum(1 for score in hybrid_scores if score.doc_exact)

    failures: list[str] = []

    def _require(condition: bool, message: str) -> bool:
        if not condition:
            failures.append(message)
        return condition

    _require(totals["docs"] == 24, "corpus must load 24 documents")
    _require(totals["lane_failures"] == 0, "hybrid lane must cover all docs via replay")
    _require(
        totals["false_authority_events"] == 0,
        "zero false-authority events required",
    )
    _require(
        totals["fabricated_to_authority_events"] == 0,
        "zero fabricated values may reach authority",
    )
    _require(
        lineage_coverage == 1.0,
        "every accepted value must carry source/proof lineage",
    )
    _require(
        hybrid_exact >= pr80a_exact,
        "hybrid must not reduce exact documents vs PR80A",
    )

    aggregate = {
        PR80A_ID: aggregate_metrics(pr80a_scores, slices_by_doc),
        hybrid_system_id(model): aggregate_metrics(hybrid_scores, slices_by_doc),
    }

    artifact = {
        "artifact_schema_version": ARTIFACT_SCHEMA,
        "git_sha": _git_sha(),
        "corpus": {
            "root": str(corpus_root.relative_to(ROOT)),
            "fingerprint": corpus.fingerprint,
            "documents": len(corpus.docs),
        },
        "specialist": {
            "model_requested": model,
            "model_served": served_models,
            "replay": True,
            "cache_path": str(cache_path.relative_to(ROOT)),
            "cache_fingerprint": _file_fingerprint(cache_path),
            "recorded_responses": len(cache["responses"]),
        },
        "policy": {
            "grounded": "marker.extraction.reconcile/v1",
            "hybrid": "marker.extraction.hybrid/v1",
            "corroboration_rule": (
                "hybrid.corroboration.deterministic_normalization.v1"
            ),
        },
        "schema_identity": "demo.invoice@1.0.0",
        "totals": totals,
        "lineage_coverage": lineage_coverage,
        "doc_exact": {"marker-pr80a": pr80a_exact, "hybrid": hybrid_exact},
        "aggregate_metrics": aggregate,
        "fabricated_to_authority": fabricated_events,
        "per_doc": {
            doc.doc_id: {
                "metrics": hybrid_metrics[doc.doc_id],
                "score": score.to_dict(),
            }
            for doc, score in zip(corpus.docs, hybrid_scores)
        },
        "acceptance": {
            "corpus_24": totals["docs"] == 24,
            "zero_false_authority": totals["false_authority_events"] == 0,
            "zero_fabricated_to_authority": (
                totals["fabricated_to_authority_events"] == 0
            ),
            "full_lineage": lineage_coverage == 1.0,
            "no_exact_doc_regression": hybrid_exact >= pr80a_exact,
            "lane_full_coverage": totals["lane_failures"] == 0,
        },
        "claim_scope": (
            "24 synthetic PR80B corpus documents, one recorded free-tier "
            "model replayed offline, demo.invoice@1.0.0 only; no claim "
            "about held-out routing promotion, production review "
            "capacity, or other providers"
        ),
    }

    print(f"docs: {totals['docs']}")
    print(f"authoritative accepted fields: {totals['authoritative_accepted_fields']}")
    print(f"  of which corroborated: {totals['corroborated_fields']}")
    print(f"proposal-review fields: {totals['proposal_review_fields']}")
    print(f"false authority events: {totals['false_authority_events']}")
    print(f"fabricated-to-authority events: {totals['fabricated_to_authority_events']}")
    print(f"lineage coverage: {lineage_coverage}")
    print(f"doc-exact: pr80a={pr80a_exact} hybrid={hybrid_exact}")
    if failures:
        print("ACCEPTANCE FAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"artifact written: {output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="reuse a specific scratch dir (fresh throwaway dir by default)",
    )
    args = parser.parse_args(argv)

    import asyncio

    return asyncio.run(
        run(
            corpus_root=args.corpus_root,
            cache_path=args.cache,
            output_path=args.output,
            workdir=args.workdir,
            write=args.write,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
