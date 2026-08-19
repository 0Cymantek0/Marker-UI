"""PR81A lane-neutral scoring: retrieval, downstream, and danger kept apart.

A route's evidence for one query is scored into three independent
layers, because they answer different questions:

* **retrieval** — did the route put the gold page in front of the task
  (top-1 hit, rank, recall over its delivered ranking)?
* **downstream** — given only what the route delivered, was the actual
  task answer correct?
* **danger** — did the route commit a failure the product must never
  average away: delivering forbidden material, delivering a stale
  revision as current, claiming a hit it cannot resolve to source
  identity, or confusing a near-duplicate template twin?

The primary promotion metric is ``task_success`` = page hit at rank 1
AND correct answer AND no danger. Pure functions only; the runner
double-scores and requires byte-identical results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from app.eval.pr81a.corpus import CorpusQuery
from app.eval.pr81a.normalize import NormalizeError, normalize_answer

#: Near-duplicate template twins: selecting the twin of the gold document
#: is the specific dangerous confusion this corpus is built to expose.
DECOY_TWINS: dict[str, str] = {
    "doc-fin-01": "doc-fin-03",
    "doc-fin-03": "doc-fin-01",
    "doc-rd-01": "doc-rd-02",
    "doc-rd-02": "doc-rd-01",
    "doc-sec-02": "doc-pub-03",
    "doc-pub-03": "doc-sec-02",
}

RETRIEVAL_OUTCOMES = frozenset(
    {"page_hit", "page_miss", "unavailable", "not_admitted", "no_delivery_ok", "no_delivery_violated"}
)
DOWNSTREAM_OUTCOMES = frozenset(
    {"answer_correct", "answer_wrong", "answer_null", "no_evidence", "answer_unparseable"}
)
DANGER_CLASSES = frozenset(
    {
        "forbidden_delivered",
        "stale_revision_delivered",
        "unresolvable_source",
        "decoy_confusion",
    }
)
EVIDENCE_KINDS = frozenset({"text_page", "image_page", "image_text_page", "none"})


@dataclass(frozen=True)
class RouteEvidence:
    """What one route delivered for one query."""

    system_id: str
    ranked_pages: tuple[tuple[str, int], ...] = ()
    delivered_page: tuple[str, int] | None = None
    revision: str | None = None
    answer: str | None = None
    answer_parsed_null: bool = False
    answer_unparseable: bool = False
    evidence_kind: str = "none"
    source_resolvable: bool = False
    error: str | None = None
    forbidden_source_delivered: bool = False
    stale_revision_delivered: bool = False


@dataclass(frozen=True)
class QueryScore:
    query_id: str
    slice_tag: str
    system_id: str
    phase: str
    profile: str
    retrieval: str
    downstream: str
    task_success: bool
    danger: str | None
    rank_of_gold: int | None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "slice_tag": self.slice_tag,
            "system_id": self.system_id,
            "phase": self.phase,
            "profile": self.profile,
            "retrieval": self.retrieval,
            "downstream": self.downstream,
            "task_success": self.task_success,
            "danger": self.danger,
            "rank_of_gold": self.rank_of_gold,
            "detail": self.detail,
        }


def score_query(query: CorpusQuery, evidence: RouteEvidence) -> QueryScore:
    """Score one route's evidence against committed gold. Pure."""
    if evidence.system_id == "":
        raise ValueError("evidence.system_id must be set")
    if evidence.evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(f"bad evidence_kind: {evidence.evidence_kind!r}")

    danger: str | None = None
    if evidence.forbidden_source_delivered:
        danger = "forbidden_delivered"
    elif evidence.stale_revision_delivered:
        danger = "stale_revision_delivered"
    elif evidence.delivered_page is not None and not evidence.source_resolvable:
        danger = "unresolvable_source"

    rank_of_gold: int | None = None
    for rank, (doc_id, page_number) in enumerate(evidence.ranked_pages, start=1):
        if doc_id == query.doc_id and page_number == query.page_number:
            rank_of_gold = rank
            break

    # -- no_delivery authorization cases ----------------------------------
    if query.expectation == "no_delivery":
        # The gold answer exists only inside forbidden material. The
        # route may still search the allowed corpus (and even deliver an
        # irrelevant allowed page), but it must not deliver forbidden
        # material and must not emit a confident answer.
        answered = evidence.answer is not None and not evidence.answer_parsed_null
        violated = evidence.forbidden_source_delivered or answered
        if violated:
            retrieval = "no_delivery_violated"
            if danger is None:
                danger = "forbidden_delivered"
        else:
            retrieval = "no_delivery_ok"
        downstream = "answer_wrong" if answered else "answer_null"
        return QueryScore(
            query_id=query.query_id,
            slice_tag=query.slice_tag,
            system_id=evidence.system_id,
            phase=query.phase,
            profile=query.profile,
            retrieval=retrieval,
            downstream=downstream,
            task_success=retrieval == "no_delivery_ok" and danger is None,
            danger=danger,
            rank_of_gold=rank_of_gold,
            detail={"expectation": "no_delivery"},
        )

    # -- honest unavailability ---------------------------------------------
    if evidence.error is not None:
        retrieval = "unavailable"
        downstream = "no_evidence"
        return QueryScore(
            query_id=query.query_id,
            slice_tag=query.slice_tag,
            system_id=evidence.system_id,
            phase=query.phase,
            profile=query.profile,
            retrieval=retrieval,
            downstream=downstream,
            task_success=False,
            danger=danger,
            rank_of_gold=rank_of_gold,
            detail={"error": evidence.error},
        )
    if evidence.delivered_page is None and evidence.answer is None:
        retrieval = "not_admitted" if evidence.ranked_pages == () else "page_miss"
        return QueryScore(
            query_id=query.query_id,
            slice_tag=query.slice_tag,
            system_id=evidence.system_id,
            phase=query.phase,
            profile=query.profile,
            retrieval=retrieval,
            downstream="no_evidence",
            task_success=False,
            danger=danger,
            rank_of_gold=rank_of_gold,
            detail={},
        )

    # -- retrieval layer ----------------------------------------------------
    if evidence.delivered_page is not None:
        doc_id, page_number = evidence.delivered_page
        retrieval = "page_hit" if (doc_id == query.doc_id and page_number == query.page_number) else "page_miss"
        if retrieval == "page_miss" and DECOY_TWINS.get(query.doc_id) == doc_id and danger is None:
            danger = "decoy_confusion"
    else:
        retrieval = "page_miss"

    # -- downstream layer ----------------------------------------------------
    if evidence.answer is None:
        downstream = "no_evidence" if evidence.delivered_page is None else "answer_null"
    elif evidence.answer_parsed_null or evidence.answer_unparseable:
        downstream = (
            "answer_unparseable" if evidence.answer_unparseable else "answer_null"
        )
    else:
        try:
            normalized_route = normalize_answer(evidence.answer, query.answer_kind)
            downstream = "answer_correct" if normalized_route == query.normalized_answer else "answer_wrong"
        except NormalizeError:
            downstream = "answer_unparseable"

    task_success = (
        retrieval == "page_hit"
        and downstream == "answer_correct"
        and danger is None
    )
    detail: dict = {}
    if evidence.delivered_page is not None:
        detail["delivered_page"] = list(evidence.delivered_page)
        detail["delivered_revision"] = evidence.revision
    if evidence.answer is not None:
        detail["answer"] = evidence.answer
    return QueryScore(
        query_id=query.query_id,
        slice_tag=query.slice_tag,
        system_id=evidence.system_id,
        phase=query.phase,
        profile=query.profile,
        retrieval=retrieval,
        downstream=downstream,
        task_success=task_success,
        danger=danger,
        rank_of_gold=rank_of_gold,
        detail=detail,
    )


def aggregate_metrics(scores: Sequence[QueryScore]) -> dict:
    """Aggregate per-route metrics; danger classes never merge into rates."""
    systems: dict[str, list[QueryScore]] = {}
    for score in scores:
        systems.setdefault(score.system_id, []).append(score)
    out: dict = {}
    for system_id, system_scores in sorted(systems.items()):
        # answer-expectation queries only; no_delivery probes are scored
        # separately below
        judged = [s for s in system_scores if s.detail.get("expectation") != "no_delivery"]
        n = len(judged)
        hits = sum(1 for s in judged if s.retrieval == "page_hit")
        successes = sum(1 for s in judged if s.task_success)
        with_answer = [s for s in judged if s.downstream in ("answer_correct", "answer_wrong", "answer_unparseable")]
        correct = sum(1 for s in judged if s.downstream == "answer_correct")
        reciprocal: list[float] = []
        for s in judged:
            if s.rank_of_gold:
                reciprocal.append(1.0 / s.rank_of_gold)
        danger_counts: dict[str, int] = {}
        for s in system_scores:
            if s.danger:
                danger_counts[s.danger] = danger_counts.get(s.danger, 0) + 1
        slices: dict[str, dict] = {}
        for s in judged:
            bucket = slices.setdefault(
                s.slice_tag,
                {"queries": 0, "task_success": 0, "page_hits": 0, "dangers": 0},
            )
            bucket["queries"] += 1
            bucket["task_success"] += 1 if s.task_success else 0
            bucket["page_hits"] += 1 if s.retrieval == "page_hit" else 0
            bucket["dangers"] += 1 if s.danger else 0
        for tag, bucket in slices.items():
            bucket["task_success_rate"] = round(bucket["task_success"] / bucket["queries"], 4) if bucket["queries"] else 0.0
            bucket["page_hit_rate"] = round(bucket["page_hits"] / bucket["queries"], 4) if bucket["queries"] else 0.0
        no_delivery = [
            s for s in system_scores if s.detail.get("expectation") == "no_delivery"
        ]
        out[system_id] = {
            "queries_judged": n,
            "task_success": successes,
            "task_success_rate": round(successes / n, 4) if n else 0.0,
            "page_hits": hits,
            "page_hit_rate": round(hits / n, 4) if n else 0.0,
            "answers_delivered": len(with_answer),
            "answer_correct": correct,
            "answer_accuracy_on_delivered": round(correct / len(with_answer), 4) if with_answer else None,
            "mrr": round(sum(reciprocal) / len(reciprocal), 4) if reciprocal else None,
            "unavailable": sum(1 for s in judged if s.retrieval == "unavailable"),
            "not_admitted": sum(1 for s in judged if s.retrieval == "not_admitted"),
            "danger_counts": danger_counts,
            "slices": slices,
            "no_delivery_required_ok": sum(1 for s in no_delivery if s.retrieval == "no_delivery_ok"),
            "no_delivery_required_total": len(no_delivery),
        }
    return out
