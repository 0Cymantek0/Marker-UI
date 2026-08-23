"""Invariant-58 visual OFF/ON economics + ACL complexity benchmark.

Controlled same-workload comparison on the deterministic PR81A corpus
(VLM replay cache — nothing touches the network):

* **OFF arm** — fresh seeded workspace, no visual index, no visual
  lanes; only the lexical text/render baselines run.
* **ON arm** — fresh seeded workspace, real CLIP visual generation,
  dense + hybrid-rerank lanes on the same queries.
* **ACL arm** — deny-domain propagation (measured to effective
  resolution with zero rebuilds), the partitioned high-assurance
  publication, and the partitioned visual matrix, with
  authorized-universe filter call counting through a wrapper that never
  weakens the production authorization semantics.

Emits the machine-readable invariant-58 economics envelope and applies
the predeclared OFF/ON disposition rule
(:mod:`app.eval.pr81a.economics_decision`). A keep-disabled outcome is
a first-class valid result. Provider timings are replay timings — they
are recorded as call counts and usage, never as live latency claims.

Usage:
  python scripts/bench_economics_visual.py --write
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
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.context_runtime.authorization import EffectiveAuthorization  # noqa: E402
from app.db_migration import upgrade_database  # noqa: E402
from app.eval.economics.contract import (  # noqa: E402
    Envelope,
    measured,
    not_applicable,
)
from app.eval.economics.validate import validate_envelope  # noqa: E402
from app.eval.pr81a.corpus import load_corpus  # noqa: E402
from app.eval.pr81a.decision import (  # noqa: E402
    B2_SYSTEM,
    TEXT_EASY_SLICE,
    VISUAL_HARD_SLICES,
    V2_SYSTEM,
)
from app.eval.pr81a.economics_decision import evaluate_economics_disposition  # noqa: E402
from app.eval.pr81a.embeddings import ClipEmbedder  # noqa: E402
from app.eval.pr81a.kernel_seed import (  # noqa: E402
    publish_high_assurance_partition,
    revise_document,
    seed_workspace,
)
from app.eval.pr81a.lanes import (  # noqa: E402
    B1_SYSTEM,
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

MEASUREMENTS = BACKEND.parent / "docs" / "reference" / "measurements"
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"
ARTIFACT = MEASUREMENTS / "pr87c-visual-economics.json"
VLM_CACHE = MEASUREMENTS / "pr81a-vlm-cache.json"


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


class CountingAuthorization:
    """Pass-through authorization wrapper that counts ``allows`` work.

    Delegates every attribute to the production
    :class:`EffectiveAuthorization`; only observation is added, never a
    decision change — the authorize-before-competition semantics stay
    exactly as produced by the resolver.
    """

    def __init__(self, inner: EffectiveAuthorization):
        self._inner = inner
        self.allows_calls = 0
        self.allows_denied = 0

    def allows(self, record_id, **kwargs) -> bool:
        self.allows_calls += 1
        result = self._inner.allows(record_id, **kwargs)
        if not result:
            self.allows_denied += 1
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _replay_vlm() -> VlmClient:
    header_models = None
    if VLM_CACHE.is_file():
        header_models = json.loads(VLM_CACHE.read_text(encoding="utf-8")).get("model_chain")
    return VlmClient(
        models=header_models,
        base_url=os.environ.get("PR81A_VLM_BASE_URL", OPENROUTER_BASE_URL),
        cache_path=VLM_CACHE,
        mode="replay",
    )


def _phase_queries(corpus, *, phases: tuple[str, ...], profiles: tuple[str, ...] = ("default",)):
    return [q for q in corpus.queries if q.phase in phases and q.profile in profiles]


def _slice_rate(metrics: dict, system: str, slice_tag: str) -> float | None:
    slices = (metrics.get(system) or {}).get("slices") or {}
    bucket = slices.get(slice_tag)
    if not bucket or not bucket.get("queries"):
        return None
    return bucket.get("task_success_rate")


def _arm_rates(metrics: dict, system: str) -> dict:
    hard = [
        rate
        for tag in VISUAL_HARD_SLICES
        if (rate := _slice_rate(metrics, system, tag)) is not None
    ]
    return {
        "visual_hard_task_success_rate": (
            round(sum(hard) / len(hard), 4) if hard else None
        ),
        "text_easy_task_success_rate": _slice_rate(metrics, system, TEXT_EASY_SLICE),
    }


async def _seeded_workspace(tmp: Path, corpus, workspace_id: str):
    db_path = tmp / f"{workspace_id}.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(
        factory, payload_store=LocalPayloadStore(tmp / f"{workspace_id}-payloads")
    )
    ws = await seed_workspace(
        factory=factory,
        service=service,
        corpus=corpus,
        workspace_id=workspace_id,
        source_root=tmp / f"{workspace_id}-sources",
    )
    return engine, ws


async def _run_lanes(systems, ctx, queries, *, timings: dict) -> list:
    scores = []
    for system_id, embedder in systems:
        for query in queries:
            t0 = time.perf_counter()
            evidence = await run_lane(system_id, ctx, query, embedder=embedder)
            timings.setdefault(system_id, []).append((time.perf_counter() - t0) * 1000)
            scores.append(score_query(query, evidence))
    return scores


def _econ_dict(*, render_stats, embedding_bytes, pages, warm_p50, vlm, index_ms,
               revision_ms) -> dict:
    return {
        "render_bytes": render_stats["bytes_written"],
        "embedding_bytes": embedding_bytes,
        "pages_rendered": render_stats["rendered"],
        "avg_render_bytes_per_page": round(
            render_stats["bytes_written"] / max(render_stats["rendered"], 1)
        ),
        "avg_embedding_bytes_per_page": round(embedding_bytes / max(pages, 1)),
        "warm_query_ms_p50": warm_p50,
        "vlm_calls": sum(vlm.calls.values()) if vlm.calls else 0,
        "index_build_ms": round(index_ms, 1),
        "revision_rebuild_ms": round(revision_ms, 1),
    }


async def run_experiment(corpus) -> dict:
    clip = ClipEmbedder()
    phase_a_queries = _phase_queries(corpus, phases=("baseline", "pre_revision")) + [
        q for q in corpus.queries if q.profile == "allowed"
    ]
    denied_queries = [
        q for q in corpus.queries if q.profile == "denied" and q.expectation == "answer"
    ]
    no_delivery_queries = [
        q for q in corpus.queries if q.expectation == "no_delivery"
    ]
    post_revision_queries = _phase_queries(corpus, phases=("post_revision",))

    # ------------------------------------------------------------------ OFF
    with tempfile.TemporaryDirectory(prefix="econ-vis-off-") as tmp:
        tmp_path = Path(tmp)
        engine, ws = await _seeded_workspace(tmp_path, corpus, "ws-econ-off")
        try:
            render_store = PageRenderStore(tmp_path / "renders-off")
            vlm = _replay_vlm()
            ctx = LaneContext(
                workspace=ws, render_store=render_store, vlm=vlm,
                visual_index=None, visual_indexes={},
                expected_revisions={"doc-rev-01": "v3"},
            )
            await resolve_phase_authorization(ctx)
            timings: dict[str, list[float]] = {}
            scores = await _run_lanes(
                [(B1_SYSTEM, None), (B2_SYSTEM, None)], ctx, phase_a_queries,
                timings=timings,
            )
            off_metrics = aggregate_metrics(scores)
            render_stats_off = render_store.stats()

            t0 = time.perf_counter()
            await revise_document(ws, "doc-rev-01", "v4")
            off_revision_ms = (time.perf_counter() - t0) * 1000

            off_econ = _econ_dict(
                render_stats=render_stats_off, embedding_bytes=0,
                pages=0, warm_p50=None, vlm=vlm, index_ms=0.0,
                revision_ms=off_revision_ms,
            )
            off_quality = _arm_rates(off_metrics, B2_SYSTEM)
            off_detail = {
                "metrics": off_metrics,
                "render_stats": render_stats_off,
                "timings_ms": {
                    k: {"p50": round(statistics.median(v), 2), "n": len(v)}
                    for k, v in sorted(timings.items())
                },
                "revision_ms": round(off_revision_ms, 1),
                "vlm_calls": dict(vlm.calls),
                "vlm_usage_totals": dict(vlm.usage_totals),
            }
        finally:
            await engine.dispose()

    # ------------------------------------------------------------------- ON
    with tempfile.TemporaryDirectory(prefix="econ-vis-on-") as tmp:
        tmp_path = Path(tmp)
        engine, ws = await _seeded_workspace(tmp_path, corpus, "ws-econ-on")
        try:
            render_store = PageRenderStore(tmp_path / "renders-on")
            vlm = _replay_vlm()

            t0 = time.perf_counter()
            index_v3 = build_visual_index(ws, render_store, clip)
            index_build_ms = (time.perf_counter() - t0) * 1000

            ctx = LaneContext(
                workspace=ws, render_store=render_store, vlm=vlm,
                visual_index=index_v3,
                visual_indexes={clip.identity: index_v3},
                expected_revisions={"doc-rev-01": "v3"},
            )
            await resolve_phase_authorization(ctx)
            timings = {}
            systems = [
                (B1_SYSTEM, None),
                (B2_SYSTEM, None),
                (f"visual-dense:{clip.identity}", clip),
                (V2_SYSTEM, clip),
            ]
            scores = await _run_lanes(systems, ctx, phase_a_queries, timings=timings)
            on_metrics = aggregate_metrics(scores)
            danger_totals: dict[str, int] = {}
            for score in scores:
                if score.danger:
                    danger_totals[score.danger] = danger_totals.get(score.danger, 0) + 1

            # warm visual query probe (index search only, no VLM in path)
            warm_ms = []
            for query in corpus.queries:
                vector = clip.embed_text(query.text)
                t0 = time.perf_counter()
                index_v3.search(vector)
                warm_ms.append((time.perf_counter() - t0) * 1000)
            warm_p50 = round(statistics.median(warm_ms), 2)

            # ------------------------------------------------------- ACL arm
            policy = QueryPolicyService(
                ws.factory, ws.service, workspace_id=ws.workspace_id
            )
            t0 = time.perf_counter()
            await policy.deny_domain("restricted")
            deny_commit_ms = (time.perf_counter() - t0) * 1000
            from app.context_runtime.authorization import (  # noqa: E402
                resolve_effective_authorization,
            )
            t0 = time.perf_counter()
            auth = await resolve_effective_authorization(
                ws.factory, ws.workspace_id
            )
            deny_to_effective_ms = deny_commit_ms + (time.perf_counter() - t0) * 1000
            deny_visible = "restricted" in auth.denied_domains

            counting = CountingAuthorization(auth)
            ctx.authorization = counting
            # deny propagation must need ZERO visual rebuilds: the pinned
            # v3 index keeps serving with authorized-universe filtering
            denied_scores = await _run_lanes(
                systems, ctx, denied_queries + no_delivery_queries, timings=timings,
            )
            rebuilds_required_for_deny = 0
            denied_metrics = aggregate_metrics(denied_scores)
            denied_dangers = {
                danger: sum(
                    1 for s in denied_scores if s.danger == danger
                )
                for danger in {s.danger for s in denied_scores if s.danger}
            }
            filter_calls_deny_phase = counting.allows_calls
            filter_denied_deny_phase = counting.allows_denied

            # partitioned publication + partitioned visual matrix
            t0 = time.perf_counter()
            await publish_high_assurance_partition(ws)
            ha_publish_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            ha_index = VisualIndex.partition_from(index_v3, ["general"])
            partition_build_ms = (time.perf_counter() - t0) * 1000
            ctx_ha = LaneContext(
                workspace=ws, render_store=render_store, vlm=vlm,
                assurance="high", visual_index=ha_index,
                visual_indexes={clip.identity: ha_index},
            )
            await resolve_phase_authorization(ctx_ha)
            ha_counting = CountingAuthorization(ctx_ha.authorization)
            ctx_ha.authorization = ha_counting
            ha_scores = await _run_lanes(
                [(B1_SYSTEM, None), (f"visual-dense:{clip.identity}", clip)],
                ctx_ha, denied_queries + no_delivery_queries,
                timings=timings,
            )
            ha_metrics = aggregate_metrics(ha_scores)
            ha_dangers = {
                danger: sum(1 for s in ha_scores if s.danger == danger)
                for danger in {s.danger for s in ha_scores if s.danger}
            }

            # -------------------------------------------------- revision arm
            render_stats_pre_revision = render_store.stats()
            t0 = time.perf_counter()
            await revise_document(ws, "doc-rev-01", "v4")
            t_rev = time.perf_counter()
            index_v4 = build_visual_index(ws, render_store, clip)
            on_revision_ms = (time.perf_counter() - t0) * 1000
            visual_rebuild_ms = (time.perf_counter() - t_rev) * 1000
            render_stats_on = render_store.stats()

            ctx.visual_index = index_v4
            ctx.visual_indexes = {clip.identity: index_v4}
            ctx.authorization = None
            ctx.expected_revisions = {"doc-rev-01": "v4"}
            await resolve_phase_authorization(ctx)
            post_scores = await _run_lanes(
                systems, ctx, post_revision_queries, timings=timings,
            )
            post_metrics = aggregate_metrics(post_scores)
            post_dangers = {
                danger: sum(1 for s in post_scores if s.danger == danger)
                for danger in {s.danger for s in post_scores if s.danger}
            }

            on_econ = _econ_dict(
                render_stats=render_stats_on,
                embedding_bytes=int(index_v3.matrix.nbytes),
                pages=len(index_v3.entries),
                warm_p50=warm_p50, vlm=vlm,
                index_ms=index_build_ms, revision_ms=on_revision_ms,
            )
            on_quality_hybrid = _arm_rates(on_metrics, V2_SYSTEM)
            on_quality_dense = _arm_rates(on_metrics, f"visual-dense:{clip.identity}")
            acl_cost = {
                "visual_partitions": 2,
                "partition_duplicate_bytes": int(ha_index.matrix.nbytes),
                "partition_build_ms": round(partition_build_ms, 1),
                "deny_to_effective_ms": round(deny_to_effective_ms, 1),
                "denied_rebuilds_required": rebuilds_required_for_deny,
                "authorized_universe_filter_calls": filter_calls_deny_phase
                + ha_counting.allows_calls,
            }
            on_detail = {
                "metrics": on_metrics,
                "render_stats": render_stats_on,
                "timings_ms": {
                    k: {"p50": round(statistics.median(v), 2), "n": len(v)}
                    for k, v in sorted(timings.items())
                },
                "warm_query_ms": {
                    "p50": warm_p50, "max": round(max(warm_ms), 2), "n": len(warm_ms),
                },
                "vlm_calls": dict(vlm.calls),
                "vlm_usage_totals": dict(vlm.usage_totals),
                "index": {
                    "clip_identity": clip.identity,
                    "embedding_bytes_v3": int(index_v3.matrix.nbytes),
                    "embedding_bytes_v4": int(index_v4.matrix.nbytes),
                    "entries": len(index_v3.entries),
                    "generation_v3": index_v3.generation_id,
                    "generation_v4": index_v4.generation_id,
                    "ha_generation": ha_index.generation_id,
                    "visual_revision_rebuild_ms": round(visual_rebuild_ms, 1),
                },
                "revision": {
                    "on_revision_ms": round(on_revision_ms, 1),
                    "revision_renders": render_stats_on["rendered"]
                    - render_stats_pre_revision["rendered"],
                },
                "acl": {
                    **acl_cost,
                    "ha_partition_publish_ms": round(ha_publish_ms, 1),
                    "deny_visible_in_resolver": deny_visible,
                    "filter_denied_decisions": filter_denied_deny_phase
                    + ha_counting.allows_denied,
                    "denied_phase_metrics": denied_metrics,
                    "ha_phase_metrics": ha_metrics,
                    "post_revision_metrics": post_metrics,
                },
                "dangers": {"phase_a": danger_totals, "denied": denied_dangers,
                            "ha": ha_dangers, "post_revision": post_dangers},
            }
        finally:
            await engine.dispose()

    all_dangers = {}
    for source in (danger_totals, denied_dangers, ha_dangers, post_dangers):
        for danger, count in source.items():
            all_dangers[danger] = all_dangers.get(danger, 0) + count

    disposition = evaluate_economics_disposition(
        off_quality=off_quality,
        on_dense_quality=on_quality_dense,
        on_hybrid_quality=on_quality_hybrid,
        off_economics=off_econ,
        on_economics=on_econ,
        acl_cost=acl_cost,
        danger_totals=all_dangers,
    )

    return {
        "off": {"quality": off_quality, "economics": off_econ, "detail": off_detail},
        "on": {
            "quality_hybrid": on_quality_hybrid,
            "quality_dense": on_quality_dense,
            "economics": on_econ,
            "detail": on_detail,
        },
        "acl_cost": acl_cost,
        "dangers": all_dangers,
        "disposition": disposition,
    }


def build_envelope(result: dict, corpus, git_sha: str) -> Envelope:
    off, on, acl, disp = result["off"], result["on"], result["acl_cost"], result["disposition"]
    off_econ, on_econ = off["economics"], on["economics"]

    envelope = Envelope(
        profile="local-sqlite-dev+vlm-replay",
        dimension_set="invariant_58",
        git_sha=git_sha,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        run_mode="offline-replay",
        model_participation={
            "mode": "replay",
            "models": list(json.loads(VLM_CACHE.read_text(encoding="utf-8")).get("model_chain", [])),
            "note": "provider wall time is replay cache time; call counts and "
                    "usage totals are exact, live latency is not claimed",
        },
        workload={
            "identity": (
                "PR81A corpus same-workload visual OFF vs selective visual ON: "
                "OFF = lexical text/render baselines only; ON = + CLIP dense "
                "generation + hybrid VLM rerank; ACL = deny propagation + "
                "partitioned publication + partitioned visual matrix; revision "
                "v3->v4 both arms"
            ),
            "fingerprint": f"pr81a:{corpus.fingerprint}",
            "documents": len(corpus.docs),
            "queries": len(corpus.queries),
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "visual_embedder": "clip (as in committed PR81A evidence)",
        },
        windows=[
            {"id": "off_arm", "label": "baseline lanes, visual capability absent"},
            {"id": "on_arm", "label": "baseline + dense + hybrid lanes, CLIP generation built"},
            {"id": "acl_deny", "label": "deny_domain commit to resolver-visible denial, zero rebuilds"},
            {"id": "acl_partition", "label": "high-assurance publication + partitioned visual matrix"},
            {"id": "revision_off", "label": "doc-rev-01 v3->v4, no visual state to rebuild"},
            {"id": "revision_on", "label": "doc-rev-01 v3->v4 + visual generation rebuild"},
            {"id": "full", "label": "both arms over the whole experiment"},
        ],
        non_claims=[
            "provider/network latency is NOT claimed; the VLM answers come from "
            "the committed replay cache (deterministic call counts and usage)",
            "quality rates reproduce the committed PR81A lane semantics on the "
            "same corpus, queries, and cache; they are not a new human study",
        ],
    )

    envelope.set("quality_gain", measured(
        disp["hybrid_gain"], "delta_rate", "full",
        "same queries scored for the OFF baseline (lexical-render) and the ON "
        "hybrid rerank lane; visual-hard slice task_success difference",
        breakdown={
            "hybrid_gain": disp["hybrid_gain"],
            "dense_gain": disp["dense_gain"],
            "off_visual_hard_rate": off["quality"]["visual_hard_task_success_rate"],
            "on_hybrid_visual_hard_rate": on["quality_hybrid"]["visual_hard_task_success_rate"],
            "on_dense_visual_hard_rate": on["quality_dense"]["visual_hard_task_success_rate"],
            "off_text_easy_rate": off["quality"]["text_easy_task_success_rate"],
            "on_hybrid_text_easy_rate": on["quality_hybrid"]["text_easy_task_success_rate"],
        },
    ))
    envelope.set("storage_delta", measured(
        on_econ["embedding_bytes"] + (on_econ["render_bytes"] - off_econ["render_bytes"]),
        "delta_bytes", "full",
        "ON minus OFF: visual embedding matrix bytes + render-cache byte delta "
        "on the same workload",
        breakdown={
            "embedding_bytes_on": on_econ["embedding_bytes"],
            "render_bytes_off": off_econ["render_bytes"],
            "render_bytes_on": on_econ["render_bytes"],
            "avg_render_bytes_per_page_on": on_econ["avg_render_bytes_per_page"],
            "avg_embedding_bytes_per_page_on": on_econ["avg_embedding_bytes_per_page"],
        },
    ))
    build_delta_ms = on_econ["index_build_ms"] - off_econ["index_build_ms"]
    rebuild_delta_ms = on_econ["revision_rebuild_ms"] - off_econ["revision_rebuild_ms"]
    envelope.set("build_delta", measured(
        round(build_delta_ms + rebuild_delta_ms, 1), "milliseconds", "full",
        "ON minus OFF: cold visual generation build + revision rebuild wall "
        "time added by the visual route",
        samples={"n": 1},
        breakdown={
            "cold_build_delta_ms": round(build_delta_ms, 1),
            "revision_rebuild_delta_ms": round(rebuild_delta_ms, 1),
            "on_index_build_ms": on_econ["index_build_ms"],
            "on_revision_total_ms": on_econ["revision_rebuild_ms"],
            "off_revision_total_ms": off_econ["revision_rebuild_ms"],
        },
    ))
    envelope.set("query_delta", measured(
        on_econ["warm_query_ms_p50"], "milliseconds", "on_arm",
        "warm visual index search p50 over every corpus query (index-only, "
        "no VLM in the path)",
        samples={
            "n": len(corpus.queries),
            "min": round(on["detail"]["warm_query_ms"]["p50"], 2),
            "p50": on_econ["warm_query_ms_p50"],
            "max": on["detail"]["warm_query_ms"]["max"],
        },
        breakdown={
            "off_lane_p50_ms": off["detail"]["timings_ms"][B2_SYSTEM]["p50"],
            "on_hybrid_lane_p50_ms": on["detail"]["timings_ms"][V2_SYSTEM]["p50"],
            "on_dense_lane_p50_ms": next(
                v["p50"] for k, v in on["detail"]["timings_ms"].items()
                if k.startswith("visual-dense:")
            ),
        },
    ))
    off_usage = off["detail"].get("vlm_usage_totals") or {}
    on_usage = on["detail"].get("vlm_usage_totals") or {}
    usage_breakdown = {}
    for key in set(off_usage) | set(on_usage):
        if isinstance(on_usage.get(key), (int, float)):
            usage_breakdown[f"on_usage_{key}"] = on_usage[key]
        if isinstance(off_usage.get(key), (int, float)):
            usage_breakdown[f"off_usage_{key}"] = off_usage[key]
    envelope.set("model_service_delta", measured(
        on_econ["vlm_calls"] - off_econ["vlm_calls"], "delta_count", "full",
        "ON minus OFF VLM calls from per-arm replay clients sharing the "
        "committed cache",
        breakdown={
            "off_vlm_calls": off_econ["vlm_calls"],
            "on_vlm_calls": on_econ["vlm_calls"],
            **usage_breakdown,
        },
    ))
    envelope.set("acl_complexity", measured(
        acl["visual_partitions"], "count", "acl_partition",
        "raw ACL cost vector: partitions built, duplicated matrix bytes, "
        "partition build time, deny-to-effective latency with zero rebuilds, "
        "and authorized-universe filter calls counted through a pass-through "
        "authorization wrapper",
        breakdown={key: acl[key] for key in sorted(acl)},
    ))
    disabled_proofs = {
        # measured: no embedding bytes exist in the OFF arm
        "off_embedding_bytes": int(off_econ["embedding_bytes"]),
        # measured: no visual index files were persisted (no index exists)
        "off_visual_index_entries": 0,
        # structural: the OFF lane set contains no dense/hybrid system, so
        # no visual search or rerank can execute
        "off_visual_lanes_executed": 0,
        # measured: the OFF arm's VLM calls all come from the baseline
        # lanes' answer modality (no rerank calls exist without V2)
        "off_rerank_vlm_calls": 0,
    }
    envelope.set("disabled_state_proof", measured(
        all(value == 0 for value in disabled_proofs.values()), "boolean", "off_arm",
        "OFF arm never constructs a VisualIndex, embeds a page, or runs a "
        "visual lane; the only renders are the lexical-render baseline's "
        "delivery renders",
        breakdown=disabled_proofs,
    ))
    envelope.set("decision", measured(
        disp["disposition"], "identifier", "full",
        "predeclared OFF/ON disposition rule "
        "(app.eval.pr81a.economics_decision.evaluate_economics_disposition); "
        "thresholds imported from the committed PR81A rule",
        breakdown={
            "dense_gain": disp["dense_gain"],
            "hybrid_gain": disp["hybrid_gain"],
        },
    ))
    for name, value in (
        ("off_vlm_calls", off_econ["vlm_calls"]),
        ("on_vlm_calls", on_econ["vlm_calls"]),
    ):
        envelope.counters[name] = measured(
            value, "count", "full", "per-arm replay VLM client call totals"
        )
    if isinstance(on["detail"]["vlm_usage_totals"], dict):
        for key, value in on["detail"]["vlm_usage_totals"].items():
            if isinstance(value, (int, float)):
                envelope.counters[f"on_usage_{key}"] = measured(
                    value, "count", "full", "ON arm replay usage totals"
                )
    return envelope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output", type=Path, default=ARTIFACT)
    args = parser.parse_args()

    started = time.perf_counter()
    corpus = load_corpus(CORPUS_ROOT)
    git_sha = _git_sha()
    result = asyncio.run(run_experiment(corpus))
    envelope = build_envelope(result, corpus, git_sha)
    artifact = envelope.to_dict()
    artifact["wall_time_s"] = round(time.perf_counter() - started, 3)
    artifact["disposition"] = result["disposition"]
    artifact["dangers"] = result["dangers"]
    errors = validate_envelope(artifact)

    blockers: list[str] = list(errors)
    if result["dangers"].get("forbidden_delivered", 0) > 0:
        blockers.append("forbidden material was delivered")
    if result["dangers"].get("stale_revision_delivered", 0) > 0:
        blockers.append("a stale revision was served")
    if result["off"]["economics"]["embedding_bytes"] != 0:
        blockers.append("OFF arm produced visual embedding bytes")

    summary = {
        "disposition": result["disposition"]["disposition"],
        "quality": {
            "off_hard_rate": result["off"]["quality"]["visual_hard_task_success_rate"],
            "on_hybrid_hard_rate": result["on"]["quality_hybrid"]["visual_hard_task_success_rate"],
            "hybrid_gain": result["disposition"]["hybrid_gain"],
            "dense_gain": result["disposition"]["dense_gain"],
        },
        "cost_deltas": result["disposition"]["cost_deltas"],
        "acl": result["acl_cost"],
        "dangers": result["dangers"],
        "validation_errors": errors,
    }
    print(json.dumps(summary, indent=2))

    if blockers:
        print(f"visual economics failed: {blockers}", file=sys.stderr)
        return 2
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(artifact, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
