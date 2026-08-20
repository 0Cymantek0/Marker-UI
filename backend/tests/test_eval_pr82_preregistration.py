"""PR82 preregistration and release-evidence contract tests."""

from __future__ import annotations

import pytest

from app.eval.pr82.evidence import (
    PR83_READY,
    RELEASE_EVIDENCE_SCHEMA_VERSION,
    ReleaseEvidenceError,
    answer_question,
    validate_release_bundle,
)
from app.eval.pr82.preregistration import (
    DECISION_VOCABULARY,
    DOMAINS,
    MODE_VOCABULARY,
    PREREGISTERED_QUESTIONS,
    STATUS_VOCABULARY,
    preregistration_identity,
    question_by_id,
)


def _minimal_bundle(**overrides):
    bundle = {
        "schema_version": RELEASE_EVIDENCE_SCHEMA_VERSION,
        "git_sha": "0" * 40,
        "planning_head": "f" * 40,
        "preregistration_identity": preregistration_identity(),
        "environment": {"python": "3.11.9", "platform": "test", "machine": "test"},
        "consumed_evidence": [
            {
                "artifact": "docs/reference/measurements/pr81b-model-sensitivity.json",
                "schema_version": "marker.pr81b_model_sensitivity.v1",
                "lifecycle": "current",
                "supports": ["Q11"],
            }
        ],
        "suites": {
            "mapping": {
                "questions": ["Q1"],
                "mode": "deterministic",
                "status": "pass",
                "decision": "pass",
                "reason": "corpus clean",
                "checks": {"cases": 12, "silent_identity_changes": 0},
                "findings": [],
                "blockers": [],
            }
        },
        "answers": {
            "Q1": answer_question(
                "Q1",
                decision="pass",
                status="pass",
                evidence="suite:mapping",
                reason="no silent identity changes",
            )
        },
        "readiness_invariants": [
            {"invariant": "no false identity across revisions", "status": "pass", "evidence": "suite:mapping"}
        ],
        "recommendation": {"pr83": PR83_READY, "reason": "test bundle"},
        "reproduce": {"focused": ["pytest tests/test_kernel_anchor_mapping.py"], "full": "python -m pytest tests conformance -q"},
        "limitations": [],
    }
    bundle.update(overrides)
    return bundle


class TestPreregistration:
    def test_question_ids_are_unique_and_stable(self):
        ids = [q.question_id for q in PREREGISTERED_QUESTIONS]
        assert len(ids) == len(set(ids))
        assert ids == [f"Q{i}" for i in range(1, len(ids) + 1)]

    def test_every_question_has_a_decision_rule(self):
        for question in PREREGISTERED_QUESTIONS:
            assert question.domain in DOMAINS
            assert len(question.decision_rule) > 20, question.question_id
            assert question.question.endswith("?")

    def test_preregistration_identity_is_deterministic(self):
        assert preregistration_identity() == preregistration_identity()

    def test_unknown_question_fails(self):
        with pytest.raises(KeyError, match="not preregistered"):
            question_by_id("Q999")

    def test_vocabularies_are_closed(self):
        assert DECISION_VOCABULARY == frozenset(
            {
                "pass",
                "promote_narrow",
                "shadow",
                "non_promoted",
                "kill_or_simplify",
                "blocked",
                "inconclusive",
                "characterization_only",
            }
        )
        assert len(STATUS_VOCABULARY) == 6
        assert MODE_VOCABULARY == frozenset(
            {"deterministic", "replay", "live", "machine_dependent", "unavailable"}
        )


class TestReleaseBundleValidation:
    def test_minimal_valid_bundle_passes(self):
        validate_release_bundle(_minimal_bundle())

    def test_preregistration_identity_mismatch_fails(self):
        bundle = _minimal_bundle(preregistration_identity="sha256:deadbeef")
        with pytest.raises(ReleaseEvidenceError, match="preregistration_identity"):
            validate_release_bundle(bundle)

    def test_unknown_root_field_fails(self):
        with pytest.raises(ReleaseEvidenceError, match="unknown root fields"):
            validate_release_bundle(_minimal_bundle(vibes="great"))

    def test_unknown_status_decision_mode_fail(self):
        with pytest.raises(ReleaseEvidenceError, match="status"):
            validate_release_bundle(
                _minimal_bundle(
                    suites={"mapping": {**_minimal_bundle()["suites"]["mapping"], "status": "mostly-fine"}}
                )
            )
        with pytest.raises(ReleaseEvidenceError, match="decision"):
            validate_release_bundle(
                _minimal_bundle(
                    suites={"mapping": {**_minimal_bundle()["suites"]["mapping"], "decision": "promote"}}
                )
            )
        with pytest.raises(ReleaseEvidenceError, match="mode"):
            validate_release_bundle(
                _minimal_bundle(
                    suites={"mapping": {**_minimal_bundle()["suites"]["mapping"], "mode": "vibes"}}
                )
            )

    def test_answer_without_owning_suite_fails(self):
        answers = dict(_minimal_bundle()["answers"])
        answers["Q2"] = answer_question("Q2", decision="shadow", status="shadow", evidence="suite:mapping", reason="x")
        with pytest.raises(ReleaseEvidenceError, match="claimed by no suite"):
            validate_release_bundle(_minimal_bundle(answers=answers))

    def test_unregistered_question_answer_fails(self):
        with pytest.raises(ReleaseEvidenceError, match="unregistered question"):
            validate_release_bundle(
                _minimal_bundle(
                    suites={
                        "bogus": {
                            "questions": ["Q99"],
                            "mode": "deterministic",
                            "status": "pass",
                            "decision": "pass",
                        }
                    },
                    answers={},
                )
            )

    def test_question_answered_by_two_suites_fails(self):
        mapping = _minimal_bundle()["suites"]["mapping"]
        duplicate = {**mapping, "questions": ["Q1"]}
        with pytest.raises(ReleaseEvidenceError, match="more than one suite"):
            validate_release_bundle(
                _minimal_bundle(suites={"mapping": mapping, "mapping2": duplicate})
            )

    def test_runtime_facts_outside_environment_fail(self):
        suite = {**_minimal_bundle()["suites"]["mapping"], "wall_time_s": 12.5}
        with pytest.raises(ReleaseEvidenceError, match="unknown fields"):
            validate_release_bundle(_minimal_bundle(suites={"mapping": suite}))

    def test_unknown_consumed_lifecycle_fails(self):
        consumed = [
            {
                "artifact": "x.json",
                "schema_version": "v1",
                "lifecycle": "fresh-enough",
                "supports": [],
            }
        ]
        with pytest.raises(ReleaseEvidenceError, match="lifecycle"):
            validate_release_bundle(_minimal_bundle(consumed_evidence=consumed))

    def test_unknown_pr83_recommendation_fails(self):
        with pytest.raises(ReleaseEvidenceError, match="pr83"):
            validate_release_bundle(_minimal_bundle(recommendation={"pr83": "ship_it"}))

    def test_answer_question_rejects_unregistered_and_bad_vocab(self):
        with pytest.raises(KeyError):
            answer_question("Q999", decision="pass", status="pass", evidence="x", reason="y")
        with pytest.raises(ReleaseEvidenceError, match="decision"):
            answer_question("Q1", decision="yolo", status="pass", evidence="x", reason="y")
        with pytest.raises(ReleaseEvidenceError, match="status"):
            answer_question("Q1", decision="pass", status="yolo", evidence="x", reason="y")
