"""PR82 release-evidence bundle shape and validation.

One machine-readable artifact joins the Quality Lab suites without
pretending they have equal strength. The bundle separates:

* semantic content (identities, decisions, counts, blockers) —
  reproducible from frozen inputs;
* environment/runtime metadata (python, platform, machine, wall time) —
  never part of semantic identity;
* per-suite execution mode (deterministic / replay / live /
  machine_dependent / unavailable) so a reader can tell what is proven
  versus characterized versus missing.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.eval.pr82.preregistration import (
    DECISION_VOCABULARY,
    MODE_VOCABULARY,
    PREREGISTERED_QUESTIONS,
    PREREGISTRATION_SCHEMA_VERSION,
    STATUS_VOCABULARY,
    preregistration_identity,
    question_by_id,
)

RELEASE_EVIDENCE_SCHEMA_VERSION = "marker.pr82_release_evidence.v1"

PR83_READY = "ready_for_pr83"
PR83_READY_WITH_SCOPED_NON_PROMOTIONS = "ready_with_scoped_non_promotions"
PR83_NEEDS_TARGETED_PR82B = "needs_targeted_pr82b"
PR83_BLOCKED_BY_SEMANTIC_GAP = "blocked_by_semantic_gap"
PR83_INCONCLUSIVE = "inconclusive_due_to_environment_or_sample_support"

PR83_RECOMMENDATION_VOCABULARY: frozenset[str] = frozenset(
    {
        PR83_READY,
        PR83_READY_WITH_SCOPED_NON_PROMOTIONS,
        PR83_NEEDS_TARGETED_PR82B,
        PR83_BLOCKED_BY_SEMANTIC_GAP,
        PR83_INCONCLUSIVE,
    }
)

#: Lifecycle of a consumed prior artifact.
EVIDENCE_CURRENT = "current"
EVIDENCE_STALE = "stale"
EVIDENCE_SUPERSEDED = "superseded"
EVIDENCE_LIFECYCLE: frozenset[str] = frozenset(
    {EVIDENCE_CURRENT, EVIDENCE_STALE, EVIDENCE_SUPERSEDED}
)

_ALLOWED_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "git_sha",
        "planning_head",
        "preregistration_identity",
        "environment",
        "consumed_evidence",
        "suites",
        "answers",
        "readiness_invariants",
        "recommendation",
        "reproduce",
        "limitations",
    }
)
_ALLOWED_SUITE_KEYS = frozenset(
    {"questions", "mode", "status", "decision", "reason", "checks", "findings", "blockers"}
)
_ALLOWED_ANSWER_KEYS = frozenset({"decision", "status", "evidence", "reason"})
_ALLOWED_CONSUMED_KEYS = frozenset({"artifact", "schema_version", "lifecycle", "supports"})
_ALLOWED_INVARIANT_KEYS = frozenset({"invariant", "status", "evidence"})


class ReleaseEvidenceError(ValueError):
    """Raised when a bundle violates the evidence contract."""


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseEvidenceError(f"{field} must be a mapping, got {type(value).__name__}")
    return value


def _require_vocab(value: Any, vocabulary: frozenset[str], field: str) -> str:
    if value not in vocabulary:
        raise ReleaseEvidenceError(
            f"{field}: unknown value {value!r}; allowed: {sorted(vocabulary)}"
        )
    return value


def validate_release_bundle(bundle: Mapping[str, Any]) -> None:
    """Fail-closed structural and vocabulary validation.

    Enforces that every answered question is preregistered, every suite
    answer carries a decision from the frozen vocabulary, and runtime
    metadata stays inside the environment block.
    """
    _require_mapping(bundle, "bundle")
    unknown = set(bundle) - _ALLOWED_ROOT_KEYS
    if unknown:
        raise ReleaseEvidenceError(f"unknown root fields {sorted(unknown)}")
    if bundle.get("schema_version") != RELEASE_EVIDENCE_SCHEMA_VERSION:
        raise ReleaseEvidenceError(
            f"schema_version must be {RELEASE_EVIDENCE_SCHEMA_VERSION!r}"
        )
    for field in ("git_sha", "planning_head"):
        if not isinstance(bundle.get(field), str) or not bundle[field]:
            raise ReleaseEvidenceError(f"{field} must be a non-empty string")
    if bundle.get("preregistration_identity") != preregistration_identity():
        raise ReleaseEvidenceError(
            "preregistration_identity does not match the frozen question "
            "set; answers must map onto the preregistered questions"
        )
    environment = _require_mapping(bundle.get("environment", {}), "environment")
    unknown_env = set(environment) - {"python", "platform", "machine", "cpu_count", "notes"}
    if unknown_env:
        raise ReleaseEvidenceError(
            f"environment declares non-metadata keys {sorted(unknown_env)}; "
            "runtime facts are metadata and must stay in known fields"
        )

    registered = {question.question_id for question in PREREGISTERED_QUESTIONS}

    consumed = bundle.get("consumed_evidence", [])
    if not isinstance(consumed, list):
        raise ReleaseEvidenceError("consumed_evidence must be a list")
    for entry in consumed:
        entry = _require_mapping(entry, "consumed_evidence entry")
        unknown = set(entry) - _ALLOWED_CONSUMED_KEYS
        if unknown:
            raise ReleaseEvidenceError(f"consumed_evidence entry has unknown fields {sorted(unknown)}")
        _require_vocab(entry.get("lifecycle"), EVIDENCE_LIFECYCLE, "consumed_evidence.lifecycle")
        for question_id in entry.get("supports", ()):
            if question_id not in registered:
                raise ReleaseEvidenceError(
                    f"consumed_evidence supports unregistered question {question_id!r}"
                )

    suites = _require_mapping(bundle.get("suites", {}), "suites")
    answered: set[str] = set()
    for suite_name, suite in suites.items():
        suite = _require_mapping(suite, f"suites.{suite_name}")
        unknown = set(suite) - _ALLOWED_SUITE_KEYS
        if unknown:
            raise ReleaseEvidenceError(
                f"suites.{suite_name} has unknown fields {sorted(unknown)}"
            )
        _require_vocab(suite.get("mode"), MODE_VOCABULARY, f"suites.{suite_name}.mode")
        _require_vocab(suite.get("status"), STATUS_VOCABULARY, f"suites.{suite_name}.status")
        _require_vocab(suite.get("decision"), DECISION_VOCABULARY, f"suites.{suite_name}.decision")
        for question_id in suite.get("questions", ()):
            if question_id not in registered:
                raise ReleaseEvidenceError(
                    f"suites.{suite_name} answers unregistered question {question_id!r}"
                )
            if question_id in answered:
                raise ReleaseEvidenceError(
                    f"question {question_id!r} is answered by more than one suite"
                )
            answered.add(question_id)

    answers = _require_mapping(bundle.get("answers", {}), "answers")
    for question_id, answer in answers.items():
        if question_id not in registered:
            raise ReleaseEvidenceError(f"answers contain unregistered question {question_id!r}")
        if question_id not in answered:
            raise ReleaseEvidenceError(
                f"question {question_id!r} is answered but claimed by no suite"
            )
        answer = _require_mapping(answer, f"answers.{question_id}")
        unknown = set(answer) - _ALLOWED_ANSWER_KEYS
        if unknown:
            raise ReleaseEvidenceError(
                f"answers.{question_id} has unknown fields {sorted(unknown)}"
            )
        _require_vocab(answer.get("decision"), DECISION_VOCABULARY, f"answers.{question_id}.decision")

    invariants = bundle.get("readiness_invariants", [])
    if not isinstance(invariants, list):
        raise ReleaseEvidenceError("readiness_invariants must be a list")
    for entry in invariants:
        entry = _require_mapping(entry, "readiness_invariants entry")
        unknown = set(entry) - _ALLOWED_INVARIANT_KEYS
        if unknown:
            raise ReleaseEvidenceError(
                f"readiness_invariants entry has unknown fields {sorted(unknown)}"
            )
        _require_vocab(
            entry.get("status"), STATUS_VOCABULARY, "readiness_invariants.status"
        )

    recommendation = _require_mapping(bundle.get("recommendation", {}), "recommendation")
    if "pr83" in recommendation:
        _require_vocab(
            recommendation["pr83"],
            PR83_RECOMMENDATION_VOCABULARY,
            "recommendation.pr83",
        )


def answer_question(
    question_id: str,
    *,
    decision: str,
    status: str,
    evidence: str,
    reason: str,
) -> dict[str, Any]:
    """Build one answer entry, checking the question is preregistered."""
    question_by_id(question_id)  # raises KeyError for unregistered ids
    _require_vocab(decision, DECISION_VOCABULARY, "decision")
    _require_vocab(status, STATUS_VOCABULARY, "status")
    return {
        "decision": decision,
        "status": status,
        "evidence": evidence,
        "reason": reason,
    }


__all__ = [
    "PREREGISTRATION_SCHEMA_VERSION",
    "RELEASE_EVIDENCE_SCHEMA_VERSION",
    "PR83_RECOMMENDATION_VOCABULARY",
    "PR83_READY",
    "PR83_READY_WITH_SCOPED_NON_PROMOTIONS",
    "PR83_NEEDS_TARGETED_PR82B",
    "PR83_BLOCKED_BY_SEMANTIC_GAP",
    "PR83_INCONCLUSIVE",
    "ReleaseEvidenceError",
    "answer_question",
    "validate_release_bundle",
]
