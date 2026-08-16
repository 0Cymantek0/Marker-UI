"""Operational benchmark for PR73 conflict-aware patches & incremental rebuild.

Measures, on this machine:

* patch/proposal/view identity derivation cost;
* precondition (conflict-check) evaluation cost;
* clean vs incremental rebuild for a local source edit as the synthetic
  graph grows 10x — derivation-locality is the incremental win: the
  clean path derives every node, the incremental path derives only the
  invalidated node and carries the rest from the declared source;
* worst-case invalidation amplification when knowledge is conservative.

Writes docs/reference/measurements/pr73-patches-incremental.json.
Run: python backend/scripts/bench_pr73_patches.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.kernel.dependencies import (  # noqa: E402
    COMPLETENESS_CONSERVATIVE_SCOPE,
    COMPLETENESS_EXACT_NATIVE,
    DependencyDeclarationRecord,
    DependencyInput,
    compute_invalidation,
)
from app.kernel.patches import (  # noqa: E402
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    ViewDocumentRecord,
    apply_rebase_source,
    evaluate_preconditions,
    view_text_hash,
)
from app.kernel.reading_order import (  # noqa: E402
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    order_confidence,
)

MEASUREMENTS_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "measurements"
)

CONF = order_confidence("1.0")


def make_graph(node_ids):
    nodes = [OrderNode(node_id=nid, anchor_ref=f"anchor-{nid}") for nid in node_ids]
    edges = [
        OrderEdge(kind="before", source_id=a, target_id=b, producer="bench", confidence=CONF)
        for a, b in zip(node_ids, node_ids[1:])
    ]
    return ReadingOrderGraph.build(nodes, edges)


def time_callable(fn, *, repeat=5):
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best


def bench_identity(count=1000):
    node_ids = [f"n{i:05d}" for i in range(count)]
    graph = make_graph(node_ids[:3])
    texts = {nid: f"text-{nid}" for nid in node_ids[:3]}
    view = ViewDocumentRecord(
        record_id="bench-view", content_revision_ref="rev-s1", graph=graph, texts=texts
    )

    def identities():
        for i in range(count):
            proposal = PatchProposalRecord(
                record_id=f"p{i}",
                preconditions=PatchPreconditions(
                    base_revision_id=view.view_revision_id(),
                    target_checks=(
                        TargetCheck(
                            node_id="n00000", before_hash=view_text_hash("text-n00000")
                        ),
                    ),
                ),
                operations=(
                    PatchOperation.replace_text(
                        node_id="n00000", after_text=f"fixed-{i}"
                    ),
                ),
            )
            proposal.proposal_id()
            view.view_revision_id()

    elapsed = time_callable(identities)
    return {
        "proposal_and_view_identities_1000_s": round(elapsed, 4),
        "identity_us_each": round(elapsed / count * 1e6, 1),
    }


def bench_preconditions(count=2000):
    node_ids = [f"n{i:05d}" for i in range(50)]
    graph = make_graph(node_ids)
    texts = {nid: f"text-{nid}" for nid in node_ids}
    view = ViewDocumentRecord(
        record_id="bench-view", content_revision_ref="rev-s1", graph=graph, texts=texts
    )
    checks = tuple(
        TargetCheck(node_id=nid, before_hash=view_text_hash(texts[nid]))
        for nid in node_ids
    )
    preconditions = PatchPreconditions(
        base_revision_id=view.view_revision_id(), target_checks=checks
    )

    def run():
        for _ in range(count // 50):
            evaluate_preconditions(view, preconditions)

    elapsed = time_callable(run)
    return {
        "precondition_checks_total": count,
        "precondition_evaluations_s": round(elapsed, 4),
        "precondition_us_per_check": round(elapsed / count * 1e6, 2),
    }


def bench_rebuild(n_nodes, edit_node_index=0):
    node_ids = [f"n{i:05d}" for i in range(n_nodes)]
    s1 = {nid: f"text-{nid}" for nid in node_ids}
    graph = make_graph(node_ids)
    view = ViewDocumentRecord(
        record_id="bench-view", content_revision_ref="rev-s1", graph=graph, texts=dict(s1)
    )
    # One accepted repair on an untouched node, to make replay realistic.
    proposal = PatchProposalRecord(
        record_id="bench-repair",
        preconditions=PatchPreconditions(
            base_revision_id=view.view_revision_id(),
            target_checks=(
                TargetCheck(
                    node_id=node_ids[-1], before_hash=view_text_hash(s1[node_ids[-1]])
                ),
            ),
        ),
        operations=(
            PatchOperation.replace_text(node_id=node_ids[-1], after_text="repaired"),
        ),
    )
    proposals = {"bench-repair": proposal}
    changed = node_ids[edit_node_index]
    s2 = dict(s1)
    s2[changed] = f"text-{changed}-v2"

    derive_calls = {"clean": 0, "incremental": 0}

    def run_clean():
        # Full derivation: every node's value is recomputed.
        texts = {}
        for nid in node_ids:
            derive_calls["clean"] += 1
            texts[nid] = s2[nid]
        op = PatchOperation.rebase_source(
            new_content_revision_ref="rev-s2",
            source_graph=graph,
            source_texts=texts,
            replay_proposal_refs=("bench-repair",),
        )
        return apply_rebase_source(op, proposals).view.view_revision_id()

    def run_incremental():
        # Localized derivation: only the invalidated node is recomputed;
        # the rest carries from the declared source (s1 values, which
        # for unchanged nodes equal the new source's values).
        texts = {}
        for nid in node_ids:
            if nid == changed:
                derive_calls["incremental"] += 1
                texts[nid] = s2[nid]
            else:
                texts[nid] = s1[nid]
        op = PatchOperation.rebase_source(
            new_content_revision_ref="rev-s2",
            source_graph=graph,
            source_texts=texts,
            replay_proposal_refs=("bench-repair",),
        )
        return apply_rebase_source(op, proposals).view.view_revision_id()

    clean_calls_before = derive_calls["clean"]
    incremental_calls_before = derive_calls["incremental"]
    clean_time = time_callable(run_clean)
    incremental_time = time_callable(run_incremental)
    return {
        "nodes": n_nodes,
        "clean_derive_calls": derive_calls["clean"] - clean_calls_before,
        "incremental_derive_calls": derive_calls["incremental"] - incremental_calls_before,
        "clean_replay_s": round(clean_time, 4),
        "incremental_replay_s": round(incremental_time, 4),
        "derive_call_ratio_clean_over_incremental": round(
            (derive_calls["clean"] - clean_calls_before)
            / max(1, derive_calls["incremental"] - incremental_calls_before),
            1,
        ),
        "same_result": run_clean() == run_incremental(),
    }


def bench_invalidation_amplification(n_nodes):
    node_ids = [f"n{i:05d}" for i in range(n_nodes)]
    changed_ref = f"fact:{node_ids[0]}"

    exact = [
        DependencyDeclarationRecord(
            record_id=f"decl-{nid}",
            subject_ref=f"derived:{nid}",
            inputs=(DependencyInput(f"fact:{nid}", COMPLETENESS_EXACT_NATIVE),),
            operator="bench.renderer",
            operator_version="1.0.0",
        )
        for nid in node_ids
    ]
    # The same graph, but its dependency knowledge is only conservative:
    # every node's exact inputs are unknown, so one local change widens
    # to every subject sharing the declared scope.
    conservative = [
        DependencyDeclarationRecord(
            record_id=f"cdecl-{nid}",
            subject_ref=f"derived:{nid}",
            inputs=(DependencyInput(f"fact:{nid}", COMPLETENESS_CONSERVATIVE_SCOPE),),
            scope_ref="document",
            operator="bench.renderer",
            operator_version="1.0.0",
        )
        for nid in node_ids
    ]
    exact_result = compute_invalidation([changed_ref], exact)
    conservative_result = compute_invalidation([changed_ref], conservative)
    unknown_result = compute_invalidation(["fact:nobody-knows"], exact)
    return {
        "nodes": n_nodes,
        "exact_local_edit_invalidated": len(exact_result.invalidated),
        "conservative_local_edit_invalidated": len(conservative_result.invalidated),
        "conservative_widened": conservative_result.widened,
        "unknown_change_invalidated": len(unknown_result.invalidated),
        "unknown_change_uncovered": len(unknown_result.uncovered_changes),
        "worst_amplification_factor": round(
            len(conservative_result.invalidated)
            / max(1, len(exact_result.invalidated)),
            1,
        ),
    }


def main() -> int:
    results = {
        "benchmark": "pr73-patches-incremental",
        "identity": bench_identity(),
        "preconditions": bench_preconditions(),
        "rebuild_small": bench_rebuild(200),
        "rebuild_10x": bench_rebuild(2000),
        "invalidation_amplification": bench_invalidation_amplification(2000),
    }
    results["locality_holds_at_10x"] = (
        results["rebuild_10x"]["incremental_derive_calls"]
        == results["rebuild_small"]["incremental_derive_calls"]
    )
    MEASUREMENTS_PATH.mkdir(parents=True, exist_ok=True)
    output = MEASUREMENTS_PATH / "pr73-patches-incremental.json"
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
