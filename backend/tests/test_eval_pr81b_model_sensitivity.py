"""PR81B model-sensitivity tests: cache-key audit, ablation lanes,
capability probe, and the pre-declared confirmation rule.

Lanes run on throwaway SQLite workspaces seeded with the real committed
corpus bytes; the VLM is a scripted fake transport. No network, no
credentials, no GPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.pr81a.corpus import load_corpus
from app.eval.pr81a.embeddings import HashEmbedder
from app.eval.pr81a.kernel_seed import (
    publish_high_assurance_partition,
    seed_workspace,
)
from app.eval.pr81a.lanes import (
    B2_SYSTEM,
    V2_JOINT_SYSTEM,
    V2_SYSTEM,
    V2_TEXT_SYSTEM,
    V2_UNION_SYSTEM,
    LaneContext,
    build_visual_index,
    build_visual_index_high_assurance,
    resolve_phase_authorization,
    run_lane,
)
from app.eval.pr81a.scoring import score_query
from app.eval.pr81a.visual_store import PageRenderStore
from app.eval.pr81a.vlm import (
    ANSWER_SYSTEM_PROMPT,
    RERANK_SYSTEM_PROMPT,
    VlmClient,
    _image_part,
    cache_key,
)
from app.eval.pr81b.decision import (
    CEILING_FLOOR,
    MATERIAL_MARGIN,
    evaluate_confirmation,
    evaluate_model,
)
from app.eval.pr81b.probe import PROBE_CASES, run_capability_probe
from app.services.query_policy import QueryPolicyService

BACKEND = Path(__file__).resolve().parent.parent
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"


def _ok_body(content: str) -> str:
    return json.dumps(
        {
            "model": "fake/vlm",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }
    )


class FakeTransport:
    """Deterministic transport: canned bodies consumed in order."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        return 200, self.bodies.pop(0)


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
        return 200, _ok_body(body)


def _client(tmp_path, transport) -> VlmClient:
    return VlmClient(
        ["fake/model"],
        transport=transport,
        cache_path=tmp_path / "vlm-cache.json",
        mode="live",
        sleep=lambda _: None,
    )


async def _make_ctx(env, tmp_path, vlm, **overrides) -> LaneContext:
    factory, _store, service = env
    corpus = load_corpus(CORPUS_ROOT)
    ws = await seed_workspace(
        factory=factory,
        service=service,
        corpus=corpus,
        workspace_id="ws-pr81b-tests",
        source_root=tmp_path / "source-store",
    )
    render_store = PageRenderStore(tmp_path / "renders")
    ctx = LaneContext(workspace=ws, render_store=render_store, vlm=vlm, **overrides)
    await resolve_phase_authorization(ctx)
    return ctx


def _render_real_page(tmp_path, doc_id: str, page_number: int) -> bytes:
    corpus = load_corpus(CORPUS_ROOT)
    doc = corpus.doc(doc_id)
    revision = doc.current
    store = PageRenderStore(tmp_path / f"renders-{doc_id}")
    rendered = store.render(
        f"sha256:{revision.pdf_sha256}",
        page_number - 1,
        revision.pdf_path,
        admitted=True,
    )
    return rendered.path.read_bytes()


# ---------------------------------------------------------------------------
# cache-key collision audit
# ---------------------------------------------------------------------------


class TestCacheKeyAudit:
    def test_real_page_renders_yield_distinct_keys(self, tmp_path):
        png1 = _render_real_page(tmp_path, "doc-fin-01", 1)
        png2 = _render_real_page(tmp_path, "doc-fin-01", 2)
        assert png1 != png2  # real renders, not placeholders
        question = [{"type": "text", "text": "Question: same words"}]
        key1 = cache_key("m", ANSWER_SYSTEM_PROMPT, [*question, _image_part(png1)])
        key2 = cache_key("m", ANSWER_SYSTEM_PROMPT, [*question, _image_part(png2)])
        assert key1 != key2

    def test_transcript_presence_changes_key(self, tmp_path):
        png = _render_real_page(tmp_path, "doc-ops-01", 1)
        image_only = [
            {"type": "text", "text": "Question: budget?"},
            _image_part(png),
        ]
        with_text = [
            {"type": "text", "text": "Question: budget?\n\nText transcript of the page:\nApproved 95000"},
            _image_part(png),
        ]
        assert (
            cache_key("m", ANSWER_SYSTEM_PROMPT, image_only)
            != cache_key("m", ANSWER_SYSTEM_PROMPT, with_text)
        )

    def test_answer_and_rerank_prompts_cannot_collide(self, tmp_path):
        png = _render_real_page(tmp_path, "doc-fin-01", 2)
        parts = [{"type": "text", "text": "Question: tallest bar?"}, _image_part(png)]
        assert (
            cache_key("m", ANSWER_SYSTEM_PROMPT, parts)
            != cache_key("m", RERANK_SYSTEM_PROMPT, parts)
        )

    def test_same_question_two_real_pages_two_answers(self, tmp_path):
        png1 = _render_real_page(tmp_path, "doc-fin-01", 1)
        png2 = _render_real_page(tmp_path, "doc-fin-01", 2)
        transport = FakeTransport([_ok_body('{"answer": "page-one"}'), _ok_body('{"answer": "page-two"}')])
        client = _client(tmp_path, transport)
        _, first = client.answer("same question", page_png=png1, page_text=None)
        _, second = client.answer("same question", page_png=png2, page_text=None)
        assert first["answer"] == "page-one"
        assert second["answer"] == "page-two"
        cache = json.loads((tmp_path / "vlm-cache.json").read_text(encoding="utf-8"))
        assert len(cache["responses"]) == 2


# ---------------------------------------------------------------------------
# ablation lanes
# ---------------------------------------------------------------------------


class TestAblationLanes:
    @pytest.mark.asyncio
    async def test_text_ablation_answers_from_transcript_only(self, payload_env, tmp_path):
        transport = ScriptedTransport(
            answers=['{"answer": "18.5"}'],
            reranks=['{"scores": {"A": 2, "B": 9, "C": 1}}'],
        )
        ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
        ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
        query = ctx.workspace.corpus.query("q15")
        evidence = await run_lane(V2_TEXT_SYSTEM, ctx, query, embedder=HashEmbedder())
        assert evidence.system_id == V2_TEXT_SYSTEM
        assert evidence.evidence_kind == "text_page"
        assert evidence.delivered_page == evidence.ranked_pages[0]
        answer_payload = transport.payloads[1]
        user = answer_payload["messages"][1]["content"]
        text_part = next(p for p in user if p["type"] == "text")
        assert "Text transcript of the page" in text_part["text"]
        assert not any(p["type"] == "image_url" for p in user)

    @pytest.mark.asyncio
    async def test_joint_ablation_gets_transcript_and_image(self, payload_env, tmp_path):
        transport = ScriptedTransport(
            answers=['{"answer": "18.5"}'],
            reranks=['{"scores": {"A": 2, "B": 9, "C": 1}}'],
        )
        ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
        ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
        query = ctx.workspace.corpus.query("q15")
        evidence = await run_lane(V2_JOINT_SYSTEM, ctx, query, embedder=HashEmbedder())
        assert evidence.system_id == V2_JOINT_SYSTEM
        assert evidence.evidence_kind == "image_text_page"
        answer_payload = transport.payloads[1]
        user = answer_payload["messages"][1]["content"]
        text_part = next(p for p in user if p["type"] == "text")
        assert "Text transcript of the page" in text_part["text"]
        assert any(p["type"] == "image_url" for p in user)

    @pytest.mark.asyncio
    async def test_union_only_never_calls_the_reranker(self, payload_env, tmp_path):
        transport = ScriptedTransport(
            answers=['{"answer": "18.5"}'],
            reranks=[],  # any rerank call would raise IndexError
        )
        ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
        ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
        query = ctx.workspace.corpus.query("q15")
        evidence = await run_lane(V2_UNION_SYSTEM, ctx, query, embedder=HashEmbedder())
        assert evidence.system_id == V2_UNION_SYSTEM
        # lexical order over the union: gold page first for this query
        assert evidence.delivered_page == ("doc-fin-01", 3)
        assert evidence.ranked_pages[0] == evidence.delivered_page
        assert len(transport.payloads) == 1  # answer call only
        user = transport.payloads[0]["messages"][1]["content"]
        assert not any(p["type"] == "text" and "Text transcript" in p["text"] for p in user)
        assert any(p["type"] == "image_url" for p in user)

    @pytest.mark.asyncio
    async def test_hybrid_answer_prompt_is_image_only(self, payload_env, tmp_path):
        # the promoted hybrid must keep its image-only answer step: the
        # text/joint lanes are the ablations, not the default
        transport = ScriptedTransport(
            answers=['{"answer": "18.5"}'],
            reranks=['{"scores": {"A": 9, "B": 2, "C": 1}}'],
        )
        ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
        ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, HashEmbedder())
        query = ctx.workspace.corpus.query("q15")
        evidence = await run_lane(V2_SYSTEM, ctx, query, embedder=HashEmbedder())
        assert evidence.evidence_kind == "image_page"
        answer_payload = transport.payloads[1]
        user = answer_payload["messages"][1]["content"]
        text_part = next(p for p in user if p["type"] == "text")
        assert "Text transcript" not in text_part["text"]
        assert any(p["type"] == "image_url" for p in user)

    @pytest.mark.asyncio
    async def test_denied_domain_blocks_ablation_lanes(self, payload_env, tmp_path):
        factory, _store, service = payload_env
        transport = ScriptedTransport(answers=['{"answer": null}'] * 4)
        ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
        embedder = HashEmbedder()
        ctx.visual_index = build_visual_index(ctx.workspace, ctx.render_store, embedder)
        policy = QueryPolicyService(factory, service, workspace_id=ctx.workspace.workspace_id)
        await policy.deny_domain("restricted")
        await resolve_phase_authorization(ctx)
        query = ctx.workspace.corpus.query("q34")  # no_delivery expectation
        for system_id in (V2_SYSTEM, V2_TEXT_SYSTEM, V2_JOINT_SYSTEM, V2_UNION_SYSTEM):
            evidence = await run_lane(system_id, ctx, query, embedder=embedder)
            score = score_query(query, evidence)
            assert score.retrieval != "no_delivery_violated", system_id
            assert score.danger != "forbidden_delivered", system_id

    @pytest.mark.asyncio
    async def test_hybrid_under_high_assurance_partition(self, payload_env, tmp_path):
        factory, _store, service = payload_env
        transport = ScriptedTransport(
            answers=['{"answer": "2026-09-15"}'],
            reranks=['{"scores": {"A": 9, "B": 2, "C": 1}}'],
        )
        ctx = await _make_ctx(payload_env, tmp_path, _client(tmp_path, transport))
        policy = QueryPolicyService(factory, service, workspace_id=ctx.workspace.workspace_id)
        await policy.deny_domain("restricted")
        await publish_high_assurance_partition(ctx.workspace)
        ctx.assurance = "high"
        await resolve_phase_authorization(ctx)
        ctx.visual_index = build_visual_index_high_assurance(
            ctx.workspace, ctx.render_store, HashEmbedder()
        )
        query = ctx.workspace.corpus.query("q35")  # payroll date from public doc
        evidence = await run_lane(V2_SYSTEM, ctx, query, embedder=HashEmbedder())
        assert evidence.delivered_page == ("doc-pub-03", 1)
        assert evidence.forbidden_source_delivered is False
        assert ("doc-sec-02", 1) not in evidence.ranked_pages


# ---------------------------------------------------------------------------
# capability probe
# ---------------------------------------------------------------------------


class TestCapabilityProbe:
    def test_probe_cases_carry_corpus_gold(self):
        corpus = load_corpus(CORPUS_ROOT)
        expected = {"fin01-bar-value": "4", "fin01-bar-region": "west", "ops01-budget": "95000"}
        for case in PROBE_CASES:
            doc = corpus.doc(case.doc_id)
            assert case.page_number <= doc.current.page_count
            nodes = doc.current.page(case.page_number)
            assert case.gold in nodes, case.case_id  # the page provably shows it
            assert case.normalized_gold == expected[case.case_id]

    def test_probe_grades_normalized_answers(self, tmp_path):
        corpus = load_corpus(CORPUS_ROOT)
        store = PageRenderStore(tmp_path / "renders")
        transport = FakeTransport(
            [_ok_body('{"answer": "4.0"}'), _ok_body('{"answer": "West "}'), _ok_body('{"answer": "$95,000"}')]
        )
        client = _client(tmp_path, transport)
        result = run_capability_probe(corpus, store, client)
        assert result["correct"] == 3
        assert result["passed"] is True
        # every case asked with an image and no transcript
        for payload in transport.payloads:
            user = payload["messages"][1]["content"]
            assert any(p["type"] == "image_url" for p in user)
            assert not any("Text transcript" in p.get("text", "") for p in user)

    def test_probe_records_null_and_failures_honestly(self, tmp_path):
        corpus = load_corpus(CORPUS_ROOT)
        store = PageRenderStore(tmp_path / "renders")
        transport = FakeTransport(
            [_ok_body('{"answer": null}'), _ok_body('{"answer": "East"}'), _ok_body("no json")]
        )
        client = _client(tmp_path, transport)
        result = run_capability_probe(corpus, store, client)
        assert result["correct"] == 0
        assert result["passed"] is False
        errors = [c["error"] for c in result["cases"]]
        assert "answer was null" in errors
        assert "no answer delivered" in errors or "answer does not normalize" in errors

    def test_probe_transport_error_is_recorded_not_guessed(self, tmp_path):
        corpus = load_corpus(CORPUS_ROOT)
        store = PageRenderStore(tmp_path / "renders")

        class FailingTransport:
            def __call__(self, payload):
                return 401, "no key"

        client = _client(tmp_path, FailingTransport())
        result = run_capability_probe(corpus, store, client)
        assert result["passed"] is False
        assert all("401" in (c["error"] or "") for c in result["cases"])


# ---------------------------------------------------------------------------
# confirmation rule
# ---------------------------------------------------------------------------

HARD_SLICES = (
    "chart.appearance",
    "chart.value_read",
    "table.cell_grid",
    "form.label_placement",
    "layout.column_bind",
)


def _system_metrics(hard: float, easy: float, *, extra: dict | None = None) -> dict:
    slices = {
        tag: {"queries": 2, "task_success": round(hard * 2), "task_success_rate": hard}
        for tag in HARD_SLICES
    }
    slices["text.easy_control"] = {
        "queries": 6, "task_success": round(easy * 6), "task_success_rate": easy
    }
    metrics = {
        "slices": slices,
        "no_delivery_required_ok": 1,
        "no_delivery_required_total": 1,
    }
    if extra:
        metrics.update(extra)
    return metrics


def _artifact(
    *,
    base_hard=0.65,
    hybrid_hard=0.95,
    easy=0.83,
    hybrid_easy=1.0,
    text_hard=0.95,
    joint_hard=0.95,
    union_hard=0.60,
    probe_passed=True,
    dangers=None,
    no_delivery_ok=1,
) -> dict:
    metrics = {
        "lexical-render": _system_metrics(base_hard, easy),
        "visual-hybrid-rerank": _system_metrics(
            hybrid_hard, hybrid_easy, extra={"no_delivery_required_ok": no_delivery_ok}
        ),
        "visual-hybrid-rerank-text": _system_metrics(text_hard, hybrid_easy),
        "visual-hybrid-rerank-joint": _system_metrics(joint_hard, hybrid_easy),
        "visual-hybrid-union-only": _system_metrics(union_hard, hybrid_easy),
    }
    return {
        "metrics": metrics,
        "danger_totals": dangers or {},
        "economics": {
            "avg_render_bytes_per_page": 55_000,
            "avg_embedding_bytes_per_page": 3_000,
            "warm_query_ms_p50": 0.14,
        },
        "capability_probe": {"passed": probe_passed, "correct": 3 if probe_passed else 0},
    }


class TestEvaluateModel:
    def test_holding_model_attributes_deltas(self):
        result = evaluate_model("kr/claude-sonnet-4.5", _artifact())
        assert result["holds"] is True
        assert result["per_model_outcome"] == "narrow_rerank_only"
        assert result["tier"] == "frontier"
        assert result["ablation_deltas"]["answer_vision_delta"] == 0.0
        assert result["ablation_deltas"]["rerank_delta"] == 0.35

    def test_ceiling_clause_holds_without_gain_margin(self):
        # a frontier model lifted the baseline to 0.90; gain 0.05 < 0.10
        # but the hybrid sits at 0.95 without regressing
        result = evaluate_model(
            "kr/claude-sonnet-4.5", _artifact(base_hard=0.90, hybrid_hard=0.95)
        )
        assert result["per_model_outcome"] == "experimental"
        assert result["ceiling_clause_applied"] is True
        assert result["holds"] is True

    def test_ceiling_clause_requires_floor(self):
        result = evaluate_model(
            "kr/claude-sonnet-4.5",
            _artifact(base_hard=0.50, hybrid_hard=0.55),
        )
        assert result["holds"] is False

    def test_probe_failure_blocks_holding(self):
        result = evaluate_model(
            "kr/claude-sonnet-4.5", _artifact(probe_passed=False)
        )
        assert result["capability_probe_passed"] is False
        assert result["holds"] is False

    def test_security_danger_blocks_holding(self):
        result = evaluate_model(
            "kr/claude-sonnet-4.5",
            _artifact(dangers={"forbidden_delivered": 1}),
        )
        assert result["security_dangers"] == {"forbidden_delivered": 1}
        assert result["holds"] is False

    def test_no_delivery_violation_blocks_holding(self):
        result = evaluate_model(
            "kr/claude-sonnet-4.5", _artifact(no_delivery_ok=0)
        )
        assert result["no_delivery_clean"] is False
        assert result["holds"] is False

    def test_decoy_confusion_is_quality_not_security(self):
        result = evaluate_model(
            "kr/claude-sonnet-4.5", _artifact(dangers={"decoy_confusion": 2})
        )
        assert result["security_dangers"] == {}
        assert result["holds"] is True


class TestEvaluateConfirmation:
    def test_confirmed_with_rerank_attribution(self):
        per_model = {
            m: _artifact()
            for m in (
                "kr/claude-sonnet-4.5",
                "kr/claude-haiku-4.5",
                "cx/gpt-5.6-luna",
                "free/bbl/gemini-3.0-flash",
            )
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "confirmed"
        assert result["attribution"] == "rerank_vision"
        assert len(result["holders"]) == 4
        assert "kr/claude-sonnet-4.5" in result["frontier_holders"]

    def test_confirmed_with_answer_vision_attribution(self):
        per_model = {
            m: _artifact(text_hard=0.55)
            for m in ("kr/claude-sonnet-4.5", "kr/claude-haiku-4.5", "cx/gpt-5.6-luna")
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "confirmed"
        assert result["attribution"] == "answer_vision"
        assert result["answer_vision_models"] == sorted(per_model)

    def test_retrieval_only_rescopes_the_claim(self):
        per_model = {
            m: _artifact(text_hard=0.95, union_hard=0.95)
            for m in ("kr/claude-sonnet-4.5", "kr/claude-haiku-4.5", "cx/gpt-5.6-luna")
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "confirmed"
        assert result["attribution"] == "retrieval_only"
        assert result["rerank_vision_models"] == []

    def test_few_holders_means_model_gated_experimental(self):
        per_model = {
            "kr/claude-sonnet-4.5": _artifact(),
            "free/bbl/gemini-3.0-flash": _artifact(),
            "free/opc/mimo-v2.5-free": _artifact(hybrid_hard=0.70),  # no gain
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "model_gated_experimental"
        assert len(result["holders"]) == 2
        assert len(result["frontier_holders"]) == 1

    def test_frontier_depth_required(self):
        per_model = {
            m: _artifact()
            for m in ("free/bbl/gemini-3.0-flash", "free/opc/mimo-v2.5-free")
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "model_gated_experimental"
        assert result["frontier_holders"] == []

    def test_zero_holders_means_do_not_promote(self):
        per_model = {
            m: _artifact(hybrid_hard=0.68)  # no gain anywhere
            for m in ("kr/claude-sonnet-4.5", "kr/claude-haiku-4.5", "cx/gpt-5.6-luna")
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "do_not_promote"
        assert result["attribution"] == "unattributed"

    def test_systemic_security_failure_overrides(self):
        per_model = {
            m: _artifact(dangers={"forbidden_delivered": 1})
            for m in ("kr/claude-sonnet-4.5", "kr/claude-haiku-4.5", "cx/gpt-5.6-luna")
        }
        result = evaluate_confirmation(per_model)
        assert result["outcome"] == "do_not_promote"

    def test_declared_rule_text_names_every_threshold(self):
        result = evaluate_confirmation({"kr/claude-sonnet-4.5": _artifact()})
        rule = result["declared_rule"]
        assert str(MATERIAL_MARGIN) in rule
        assert str(CEILING_FLOOR) in rule
        assert "frontier" in rule
