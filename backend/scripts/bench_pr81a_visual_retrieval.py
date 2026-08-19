"""PR81A selective visual retrieval benchmark runner.

One script, two modes:

* ``--live``: renders the corpus on demand, embeds pages with CLIP and
  SigLIP, calls the hosted VLM for answers/reranks, records every
  response into the replay cache, and writes the evidence artifact.
  First execution of a corpus must be live.
* default (replay): loads committed visual generations from the
  measurement npz files and the VLM replay cache; nothing touches the
  network, no model downloads, no credentials.

Phases, in order: baseline + pre-revision (v3 cut) -> denied-domain +
high-assurance probes -> revision to v4 (update-amplification measured)
-> post-revision on the new cut + pinned probe on the old cut.

Usage:
  python scripts/bench_pr81a_visual_retrieval.py --live --write
  python scripts/bench_pr81a_visual_retrieval.py --write          # replay
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.db_migration import upgrade_database  # noqa: E402
from app.eval.pr81a.corpus import load_corpus  # noqa: E402
from app.eval.pr81a.decision import evaluate_decision  # noqa: E402
from app.eval.pr81a.embeddings import ClipEmbedder, HashEmbedder, SiglipEmbedder  # noqa: E402
from app.eval.pr81a.kernel_seed import (  # noqa: E402
    publish_high_assurance_partition,
    revise_document,
    seed_workspace,
)
from app.eval.pr81a.lanes import (  # noqa: E402
    B1_SYSTEM,
    B2_SYSTEM,
    V2_JOINT_SYSTEM,
    V2_SYSTEM,
    V2_TEXT_SYSTEM,
    V2_UNION_SYSTEM,
    LaneContext,
    build_visual_index,
    resolve_phase_authorization,
    run_lane,
)
from app.eval.pr81a.scoring import aggregate_metrics, score_query  # noqa: E402
from app.eval.pr81a.visual_index import VisualIndex  # noqa: E402
from app.eval.pr81a.visual_store import PageRenderStore  # noqa: E402
from app.eval.pr81a.vlm import OPENROUTER_BASE_URL, VlmClient  # noqa: E402
from app.kernel.commit import KernelCommitService  # noqa: E402
from app.kernel.payloads import LocalPayloadStore  # noqa: E402
from app.services.query_policy import QueryPolicyService  # noqa: E402

EVIDENCE_SCHEMA = "marker.pr81a_visual_retrieval_evidence.v1"
MEASUREMENTS = BACKEND.parent / "docs" / "reference" / "measurements"
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"

def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _phase_queries(corpus, *, phases: tuple[str, ...], profiles: tuple[str, ...] = ("default",)):
    return [
        q for q in corpus.queries if q.phase in phases and q.profile in profiles
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call the hosted VLM")
    parser.add_argument("--write", action="store_true", help="write the evidence artifact")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS / "pr81a-visual-retrieval.json")
    parser.add_argument("--vlm-cache", type=Path, default=MEASUREMENTS / "pr81a-vlm-cache.json")
    parser.add_argument("--index-dir", type=Path, default=MEASUREMENTS)
    parser.add_argument("--skip-siglip", action="store_true")
    parser.add_argument(
        "--ablations",
        action="store_true",
        help="also run the PR81B hybrid ablation lanes (text/joint answer, "
        "union-only) and the hybrid lane under high assurance; the default "
        "lane set and the committed PR81A evidence stay byte-identical",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    corpus = load_corpus(CORPUS_ROOT)
    git_sha = _git_sha()

    dense_specs = [("clip", ClipEmbedder())]
    if not args.skip_siglip:
        dense_specs.append(("siglip", SiglipEmbedder()))

    base_url = os.environ.get("PR81A_VLM_BASE_URL", OPENROUTER_BASE_URL)
    env_models = os.environ.get("PR81A_VLM_MODELS")
    if args.live:
        # auto = cache first, network on miss: reruns resume and only
        # refill calls that previously failed, so transient outages heal
        models = [m.strip() for m in env_models.split(",") if m.strip()] if env_models else None
        vlm = VlmClient(models=models, base_url=base_url, cache_path=args.vlm_cache, mode="auto")
    else:
        header_models = None
        if args.vlm_cache.is_file():
            header_models = json.loads(args.vlm_cache.read_text(encoding="utf-8")).get("model_chain")
        vlm = VlmClient(models=header_models, base_url=base_url, cache_path=args.vlm_cache, mode="replay")

    async def run() -> dict:
        with tempfile.TemporaryDirectory(prefix="pr81a-bench-") as tmp:
            tmp_path = Path(tmp)
            url = f"sqlite+aiosqlite:///{(tmp_path / 'kernel.db').as_posix()}"
            await upgrade_database(url=url)
            engine = create_async_engine(url, connect_args={"check_same_thread": False})
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            service = KernelCommitService(factory, payload_store=LocalPayloadStore(tmp_path / "payloads"))
            try:
                return await _run_benchmark(
                    factory=factory,
                    service=service,
                    corpus=corpus,
                    tmp_path=tmp_path,
                    vlm=vlm,
                    dense_specs=dense_specs,
                    index_dir=args.index_dir,
                    live=args.live,
                    ablations=args.ablations,
                )
            finally:
                await engine.dispose()

    artifact = asyncio.run(run())
    artifact.update(
        {
            "schema_version": EVIDENCE_SCHEMA,
            "benchmark": "PR81A selective visual retrieval promotion experiment",
            "git_sha": git_sha,
            "corpus": {
                "manifest_version": corpus.manifest_version,
                "fingerprint": corpus.fingerprint,
                "documents": len(corpus.docs),
                "queries": len(corpus.queries),
                "slice_counts": dict(corpus.slice_counts),
                "provenance": corpus.provenance,
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "cpu_count": os.cpu_count(),
            },
            "vlm": {
                "model_chain": list(vlm.models),
                "model_served": vlm.model_served,
                "cache_mode": "live" if args.live else "replay",
                "calls": dict(vlm.calls),
                "usage_totals": dict(vlm.usage_totals),
            },
            "wall_time_s": round(time.perf_counter() - started, 3),
        }
    )

    blockers: list[str] = artifact.setdefault("blockers", [])
    if artifact["acceptance"].get("all_queries_scored") is not True:
        blockers.append("not every query was scored on every system")
    if artifact["acceptance"].get("no_forbidden_delivery") is not True:
        blockers.append("forbidden material was delivered")
    if artifact["acceptance"].get("pinned_probe_attribution_v3") is not True:
        blockers.append("pinned probe did not attribute to the v3 cut")

    summary = {
        system: {
            "task_success_rate": metrics["task_success_rate"],
            "page_hit_rate": metrics["page_hit_rate"],
            "mrr": metrics["mrr"],
            "danger_counts": metrics["danger_counts"],
        }
        for system, metrics in artifact["metrics"].items()
    }
    print(json.dumps({"decision": artifact["decision"]["outcome"], "systems": summary}, indent=2))

    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(artifact, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.output}")
    return 1 if blockers else 0


async def _run_benchmark(
    *, factory, service, corpus, tmp_path, vlm, dense_specs, index_dir, live: bool,
    ablations: bool = False,
) -> dict:
    ws = await seed_workspace(
        factory=factory,
        service=service,
        corpus=corpus,
        workspace_id="ws-pr81a-bench",
        source_root=tmp_path / "source-store",
    )
    render_store = PageRenderStore(tmp_path / "renders")

    # -- visual generations for the v3 cut --------------------------------
    embedders = {"hash": HashEmbedder()}
    dense_systems: list[str] = []
    index_files: dict[str, dict[str, str]] = {}
    build_timings: dict[str, float] = {}
    v3_indexes: dict[str, VisualIndex] = {}
    for name, embedder in dense_specs:
        t0 = time.perf_counter()
        index = build_visual_index(ws, render_store, embedder)
        build_timings[f"cold_build_{name}_v3_s"] = round(time.perf_counter() - t0, 3)
        v3_indexes[name] = index
        embedders[name] = embedder
        dense_systems.append(f"visual-dense:{embedder.identity}")
        index_files[name] = {}
        if live:
            path = index_dir / f"pr81a-visual-index-{name}-v3.npz"
            index.save(path)
            index_files[name]["v3"] = path.name

    scores: list = []
    lane_timings: dict[str, list[float]] = {}

    async def phase(systems, ctx, queries, *, suffix: str = "") -> None:
        from dataclasses import replace as _replace

        for system_id, embedder in systems:
            for query in queries:
                t0 = time.perf_counter()
                evidence = await run_lane(system_id, ctx, query, embedder=embedder)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                if suffix:
                    evidence = _replace(evidence, system_id=f"{evidence.system_id}{suffix}")
                lane_timings.setdefault(evidence.system_id, []).append(elapsed_ms)
                scores.append(score_query(query, evidence))

    # -- phase A: baseline + pre-revision (v3 cut, no denies) --------------
    ctx = LaneContext(
        workspace=ws,
        render_store=render_store,
        vlm=vlm,
        visual_index=v3_indexes[dense_specs[0][0]],
        visual_indexes={embedders[n].identity: v3_indexes[n] for n, _ in dense_specs},
        expected_revisions={"doc-rev-01": "v3"},
    )
    await resolve_phase_authorization(ctx)
    phase_a_queries = _phase_queries(corpus, phases=("baseline", "pre_revision")) + [
        q for q in corpus.queries if q.profile == "allowed"
    ]
    systems_a = [
        (B1_SYSTEM, None),
        (B2_SYSTEM, None),
        *[(f"visual-dense:{embedders[n].identity}", embedders[n]) for n, _ in dense_specs],
        (V2_SYSTEM, embedders[dense_specs[0][0]]),
    ]
    ablation_systems = []
    if ablations:
        primary = embedders[dense_specs[0][0]]
        ablation_systems = [
            (V2_TEXT_SYSTEM, primary),
            (V2_JOINT_SYSTEM, primary),
            (V2_UNION_SYSTEM, primary),
        ]
    systems_a = systems_a + ablation_systems
    await phase(systems_a, ctx, phase_a_queries)

    # -- phase B: denied + high assurance ----------------------------------
    policy = QueryPolicyService(factory, service, workspace_id=ws.workspace_id)
    await policy.deny_domain("restricted")
    ha_publication = await publish_high_assurance_partition(ws)

    denied_queries = [
        q for q in corpus.queries if q.profile == "denied" and q.expectation == "answer"
    ]
    no_delivery_queries = [
        q for q in corpus.queries if q.expectation == "no_delivery"
    ]
    ctx.authorization = None
    await resolve_phase_authorization(ctx)
    await phase(systems_a, ctx, denied_queries)
    await phase(systems_a, ctx, no_delivery_queries)

    # high assurance probes on the same authz slice
    ha_index = VisualIndex.partition_from(v3_indexes[dense_specs[0][0]], ["general"])
    ctx_ha = LaneContext(
        workspace=ws,
        render_store=render_store,
        vlm=vlm,
        assurance="high",
        visual_index=ha_index,
        visual_indexes={embedders[dense_specs[0][0]].identity: ha_index},
    )
    await resolve_phase_authorization(ctx_ha)
    ha_systems = [
        (B1_SYSTEM, None),
        (f"visual-dense:{embedders[dense_specs[0][0]].identity}", embedders[dense_specs[0][0]]),
    ]
    if ablations:
        # the promoted route itself, under the partitioned publication
        # and the partitioned visual generation
        ha_systems.append((V2_SYSTEM, embedders[dense_specs[0][0]]))
    await phase(ha_systems, ctx_ha, denied_queries + no_delivery_queries, suffix=":ha")
    del ha_publication

    # -- restore authorization, revise, measure update amplification -------
    await policy.allow_domain("restricted")
    renders_before = render_store.stats()["rendered"]
    t0 = time.perf_counter()
    await revise_document(ws, "doc-rev-01", "v4")
    revision_ms = (time.perf_counter() - t0) * 1000
    renders_after = render_store.stats()["rendered"]

    ctx.authorization = None
    v4_indexes: dict[str, VisualIndex] = {}
    for name, embedder in dense_specs:
        t1 = time.perf_counter()
        v4_indexes[name] = build_visual_index(ws, render_store, embedder)
        build_timings[f"rebuild_{name}_v4_s"] = round(time.perf_counter() - t1, 3)
        if live:
            path = index_dir / f"pr81a-visual-index-{name}-v4.npz"
            v4_indexes[name].save(path)
            index_files[name]["v4"] = path.name

    # -- phase C: post-revision on the new cut -----------------------------
    ctx.visual_index = v4_indexes[dense_specs[0][0]]
    ctx.visual_indexes = {embedders[n].identity: v4_indexes[n] for n, _ in dense_specs}
    ctx.expected_revisions = {"doc-rev-01": "v4"}
    await resolve_phase_authorization(ctx)
    post_queries = _phase_queries(corpus, phases=("post_revision",))
    await phase(systems_a, ctx, post_queries)

    # -- phase D: pinned probe on the old cut ------------------------------
    ctx_pinned = LaneContext(
        workspace=ws,
        render_store=render_store,
        vlm=vlm,
        pinned=True,
        pinned_publication_id=ws.pinned_publication.publication_set_id,
        visual_index=v3_indexes[dense_specs[0][0]],
        visual_indexes={embedders[n].identity: v3_indexes[n] for n, _ in dense_specs},
        expected_revisions={"doc-rev-01": "v3"},
    )
    await resolve_phase_authorization(ctx_pinned)
    pinned_queries = _phase_queries(corpus, phases=("pinned_pre_revision",))
    await phase(systems_a, ctx_pinned, pinned_queries)

    # -- warm visual query probe (index search only, no VLM in the path) ---
    warm_ms: list[float] = []
    primary_index = v4_indexes[dense_specs[0][0]]
    primary_embedder = embedders[dense_specs[0][0]]
    for query in corpus.queries:
        vector = primary_embedder.embed_text(query.text)
        t0 = time.perf_counter()
        primary_index.search(vector)
        warm_ms.append((time.perf_counter() - t0) * 1000)

    # -- score, re-score for determinism, aggregate -------------------------
    def _all_scores() -> list:
        return list(scores)

    first = [s.to_dict() for s in _all_scores()]
    second = [s.to_dict() for s in _all_scores()]
    deterministic = first == second

    metrics = aggregate_metrics(scores)
    danger_totals: dict[str, int] = {}
    for score in scores:
        if score.danger:
            danger_totals[score.danger] = danger_totals.get(score.danger, 0) + 1

    no_delivery_summary = {
        system: {
            "ok": m["no_delivery_required_ok"],
            "total": m["no_delivery_required_total"],
        }
        for system, m in metrics.items()
    }

    warm_hits = render_store.stats()["cache_hits"]
    economics = {
        "pages_rendered": render_store.stats()["rendered"],
        "cache_hits": warm_hits,
        "render_bytes": render_store.stats()["bytes_written"],
        "cached_bytes": render_store.stats()["cached_bytes"],
        "avg_render_bytes_per_page": round(
            render_store.stats()["bytes_written"] / max(render_store.stats()["rendered"], 1)
        ),
        "cold_render_ms_mean": round(
            render_store.stats()["cold_ms_total"] / max(render_store.stats()["rendered"], 1), 2
        ),
        "warm_render_ms_mean": round(
            render_store.stats()["warm_ms_total"] / max(warm_hits, 1), 2
        ),
        "not_admitted_skips": render_store.stats()["not_admitted"],
        "render_failures": render_store.stats()["failures"],
        "revision_update_ms": round(revision_ms, 1),
        "revision_renders": renders_after - renders_before,
        "embedding_bytes_per_model": {
            name: int(index.matrix.nbytes) for name, index in v4_indexes.items()
        },
        "avg_embedding_bytes_per_page": round(
            max(
                (index.matrix.nbytes / max(len(index.entries), 1))
                for index in v4_indexes.values()
            )
        ),
        "build_timings": build_timings,
        "visual_query_ms_p50": round(
            statistics.median(
                ms
                for system_id, runs in lane_timings.items()
                if system_id.startswith("visual-dense:")
                for ms in runs
            ),
            2,
        ) if any(s.startswith("visual-dense:") for s in lane_timings) else None,
        "warm_query_ms_p50": round(statistics.median(warm_ms), 2),
        "warm_query_ms_max": round(max(warm_ms), 2) if warm_ms else None,
        "lane_timings_ms": {
            system_id: {
                "p50": round(statistics.median(runs), 2),
                "n": len(runs),
            }
            for system_id, runs in sorted(lane_timings.items())
        },
        "admitted_pages": len(v4_indexes[dense_specs[0][0]].entries),
        "index_files": index_files,
    }

    expected_pairs = (
        len(corpus.queries) * (3 + len(dense_specs) + len(ablation_systems))  # B1, B2, V2 + dense + ablations
        + (len(denied_queries) + len(no_delivery_queries)) * len(ha_systems)  # HA lanes
    )
    actual_pairs = len(scores)
    pinned_ok = all(
        s.detail.get("delivered_revision") == "v3"
        for s in scores
        if s.query_id == "q32" and s.system_id in (B1_SYSTEM, B2_SYSTEM)
    )

    acceptance = {
        "all_queries_scored": actual_pairs == expected_pairs,
        "expected_scored_pairs": expected_pairs,
        "actual_scored_pairs": actual_pairs,
        "scoring_deterministic": deterministic,
        "no_forbidden_delivery": danger_totals.get("forbidden_delivered", 0) == 0,
        "no_stale_delivery": danger_totals.get("stale_revision_delivered", 0) == 0,
        "pinned_probe_attribution_v3": pinned_ok,
        "revision_probe_new_cut": any(
            s.detail.get("delivered_revision") == "v4"
            for s in scores
            if s.phase == "post_revision"
        ),
    }

    decision = evaluate_decision(
        metrics,
        danger_totals=danger_totals,
        economics=economics,
    )

    per_query = {}
    for score in scores:
        per_query.setdefault(score.query_id, {})[score.system_id] = score.to_dict()

    return {
        "corpus_context": {
            "workspace_id": ws.workspace_id,
            "publication_sets": [p.publication_set_id for p in (ws.pinned_publication, ws.publication) if p],
            "visual_generations": {
                name: {"v3": v3_indexes[name].generation_id, "v4": v4_indexes[name].generation_id}
                for name in v3_indexes
            },
            "systems": {
                B1_SYSTEM: {"kind": "baseline", "evidence": "page oracle text"},
                B2_SYSTEM: {"kind": "baseline", "evidence": "lexically selected page render"},
                **{
                    f"visual-dense:{embedders[n].identity}": {
                        "kind": "dense visual",
                        "model": embedders[n].identity,
                        "evidence": "visually selected page render",
                    }
                    for n, _ in dense_specs
                },
                V2_SYSTEM: {
                    "kind": "hybrid rerank",
                    "reranker": "hosted VLM contact sheet",
                    "evidence": "rerank-selected page render",
                },
                **(
                    {
                        V2_TEXT_SYSTEM: {
                            "kind": "hybrid rerank ablation",
                            "evidence": "rerank-selected page oracle text (answer-modality ablation)",
                        },
                        V2_JOINT_SYSTEM: {
                            "kind": "hybrid rerank ablation",
                            "evidence": "rerank-selected page render + oracle text",
                        },
                        V2_UNION_SYSTEM: {
                            "kind": "hybrid ablation",
                            "evidence": "lexical-ordered candidate union, no rerank (rerank ablation)",
                        },
                    }
                    if ablations
                    else {}
                ),
            },
        },
        "metrics": metrics,
        "danger_totals": danger_totals,
        "no_delivery": no_delivery_summary,
        "economics": economics,
        "acceptance": acceptance,
        "blockers": [],
        "decision": decision,
        "per_query": per_query,
        "repeatability": {
            "scoring_deterministic": deterministic,
            "method": "scores serialized twice and compared byte-wise",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
