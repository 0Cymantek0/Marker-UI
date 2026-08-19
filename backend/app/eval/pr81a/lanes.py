"""PR81A route lanes: baseline, targeted rendering, and visual candidates.

Every lane answers one query over the same seeded kernel workspace and
returns a :class:`RouteEvidence` for scoring. The lanes differ only in
how they select the evidence page and what evidence they hand the same
VLM answerer — so the measured differences come from *evidence
selection*, not from different answerers:

* ``lexical-text`` (B1): production lexical search picks the page; the
  answerer sees that page's oracle text transcript. No pixels anywhere.
* ``lexical-render`` (B2): the same lexical ranking picks the page; the
  answerer sees that page's rendered image — the masterplan's strongest
  non-visual alternative ("text/structure plus top-page rendering").
* ``visual-dense:<model>`` (V1/V1b): the visual index ranks pages by
  image embedding; the answerer sees the top page's rendered image.
* ``visual-hybrid-rerank`` (V2): lexical and visual candidate pages are
  merged onto one contact sheet; a VLM reranker picks the evidence page
  before the same answerer runs.

Authorization is enforced before evidence selection everywhere: lexical
lanes run through the production executor (PR78 filtering), visual
lanes restrict the scored candidate universe through the effective
authorization, and the high-assurance visual lane queries a physically
partitioned index. A lane never converts an image-derived result into a
textual citation: delivered evidence carries the exact ``(doc_id,
page_number, revision, blob_key)`` identity of the page it used.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from typing import Sequence

from app.context_runtime.authorization import (
    EffectiveAuthorization,
    resolve_effective_authorization,
)
from app.context_runtime.contract import QUERY_SCHEMA_VERSION, parse_query_request
from app.context_runtime.executor import execute_query
from app.context_runtime.packets import EvidencePacket
from app.eval.pr81a.corpus import CorpusQuery
from app.eval.pr81a.kernel_seed import SeededDoc, SeededWorkspace
from app.eval.pr81a.scoring import RouteEvidence
from app.eval.pr81a.visual_index import VisualIndex, VisualPageEntry, VisualQueryBudget
from app.eval.pr81a.visual_store import PageRenderStore
from app.eval.pr81a.vlm import VlmClient

B1_SYSTEM = "lexical-text"
B2_SYSTEM = "lexical-render"
V2_SYSTEM = "visual-hybrid-rerank"
V_SYSTEM_PREFIX = "visual-dense"

LEXICAL_RANK_K = 8
VISUAL_RANK_K = 8
RERANK_CANDIDATES_PER_ROUTE = 3


@dataclass
class LaneContext:
    """Everything a lane may use for one benchmark phase.

    ``authorization`` is resolved once per phase by the runner; the
    production executor re-resolves it per operation internally, and the
    lanes use the phase view only to restrict visual candidate
    universes and to double-check delivered pages.
    """

    workspace: SeededWorkspace
    render_store: PageRenderStore
    vlm: VlmClient
    authorization: EffectiveAuthorization | None = None
    assurance: str = "standard"
    #: visual generation for the live cut; replaced after a revision
    visual_index: VisualIndex | None = None
    #: revision each doc must be attributed to for this phase
    expected_revisions: dict[str, str] | None = None

    def require_auth(self) -> EffectiveAuthorization:
        if self.authorization is None:
            raise RuntimeError("phase authorization not resolved")
        return self.authorization

    def allows_doc(self, doc: SeededDoc) -> bool:
        auth = self.require_auth()
        return auth.allows(
            doc.source_ref, source_ref=doc.source_ref, domain_key=doc.domain
        )

    def expected_revision(self, doc_id: str) -> str | None:
        if self.expected_revisions is None:
            return None
        return self.expected_revisions.get(doc_id)


async def resolve_phase_authorization(ctx: LaneContext) -> EffectiveAuthorization:
    ctx.authorization = await resolve_effective_authorization(
        ctx.workspace.factory, ctx.workspace.workspace_id, assurance=ctx.assurance
    )
    return ctx.authorization


# ---------------------------------------------------------------------------
# visual index construction over the seeded workspace
# ---------------------------------------------------------------------------


def build_page_entries(ws: SeededWorkspace) -> list[VisualPageEntry]:
    entries: list[VisualPageEntry] = []
    for doc_id in sorted(ws.docs):
        doc = ws.docs[doc_id]
        if not doc.visual_admitted:
            continue
        for page_number in sorted(doc.page_texts):
            entries.append(
                VisualPageEntry(
                    doc_id=doc.doc_id,
                    page_number=page_number,
                    page_index=page_number - 1,
                    blob_key=doc.blob_key,
                    revision=doc.revision,
                    domain=doc.domain,
                    source_ref=doc.source_ref,
                )
            )
    return entries


def render_entries(
    ws: SeededWorkspace,
    render_store: PageRenderStore,
    entries: Sequence[VisualPageEntry],
) -> list[tuple[VisualPageEntry, bytes]]:
    """Render every admitted page on demand (cold) or reuse the cache."""
    pages: list[tuple[VisualPageEntry, bytes]] = []
    for entry in entries:
        doc = ws.docs[entry.doc_id]
        rendered = render_store.render(
            entry.blob_key,
            entry.page_index,
            doc.artifact_path(ws.source_store),
            admitted=True,
        )
        pages.append((entry, rendered.path.read_bytes()))
    return pages


def build_visual_index(
    ws: SeededWorkspace,
    render_store: PageRenderStore,
    embedder,
) -> VisualIndex:
    """Build one visual generation over the current cut (standard profile)."""
    return VisualIndex.build(
        workspace_id=ws.workspace_id,
        embedder=embedder,
        pages=render_entries(ws, render_store, build_page_entries(ws)),
    )


def build_visual_index_high_assurance(
    ws: SeededWorkspace,
    render_store: PageRenderStore,
    embedder,
) -> VisualIndex:
    """Physically partitioned visual generation (general domain only)."""
    return VisualIndex.build_high_assurance(
        workspace_id=ws.workspace_id,
        embedder=embedder,
        pages=render_entries(ws, render_store, build_page_entries(ws)),
        allowed_domains=["general"],
    )


# ---------------------------------------------------------------------------
# lexical page ranking through the production executor
# ---------------------------------------------------------------------------


def _doc_for_view(ctx: LaneContext, view_id: str) -> SeededDoc | None:
    for doc in ctx.workspace.docs.values():
        if doc.view_id == view_id:
            return doc
    for doc in ctx.workspace.superseded.values():
        if doc.view_id == view_id:
            return doc
    return None


async def lexical_ranked_pages(
    ctx: LaneContext, query: CorpusQuery, *, limit: int = LEXICAL_RANK_K
) -> tuple[list[tuple[str, int]], str | None]:
    """Run the production lexical search; aggregate node hits to page ranks.

    Page rank uses the executor's own node rank: a page's rank is the
    best (lowest) rank of any of its nodes in the lexical result.
    Returns ``(ranked_pages, error)``.
    """
    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": ctx.workspace.workspace_id,
            "assurance": ctx.assurance,
            # natural-language questions carry words that never appear on
            # pages ("what", "which"); any_term + bm25 rank is the
            # strongest practical lexical shape for this lane
            "operations": [
                {
                    "op": "lexical_search",
                    "text": query.text,
                    "mode": "any_term",
                    "limit": 32,
                }
            ],
        }
    )
    packet: EvidencePacket = await execute_query(ctx.workspace.factory, request)
    if packet.publication_status != "published":
        return [], f"publication_status={packet.publication_status}"
    page_best: dict[tuple[str, int], float] = {}
    for unit in packet.evidence:
        doc = _doc_for_view(ctx, unit.locator.view_id)
        if doc is None:
            continue
        if not unit.locator.node_id:
            continue
        page = doc.page_of_node(unit.locator.node_id)
        key = (doc.doc_id, page)
        rank = unit.rank if unit.rank is not None else float(len(page_best))
        if key not in page_best or rank < page_best[key]:
            page_best[key] = rank
    ranked = [key for key, _ in sorted(page_best.items(), key=lambda item: item[1])]
    return ranked[:limit], None


# ---------------------------------------------------------------------------
# evidence delivery helpers
# ---------------------------------------------------------------------------


def _page_evidence(
    ctx: LaneContext,
    doc_id: str,
    page_number: int,
    *,
    use_image: bool,
    question: str,
    revision: str | None = None,
) -> RouteEvidence:
    """Deliver one page of evidence (image or transcript) to the answerer."""
    doc = ctx.workspace.docs.get(doc_id)
    if doc is None:
        return RouteEvidence(system_id="", error=f"unknown doc {doc_id}")
    png_bytes: bytes | None = None
    text: str | None = None
    if use_image:
        rendered = ctx.render_store.render(
            doc.blob_key,
            page_number - 1,
            doc.artifact_path(ctx.workspace.source_store),
            admitted=True,
        )
        png_bytes = rendered.path.read_bytes()
    else:
        text = doc.page_text(page_number)
    envelope, parsed = ctx.vlm.answer(question, page_png=png_bytes, page_text=text)
    base = {
        "delivered_page": (doc_id, page_number),
        "revision": revision or doc.revision,
        "evidence_kind": "image_page" if use_image else "text_page",
        "source_resolvable": True,
    }
    if envelope.error:
        return RouteEvidence(system_id="", error=f"vlm: {envelope.error}", **base)
    answer: str | None = None
    answer_null = False
    answer_unparseable = False
    if parsed is None:
        answer_unparseable = True
    elif "answer" in parsed:
        value = parsed["answer"]
        if value is None:
            answer_null = True
        else:
            answer = str(value)
    else:
        answer_null = True
    expected = ctx.expected_revision(doc_id)
    stale = expected is not None and (revision or doc.revision) != expected
    return RouteEvidence(
        system_id="",
        answer=answer,
        answer_parsed_null=answer_null,
        answer_unparseable=answer_unparseable,
        stale_revision_delivered=bool(stale),
        **base,
    )


def _forbidden_check(ctx: LaneContext, evidence: RouteEvidence) -> RouteEvidence:
    """Mark delivery as forbidden when the delivered doc is live-denied.

    Lanes filter candidates before delivery, so this firing means a lane
    bug — exactly what the danger class exists to expose.
    """
    if evidence.delivered_page is None:
        return evidence
    doc = ctx.workspace.docs.get(evidence.delivered_page[0])
    if doc is not None and not ctx.allows_doc(doc):
        return replace(evidence, forbidden_source_delivered=True)
    return evidence


def _visual_candidate_filter(ctx: LaneContext):
    auth = ctx.require_auth()

    def allowed(entry: VisualPageEntry) -> bool:
        return auth.allows(
            entry.source_ref, source_ref=entry.source_ref, domain_key=entry.domain
        )

    return allowed


# ---------------------------------------------------------------------------
# lanes
# ---------------------------------------------------------------------------


async def run_lexical_text(ctx: LaneContext, query: CorpusQuery) -> RouteEvidence:
    ranked, error = await lexical_ranked_pages(ctx, query)
    if error:
        return RouteEvidence(system_id=B1_SYSTEM, error=error)
    if not ranked:
        return RouteEvidence(system_id=B1_SYSTEM)
    doc_id, page = ranked[0]
    evidence = _page_evidence(ctx, doc_id, page, use_image=False, question=query.text)
    return _forbidden_check(
        ctx, replace(evidence, system_id=B1_SYSTEM, ranked_pages=tuple(ranked))
    )


async def run_lexical_render(ctx: LaneContext, query: CorpusQuery) -> RouteEvidence:
    ranked, error = await lexical_ranked_pages(ctx, query)
    if error:
        return RouteEvidence(system_id=B2_SYSTEM, error=error)
    if not ranked:
        return RouteEvidence(system_id=B2_SYSTEM)
    doc_id, page = ranked[0]
    evidence = _page_evidence(ctx, doc_id, page, use_image=True, question=query.text)
    return _forbidden_check(
        ctx, replace(evidence, system_id=B2_SYSTEM, ranked_pages=tuple(ranked))
    )


async def run_visual_dense(ctx: LaneContext, query: CorpusQuery, embedder) -> RouteEvidence:
    system_id = f"{V_SYSTEM_PREFIX}:{embedder.identity}"
    index = ctx.visual_index
    if index is None:
        return RouteEvidence(system_id=system_id, error="visual generation absent")
    result = index.search(
        embedder.embed_text(query.text),
        budget=VisualQueryBudget(top_k=VISUAL_RANK_K),
        candidate_filter=_visual_candidate_filter(ctx),
    )
    ranked = tuple((h.doc_id, h.page_number) for h in result.hits)
    if not ranked:
        return RouteEvidence(system_id=system_id)
    hit = result.hits[0]
    doc = ctx.workspace.docs.get(hit.doc_id)
    revision = hit.revision if doc is not None else None
    evidence = _page_evidence(
        ctx,
        hit.doc_id,
        hit.page_number,
        use_image=True,
        question=query.text,
        revision=revision,
    )
    return _forbidden_check(
        ctx, replace(evidence, system_id=system_id, ranked_pages=ranked)
    )


def _contact_sheet(
    ctx: LaneContext, candidates: Sequence[tuple[str, int]]
) -> tuple[bytes, list[str]]:
    """Grid montage of labeled page thumbnails for the VLM reranker."""
    from PIL import Image, ImageDraw, ImageFont

    thumbs = []
    for doc_id, page in candidates:
        doc = ctx.workspace.docs[doc_id]
        rendered = ctx.render_store.render(
            doc.blob_key,
            page - 1,
            doc.artifact_path(ctx.workspace.source_store),
            admitted=True,
        )
        image = Image.open(io.BytesIO(rendered.path.read_bytes())).convert("RGB")
        image.thumbnail((300, 388))
        thumbs.append(image)
    labels = [chr(ord("A") + i) for i in range(len(thumbs))]
    cell_w, cell_h, cols = 310, 420, 3
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, thumb in enumerate(thumbs):
        col, row = i % cols, i // cols
        x = col * cell_w + 5
        y = row * cell_h + 25
        sheet.paste(thumb, (x, y))
        draw.rectangle(
            [x, row * cell_h + 2, x + 22, row * cell_h + 24], fill="black"
        )
        draw.text((x + 7, row * cell_h + 8), labels[i], fill="white", font=font)
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue(), labels


def _score_from_rerank(parsed: dict, label: str) -> float:
    try:
        value = parsed["scores"][label]
        return float(value)
    except (KeyError, TypeError, ValueError):
        return 0.0


async def run_visual_hybrid(ctx: LaneContext, query: CorpusQuery, embedder) -> RouteEvidence:
    """Lexical + visual candidate union, VLM reranked onto one page."""
    ranked_lexical, error = await lexical_ranked_pages(ctx, query)
    if error:
        return RouteEvidence(system_id=V2_SYSTEM, error=error)
    visual_ranked: list[tuple[str, int]] = []
    if ctx.visual_index is not None:
        result = ctx.visual_index.search(
            embedder.embed_text(query.text),
            budget=VisualQueryBudget(top_k=RERANK_CANDIDATES_PER_ROUTE),
            candidate_filter=_visual_candidate_filter(ctx),
        )
        visual_ranked = [(h.doc_id, h.page_number) for h in result.hits]
    candidates: list[tuple[str, int]] = []
    for key in ranked_lexical[:RERANK_CANDIDATES_PER_ROUTE] + visual_ranked:
        if key not in candidates:
            candidates.append(key)
    if not candidates:
        return RouteEvidence(system_id=V2_SYSTEM)
    montage, labels = _contact_sheet(ctx, candidates)
    envelope, parsed = ctx.vlm.rerank(query.text, montage, labels)
    if envelope.error or parsed is None or "scores" not in parsed:
        # reranker failed: fall back to lexical order honestly; the
        # evidence still reports what was actually used
        ordered = list(candidates)
    else:
        order = sorted(
            range(len(candidates)),
            key=lambda i: (-_score_from_rerank(parsed, labels[i]), i),
        )
        ordered = [candidates[i] for i in order]
    doc_id, page = ordered[0]
    evidence = _page_evidence(ctx, doc_id, page, use_image=True, question=query.text)
    return _forbidden_check(
        ctx, replace(evidence, system_id=V2_SYSTEM, ranked_pages=tuple(ordered))
    )


async def run_lane(
    system_id: str, ctx: LaneContext, query: CorpusQuery, *, embedder
) -> RouteEvidence:
    """Dispatch one lane by system id; always returns scored evidence."""
    if system_id == B1_SYSTEM:
        return await run_lexical_text(ctx, query)
    if system_id == B2_SYSTEM:
        return await run_lexical_render(ctx, query)
    if system_id == V2_SYSTEM:
        return await run_visual_hybrid(ctx, query, embedder)
    if system_id.startswith(f"{V_SYSTEM_PREFIX}:"):
        expected = f"{V_SYSTEM_PREFIX}:{embedder.identity}"
        if system_id != expected:
            raise ValueError(f"system {system_id!r} does not match embedder {expected!r}")
        return await run_visual_dense(ctx, query, embedder)
    raise ValueError(f"unknown system: {system_id!r}")
