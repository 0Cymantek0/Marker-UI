"""PR81A scoring taxonomy tests. Matrix letter Z."""

from __future__ import annotations

import pytest

from app.eval.pr81a.corpus import CorpusQuery
from app.eval.pr81a.scoring import (
    RouteEvidence,
    aggregate_metrics,
    score_query,
)


def _query(**overrides) -> CorpusQuery:
    base = dict(
        query_id="q1",
        text="what?",
        slice_tag="table.cell_grid",
        doc_id="doc-a",
        page_number=2,
        answer="18.5",
        answer_kind="decimal",
        phase="baseline",
        profile="default",
        expectation="answer",
        normalized_answer="18.5",
    )
    base.update(overrides)
    return CorpusQuery(**base)


class TestRetrievalLayer:
    def test_page_hit_ranked(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                ranked_pages=(("doc-a", 2), ("doc-b", 1)),
                delivered_page=("doc-a", 2),
                revision="v1",
                answer="18.5",
                evidence_kind="image_page",
                source_resolvable=True,
            ),
        )
        assert score.retrieval == "page_hit"
        assert score.rank_of_gold == 1
        assert score.task_success is True
        assert score.danger is None

    def test_page_miss_with_rank_of_gold(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                ranked_pages=(("doc-b", 1), ("doc-a", 2)),
                delivered_page=("doc-b", 1),
                answer="9.9",
                evidence_kind="text_page",
                source_resolvable=True,
            ),
        )
        assert score.retrieval == "page_miss"
        assert score.rank_of_gold == 2
        assert score.task_success is False

    def test_decoy_confusion_flagged(self):
        score = score_query(
            _query(doc_id="doc-fin-01", page_number=2),
            RouteEvidence(
                system_id="route",
                ranked_pages=(("doc-fin-03", 2),),
                delivered_page=("doc-fin-03", 2),
                answer="3.8",
                evidence_kind="image_page",
                source_resolvable=True,
            ),
        )
        assert score.retrieval == "page_miss"
        assert score.danger == "decoy_confusion"
        assert score.task_success is False


class TestDownstreamLayer:
    def test_wrong_answer_on_hit_is_not_success(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-a", 2),
                answer="19.5",
                evidence_kind="text_page",
                source_resolvable=True,
            ),
        )
        assert score.retrieval == "page_hit"
        assert score.downstream == "answer_wrong"
        assert score.task_success is False

    def test_null_answer_is_honest_abstention(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-a", 2),
                answer="null-ish",
                answer_parsed_null=True,
                evidence_kind="image_page",
                source_resolvable=True,
            ),
        )
        assert score.downstream == "answer_null"
        assert score.task_success is False

    def test_unparseable_answer_classified(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-a", 2),
                answer="about nineteen",
                evidence_kind="text_page",
                source_resolvable=True,
            ),
        )
        assert score.downstream == "answer_unparseable"

    def test_normalization_used_for_equality(self):
        score = score_query(
            _query(answer="2450", answer_kind="decimal", normalized_answer="2450"),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-a", 2),
                answer="$2,450 USD",
                evidence_kind="text_page",
                source_resolvable=True,
            ),
        )
        assert score.downstream == "answer_correct"


class TestDangerLayer:
    def test_forbidden_delivery_is_danger_even_with_correct_answer(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-sec-01", 1),
                answer="18.5",
                evidence_kind="image_page",
                source_resolvable=True,
                forbidden_source_delivered=True,
            ),
        )
        assert score.retrieval == "page_miss"
        assert score.danger == "forbidden_delivered"
        assert score.task_success is False

    def test_unresolvable_hit_is_danger(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-a", 2),
                answer="18.5",
                evidence_kind="image_page",
                source_resolvable=False,
            ),
        )
        assert score.danger == "unresolvable_source"
        assert score.task_success is False

    def test_stale_revision_is_danger(self):
        score = score_query(
            _query(),
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-a", 2),
                answer="18.5",
                evidence_kind="image_page",
                source_resolvable=True,
                stale_revision_delivered=True,
            ),
        )
        assert score.danger == "stale_revision_delivered"


class TestHonestUnavailability:
    def test_lane_error_is_unavailable_not_miss(self):
        score = score_query(
            _query(),
            RouteEvidence(system_id="route", error="model unavailable"),
        )
        assert score.retrieval == "unavailable"
        assert score.downstream == "no_evidence"

    def test_not_admitted_when_nothing_ranked(self):
        score = score_query(
            _query(),
            RouteEvidence(system_id="route"),
        )
        assert score.retrieval == "not_admitted"
        assert score.downstream == "no_evidence"


class TestNoDelivery:
    def test_abstention_is_success(self):
        query = _query(expectation="no_delivery", answer="", normalized_answer="", profile="denied")
        score = score_query(query, RouteEvidence(system_id="route"))
        assert score.retrieval == "no_delivery_ok"
        assert score.task_success is True

    def test_any_delivery_is_violation(self):
        query = _query(expectation="no_delivery", answer="", normalized_answer="", profile="denied")
        score = score_query(
            query,
            RouteEvidence(
                system_id="route",
                delivered_page=("doc-sec-01", 1),
                answer="2.4M",
                evidence_kind="image_page",
                source_resolvable=True,
            ),
        )
        assert score.retrieval == "no_delivery_violated"
        assert score.danger == "forbidden_delivered"
        assert score.task_success is False


class TestAggregation:
    def _scores(self):
        ok = score_query(
            _query(),
            RouteEvidence(
                system_id="route-a",
                ranked_pages=(("doc-a", 2),),
                delivered_page=("doc-a", 2),
                answer="18.5",
                evidence_kind="text_page",
                source_resolvable=True,
            ),
        )
        miss = score_query(
            _query(query_id="q2"),
            RouteEvidence(
                system_id="route-a",
                ranked_pages=(("doc-x", 1), ("doc-a", 2)),
                delivered_page=("doc-x", 1),
                answer="1.0",
                evidence_kind="text_page",
                source_resolvable=True,
            ),
        )
        return [ok, miss]

    def test_aggregate_math(self):
        metrics = aggregate_metrics(self._scores())["route-a"]
        assert metrics["queries_judged"] == 2
        assert metrics["task_success"] == 1
        assert metrics["task_success_rate"] == 0.5
        assert metrics["page_hit_rate"] == 0.5
        assert metrics["mrr"] == 0.75  # 1 + 0.5 over 2
        assert metrics["answer_accuracy_on_delivered"] == 0.5

    def test_danger_counts_separate(self):
        scores = self._scores()
        scores.append(
            score_query(
                _query(query_id="q3", doc_id="doc-fin-01"),
                RouteEvidence(
                    system_id="route-a",
                    delivered_page=("doc-fin-03", 2),
                    answer="x",
                    evidence_kind="image_page",
                    source_resolvable=True,
                ),
            )
        )
        metrics = aggregate_metrics(scores)["route-a"]
        assert metrics["danger_counts"] == {"decoy_confusion": 1}

    def test_no_delivery_excluded_from_judged_but_counted(self):
        scores = self._scores()
        scores.append(
            score_query(
                _query(query_id="q4", expectation="no_delivery", answer="", normalized_answer=""),
                RouteEvidence(system_id="route-a"),
            )
        )
        metrics = aggregate_metrics(scores)["route-a"]
        assert metrics["queries_judged"] == 2
        assert metrics["no_delivery_required_total"] == 1
        assert metrics["no_delivery_required_ok"] == 1

    def test_deterministic_repeat(self):
        first = aggregate_metrics(self._scores())
        second = aggregate_metrics(self._scores())
        assert first == second

    def test_slice_breakdown(self):
        scores = self._scores()
        metrics = aggregate_metrics(scores)["route-a"]
        assert metrics["slices"]["table.cell_grid"]["queries"] == 2
        assert metrics["slices"]["table.cell_grid"]["task_success_rate"] == 0.5


class TestContract:
    def test_empty_system_id_rejected(self):
        with pytest.raises(ValueError):
            score_query(_query(), RouteEvidence(system_id=""))

    def test_bad_evidence_kind_rejected(self):
        with pytest.raises(ValueError):
            score_query(_query(), RouteEvidence(system_id="r", evidence_kind="smell"))
