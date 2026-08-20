"""Adversarial mapping corpus evaluation tests (PR82A Q1/Q2)."""

from __future__ import annotations

from dataclasses import replace

from app.eval.pr82.mapping import (
    SOURCE_REV,
    TARGET_REV,
    _anchor,
    _case,
    build_mapping_corpus,
    evaluate_mapping_corpus,
)
from app.kernel.anchors import TextQuoteSelector


class TestMappingCorpus:
    def test_corpus_covers_the_twelve_adversarial_cases(self):
        corpus = build_mapping_corpus()
        assert len(corpus) == 12
        assert len({case.case_id for case in corpus}) == 12
        attacks = " ".join(case.attack for case in corpus)
        for topic in (
            "identity",
            "position",
            "relocation",
            "ambiguity",
            "deterministic identity",
            "exact identity",
            "reassigned",
            "candidate",
            "value edits",
            "quote evidence",
            "cross-revision identity",
            "anchors or mint mappings",
        ):
            assert topic in attacks

    def test_corpus_run_has_zero_violations_and_is_replay_stable(self):
        result = evaluate_mapping_corpus()
        assert result.violations == ()
        assert result.replay_stable is True
        summary = result.summary()
        assert summary["cases"] == 12
        assert summary["disposition_counts"] == {
            "exact": 1,
            "mapped_deterministic": 4,
            "mapped_semantic_candidate": 3,
            "stale": 2,
            "unresolved": 1,
            "refused": 1,
        }

    def test_every_case_reports_a_known_rule(self):
        result = evaluate_mapping_corpus()
        known_rules = {
            "native_identity_v1",
            "quote_unique_v1",
            "quote_duplicates_v1",
            "quote_normalized_v1",
            "quote_fuzzy_v1",
            "quote_partial_v1",
            "geometry_approximate_v1",
            "no_match_v1",
            "insufficient_evidence_v1",
        }
        for case_result in result.results:
            if case_result.refused:
                assert case_result.rule_id is None
            else:
                assert case_result.rule_id in known_rules, case_result.case_id

    def test_q2_similarity_never_promotes(self):
        result = evaluate_mapping_corpus()
        corpus = {case.case_id: case for case in build_mapping_corpus()}
        forbidden = {"exact", "mapped_deterministic", "mapped_reviewed"}
        for case_result in result.results:
            case = corpus[case_result.case_id]
            if case.exact_forbidden and not case.expect_refused:
                assert case_result.disposition not in forbidden, case_result.case_id


class TestEvaluatorNegativeControls:
    """The evaluator must be able to fail, or zero violations is noise."""

    def test_control_case_passes(self):
        case = _case(
            "control-paraphrase",
            "Paraphrased target",
            "control",
            _anchor(
                SOURCE_REV,
                quote=TextQuoteSelector(quote="The quick brown fox jumps over the lazy dog"),
            ),
            (
                _anchor(
                    TARGET_REV,
                    quote=TextQuoteSelector(quote="A swift auburn fox leaps above the idle hound"),
                ),
            ),
            frozenset({"mapped_semantic_candidate", "stale"}),
            exact_forbidden=True,
        )
        result = evaluate_mapping_corpus((case,))
        assert result.violation_count == 0

    def test_wrong_expectation_is_reported_as_violation(self):
        case = _case(
            "control-deleted",
            "Deleted target",
            "control",
            _anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Now gone from the document")),
            (_anchor(TARGET_REV, quote=TextQuoteSelector(quote="Different text entirely")),),
            frozenset({"stale"}),
            exact_forbidden=True,
        )
        corrupted = replace(case, expected_dispositions=frozenset({"exact"}))
        result = evaluate_mapping_corpus((corrupted,))
        assert result.violation_count == 1
        assert "outside expected" in result.violations[0]

    def test_promoted_similarity_is_reported_as_violation(self):
        # Force the paraphrase case to accept only candidate while the
        # cascade honestly produces stale: expectation mismatch must
        # surface, proving Q2 checks are not vacuous.
        case = _case(
            "control-paraphrase",
            "Paraphrased target",
            "control",
            _anchor(
                SOURCE_REV,
                quote=TextQuoteSelector(quote="The quick brown fox jumps over the lazy dog"),
            ),
            (
                _anchor(
                    TARGET_REV,
                    quote=TextQuoteSelector(quote="A swift auburn fox leaps above the idle hound"),
                ),
            ),
            frozenset({"mapped_semantic_candidate"}),
            exact_forbidden=True,
        )
        result = evaluate_mapping_corpus((case,))
        assert result.violation_count == 1

    def test_unrefused_policy_pair_is_reported(self):
        from app.eval.pr82.mapping import MappingCase

        mutated = MappingCase(
            case_id="control-policy-2",
            description="Pair across distinct revisions mislabeled as policy-only",
            attack="control",
            old=_anchor(SOURCE_REV, quote=TextQuoteSelector(quote="Unchanged")),
            new_anchors=(_anchor(TARGET_REV, quote=TextQuoteSelector(quote="Unchanged")),),
            expected_dispositions=frozenset(),
            exact_forbidden=False,
            expect_refused=True,
        )
        result = evaluate_mapping_corpus((mutated,))
        assert result.violation_count == 1
        assert "was not refused" in result.violations[0]
