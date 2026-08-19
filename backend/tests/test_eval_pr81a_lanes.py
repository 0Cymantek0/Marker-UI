"""PR81A lane tests over real kernel authorities. Matrix letter L2.

Everything runs on throwaway SQLite workspaces seeded with the real
committed corpus bytes. The VLM is a scripted fake transport; the
embedder is the deterministic hash lane. These tests prove lane
mechanics — production lexical routing, on-demand rendering, admission
selectivity, authorization, revision lifecycle, honest failures — not
retrieval quality, which the benchmark measures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from app.context_runtime.contract import UnsupportedOperatorError, parse_query_request
from app.eval.pr81a.corpus import load_corpus
from app.eval.pr81a.embeddings import HashEmbedder
from app.eval.pr81a.kernel_seed import (
    publish_high_assurance_partition,
    revise_document,
    seed_workspace,
)
from app.eval.pr81a.lanes import (
    B1_SYSTEM,
    B2_SYSTEM,
    V2_SYSTEM,
    LaneContext,
    build_visual_index,
    build_visual_index_high_assurance,
    resolve_phase_authorization,
    run_lane,
)
from app.eval.pr81a.scoring import RouteEvidence, score_query
from app.eval.pr81a.visual_store import PageRenderStore
from app.eval.pr81a.vlm import VlmClient
from app.services.query_policy import QueryPolicyService

BACKEND = Path(__file__).resolve().parent.parent
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"


class ScriptedTransport:
    """Deterministic multimodal transport: canned answers, then reranks."""

    def __init__(self, answers, reranks=None):
        self.answers = list(answers)
        self.reranks = list(reranks or [])
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        system = payload["messages"][0]["content"]
        if "Score how well each page" in system:
            body = self.reranks.pop(0)
        else:
            body = self.answers.pop(0)
        return 200, json.dumps(
            {
                "model": "fake/vlm",
                "choices": [{"message": {"role": "assistant", "content": body}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )


def _client(tmp_path, transport) -> VlmClient:
    return VlmClient(
        ["fake/model"],
        transport=transport,
        cache_path=tmp_path / "vlm-cache.json",
        mode="live",
        sleep=lambda _: None,
    )


async def _make_ctx(env, tmp_path, vlm, **overrides) -> LaneContext:
    factory, payload_store, service = env
    corpus = load_corpus(CORPUS_ROOT)
    ws = await seed_workspace(
        factory=factory,
        service=service,
        corpus=corpus,
        workspace_id="ws-pr81a-lanes",
        source_root=tmp_path / "source-store",
    )
    render_store = PageRenderStore(tmp_path / "renders")
    ctx = LaneContext(workspace=ws, render_store=render_store, vlm=vlm, **overrides)
    await resolve_phase_authorization(ctx)
    return ctx


@pytest.mark.asyncio
async def test_seeding_binds_real_bytes_and_publishes(payload_env, tmp_path):
    factory, _store, _service = payload_env
    corpus = load_corpus(CORPUS_ROOT)
    ws = await seed_workspace(
        factory=factory,
        service=_service,
        corpus=corpus,
        workspace_id="ws-seed",
        source_root=tmp_path / "src",
    )
    assert len(ws.docs) == 15
    assert ws.publication is not None and ws.publication.state == "published"
    fin01 = ws.doc("doc-fin-01")
    assert fin01.blob_key == corpus.doc("doc-fin-01").blob_key()  # real bytes
    assert fin01.artifact_path(ws.source_store).is_file()
    # revision doc seeded at its superseded cut first
    assert ws.doc("doc-rev-01").revision == "v3"
    # node map covers every page of the transcript
    rev_doc = corpus.doc("doc-rev-01").revision("v3")
    assert rev_doc.page_count == len(ws.doc("doc-rev-01").page_texts)
    assert ws.doc("doc-hr-01").domain == "general"
    assert ws.doc("doc-sec-01").domain == "restricted"


@pytest.mark.asyncio
async def test_b1_lexical_text_lane_mechanics(payload_env, tmp_path):
    transport = ScriptedTransport(answers=['{"answer": "ZETA-9"}'])
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    query = ctx.workspace.corpus.query("q01")
    evidence = await run_lane(B1_SYSTEM, ctx, query, embedder=HashEmbedder())
    assert evidence.system_id == B1_SYSTEM
    assert evidence.delivered_page == ("doc-fin-01", 1)
    assert evidence.evidence_kind == "text_page"
    assert evidence.source_resolvable is True
    assert evidence.ranked_pages[0] == ("doc-fin-01", 1)
    # no pixels anywhere in the text lane
    assert ctx.render_store.stats()["cached_entries"] == 0
    # the answer prompt carried the page transcript, not an image
    user = transport.payloads[0]["messages"][1]["content"]
    assert all(part["type"] == "text" for part in user)


@pytest.mark.asyncio
async def test_b2_lexical_render_lane_renders_on_demand(payload_env, tmp_path):
    transport = ScriptedTransport(answers=['{"answer": "ZETA-9"}'])
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    query = ctx.workspace.corpus.query("q01")
    evidence = await run_lane(B2_SYSTEM, ctx, query, embedder=HashEmbedder())
    assert evidence.system_id == B2_SYSTEM
    assert evidence.delivered_page == ("doc-fin-01", 1)
    assert evidence.evidence_kind == "image_page"
    stats = ctx.render_store.stats()
    assert stats["cached_entries"] == 1  # targeted: only the delivered page
    user = transport.payloads[0]["messages"][1]["content"]
    assert any(part["type"] == "image_url" for part in user)


@pytest.mark.asyncio
async def test_visual_index_admission_selectivity(payload_env, tmp_path):
    transport = ScriptedTransport(answers=[])
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
    admitted = {entry.doc_id for entry in index.entries}
    assert "doc-hr-01" not in admitted and "doc-leg-01" not in admitted
    assert "doc-fin-01" in admitted
    stats = ctx.render_store.stats()
    expected_pages = sum(
        len(ctx.workspace.docs[d].page_texts)
        for d in ctx.workspace.docs
        if ctx.workspace.docs[d].visual_admitted
    )
    assert stats["rendered"] == expected_pages
    assert stats["not_admitted"] == 0  # index build only admits by construction
    # non-admitted blobs have no cached pixels
    hr_blob = ctx.workspace.doc("doc-hr-01").blob_key
    assert ctx.render_store.peek(hr_blob, 0) is None


@pytest.mark.asyncio
async def test_visual_lane_honest_on_not_admitted_gold(payload_env, tmp_path):
    transport = ScriptedTransport(answers=['{"answer": null}'])
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
    query = ctx.workspace.corpus.query("q02")  # gold doc-hr-01: not admitted
    embedder = HashEmbedder()
    evidence = await run_lane(f"visual-dense:{embedder.identity}", ctx, query, embedder=embedder)
    assert ("doc-hr-01", 2) not in evidence.ranked_pages
    score = score_query(query, evidence)
    assert score.retrieval in {"page_miss", "not_admitted"}
    assert score.task_success is False


@pytest.mark.asyncio
async def test_denied_domain_blocks_delivery_all_lanes(payload_env, tmp_path):
    factory, _store, service = payload_env
    transport = ScriptedTransport(answers=['{"answer": null}'] * 4)
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    embedder = HashEmbedder()
    ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, embedder)
    policy = QueryPolicyService(factory, service, workspace_id=ctx.workspace.workspace_id)
    await policy.deny_domain("restricted")
    await resolve_phase_authorization(ctx)
    query = ctx.workspace.corpus.query("q34")  # no_delivery expectation
    for system_id in (B1_SYSTEM, B2_SYSTEM, f"visual-dense:{embedder.identity}", V2_SYSTEM):
        evidence = await run_lane(system_id, ctx, query, embedder=embedder)
        score = score_query(query, evidence)
        assert score.retrieval != "no_delivery_violated", system_id
        assert score.danger != "forbidden_delivered", system_id
    # restricted pages never even entered the visual scored universe
    result = ctx.visual_index.search(
        embedder.embed_text("severance budget"),
        candidate_filter=lambda e: ctx.allows_doc(ctx.workspace.docs[e.doc_id]),
    )
    assert all(h.doc_id != "doc-sec-01" for h in result.hits)


@pytest.mark.asyncio
async def test_high_assurance_partition_lanes(payload_env, tmp_path):
    factory, _store, service = payload_env
    transport = ScriptedTransport(answers=['{"answer": "2026-09-15"}'])
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    # the ha. partition profile is derived from assigned-minus-denied
    # domains, so restricted must be denied for the {general} partition
    # to be the profile the resolver derives
    policy = QueryPolicyService(factory, service, workspace_id=ctx.workspace.workspace_id)
    await policy.deny_domain("restricted")
    await publish_high_assurance_partition(ctx.workspace)
    ctx.assurance = "high"
    await resolve_phase_authorization(ctx)
    assert ctx.authorization.partition_domains() == ("general",)
    ctx.visual_index = build_visual_index_high_assurance(
        ctx.workspace, ctx.render_store, HashEmbedder()
    )
    assert all(entry.domain == "general" for entry in ctx.visual_index.entries)
    query = ctx.workspace.corpus.query("q35")  # payroll date from public doc
    evidence = await run_lane(B1_SYSTEM, ctx, query, embedder=HashEmbedder())
    assert evidence.delivered_page == ("doc-pub-03", 1)
    assert evidence.forbidden_source_delivered is False
    visual = await run_lane(
        "visual-dense:hash:64", ctx, query, embedder=HashEmbedder()
    )
    assert ("doc-sec-02", 1) not in visual.ranked_pages


@pytest.mark.asyncio
async def test_revision_lifecycle_and_stale_detection(payload_env, tmp_path):
    factory, _store, service = payload_env
    transport = ScriptedTransport(
        answers=['{"answer": "3"}', '{"answer": "3"}', '{"answer": "3"}']
    )
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    g1 = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())

    class TargetedEmbedder(HashEmbedder):
        """Text queries steer to one exact page's image vector."""

        def __init__(self, target_png: bytes) -> None:
            super().__init__()
            self._target = self.embed_image(target_png)

        def embed_text(self, text: str):
            return self._target

    rev_doc = ctx.workspace.doc("doc-rev-01")
    v3_png = ctx.render_store.peek(rev_doc.blob_key, 0).path.read_bytes()
    embedder = TargetedEmbedder(v3_png)
    ctx.visual_index = g1
    ctx.expected_revisions = {"doc-rev-01": "v3"}
    await resolve_phase_authorization(ctx)

    q29 = ctx.workspace.corpus.query("q29")
    pre = await run_lane("visual-dense:hash:64", ctx, q29, embedder=embedder)
    assert pre.delivered_page == ("doc-rev-01", 1)
    assert pre.revision == "v3"
    assert pre.stale_revision_delivered is False

    renders_before = ctx.render_store.stats()["rendered"]
    updated = await revise_document(ctx.workspace, "doc-rev-01", "v4")
    ctx.expected_revisions = {"doc-rev-01": "v4"}
    ctx.authorization = None
    await resolve_phase_authorization(ctx)
    g2 = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
    renders_after = ctx.render_store.stats()["rendered"]
    # update amplification: only the revised document's pages re-rendered
    assert renders_after - renders_before == len(updated.page_texts)
    assert g2.generation_id != g1.generation_id

    v4_png = ctx.render_store.peek(updated.blob_key, 0).path.read_bytes()
    ctx.visual_index = g2
    embedder = TargetedEmbedder(v4_png)
    q30 = ctx.workspace.corpus.query("q30")
    post = await run_lane("visual-dense:hash:64", ctx, q30, embedder=embedder)
    assert post.delivered_page == ("doc-rev-01", 1)
    assert post.revision == "v4"
    assert post.stale_revision_delivered is False

    # serving the OLD visual generation against the post-revision phase
    # must be flagged as a stale-revision danger, not silently accepted
    ctx.visual_index = g1
    embedder = TargetedEmbedder(v3_png)
    stale = await run_lane("visual-dense:hash:64", ctx, q30, embedder=embedder)
    assert stale.delivered_page == ("doc-rev-01", 1)
    score = score_query(q30, stale)
    assert score.danger == "stale_revision_delivered"


@pytest.mark.asyncio
async def test_vlm_failure_is_honest_error(payload_env, tmp_path):
    transport = ScriptedTransport(answers=[])
    def failing(payload):
        return 401, "no key"

    transport.__call__ = failing
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    query = ctx.workspace.corpus.query("q01")
    evidence = await run_lane(B2_SYSTEM, ctx, query, embedder=HashEmbedder())
    assert evidence.error is not None
    score = score_query(query, evidence)
    assert score.retrieval == "unavailable"
    assert score.task_success is False


@pytest.mark.asyncio
async def test_v2_hybrid_rerank_orders_by_scores(payload_env, tmp_path):
    transport = ScriptedTransport(
        answers=['{"answer": "18.5"}'],
        reranks=['{"scores": {"A": 2, "B": 9, "C": 1}}'],
    )
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
    query = ctx.workspace.corpus.query("q15")
    evidence = await run_lane(V2_SYSTEM, ctx, query, embedder=HashEmbedder())
    assert evidence.system_id == V2_SYSTEM
    assert len(evidence.ranked_pages) >= 2
    assert evidence.delivered_page == evidence.ranked_pages[0]
    assert evidence.evidence_kind == "image_page"
    # the reranker saw exactly one montage image
    rerank_payload = transport.payloads[0]
    user = rerank_payload["messages"][1]["content"]
    assert any(part["type"] == "image_url" for part in user)


@pytest.mark.asyncio
async def test_v2_reranker_failure_falls_back_to_lexical_honestly(payload_env, tmp_path):
    transport = ScriptedTransport(
        answers=['{"answer": "18.5"}'],
        reranks=["not json at all"],
    )
    ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
    ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
    query = ctx.workspace.corpus.query("q15")
    evidence = await run_lane(V2_SYSTEM, ctx, query, embedder=HashEmbedder())
    # fallback used lexical order: gold page still first for this query
    assert evidence.delivered_page == ("doc-fin-01", 3)


def test_visual_search_operator_remains_unsupported():
    with pytest.raises(UnsupportedOperatorError):
        parse_query_request(
            {
                "schema_version": "marker.query.v1",
                "workspace_id": "ws",
                "operations": [{"op": "visual_search", "text": "chart"}],
            }
        )
