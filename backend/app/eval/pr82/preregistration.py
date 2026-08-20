"""Preregistered Quality Lab questions and decision rules (PR82A).

The questions, their decision rules, and the decision vocabulary below
are frozen BEFORE any PR82 suite result is interpreted. A result may
only answer a preregistered question id; discovering a new question
mid-session requires adding it here (with its own rule) and marking it
``added_after_results`` honestly in the report.

Decision vocabulary (stable machine-readable meanings):

``pass``
    The declared invariant held under every executed check.
``promote_narrow``
    The claim may advance, scoped exactly as measured.
``shadow``
    Implemented and measured, but explicitly not promoted.
``non_promoted``
    Evidence does not support promotion; the negative result stands.
``kill_or_simplify``
    The measured mechanism should be removed or narrowed.
``blocked``
    A specific, reproducible gap blocks the invariant.
``inconclusive``
    Environment or sample support cannot decide the question.
``characterization_only``
    Measurements describe behavior; no correctness claim is made.
"""

from __future__ import annotations

from app.utils.canonical import record_identity_hash, to_json_ready

PREREGISTRATION_SCHEMA_VERSION = "marker.pr82_preregistration.v1"

DECISION_PASS = "pass"
DECISION_PROMOTE_NARROW = "promote_narrow"
DECISION_SHADOW = "shadow"
DECISION_NON_PROMOTED = "non_promoted"
DECISION_KILL_OR_SIMPLIFY = "kill_or_simplify"
DECISION_BLOCKED = "blocked"
DECISION_INCONCLUSIVE = "inconclusive"
DECISION_CHARACTERIZATION_ONLY = "characterization_only"

DECISION_VOCABULARY: frozenset[str] = frozenset(
    {
        DECISION_PASS,
        DECISION_PROMOTE_NARROW,
        DECISION_SHADOW,
        DECISION_NON_PROMOTED,
        DECISION_KILL_OR_SIMPLIFY,
        DECISION_BLOCKED,
        DECISION_INCONCLUSIVE,
        DECISION_CHARACTERIZATION_ONLY,
    }
)

#: Result status per check/suite. Kept distinct from decisions: a suite
#: can ``pass`` its checks while the promotion decision stays ``shadow``.
STATUS_PASS = "pass"
STATUS_SHADOW = "shadow"
STATUS_NON_PROMOTION = "non_promotion"
STATUS_FAIL = "fail"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_CHARACTERIZATION_ONLY = "characterization_only"

STATUS_VOCABULARY: frozenset[str] = frozenset(
    {
        STATUS_PASS,
        STATUS_SHADOW,
        STATUS_NON_PROMOTION,
        STATUS_FAIL,
        STATUS_INCONCLUSIVE,
        STATUS_CHARACTERIZATION_ONLY,
    }
)

#: How a check was executed. ``live`` and ``machine_dependent`` results
#: carry environment metadata and never enter semantic identity.
MODE_DETERMINISTIC = "deterministic"
MODE_REPLAY = "replay"
MODE_LIVE = "live"
MODE_MACHINE_DEPENDENT = "machine_dependent"
MODE_UNAVAILABLE = "unavailable"

MODE_VOCABULARY: frozenset[str] = frozenset(
    {
        MODE_DETERMINISTIC,
        MODE_REPLAY,
        MODE_LIVE,
        MODE_MACHINE_DEPENDENT,
        MODE_UNAVAILABLE,
    }
)

#: Suite domains named by the masterplan for PR82.
DOMAIN_MAPPING = "mapping"
DOMAIN_DEPENDENCE = "dependence"
DOMAIN_INCREMENTAL = "incremental"
DOMAIN_RUNTIME = "runtime"
DOMAIN_AGENT = "agent"
DOMAIN_CARRY_FORWARD = "carry_forward"
DOMAIN_REGRESSION = "regression"

DOMAINS: frozenset[str] = frozenset(
    {
        DOMAIN_MAPPING,
        DOMAIN_DEPENDENCE,
        DOMAIN_INCREMENTAL,
        DOMAIN_RUNTIME,
        DOMAIN_AGENT,
        DOMAIN_CARRY_FORWARD,
        DOMAIN_REGRESSION,
    }
)


# ---------------------------------------------------------------------------
# Preregistered questions
# ---------------------------------------------------------------------------


class PreregisteredQuestion:
    """One frozen question with its pre-committed decision rule."""

    __slots__ = ("question_id", "domain", "question", "decision_rule")

    def __init__(self, question_id: str, domain: str, question: str, decision_rule: str) -> None:
        if domain not in DOMAINS:
            raise ValueError(f"unknown domain {domain!r}; allowed: {sorted(DOMAINS)}")
        self.question_id = question_id
        self.domain = domain
        self.question = question
        self.decision_rule = decision_rule

    def semantic_payload(self) -> dict:
        return {
            "question_id": self.question_id,
            "domain": self.domain,
            "question": self.question,
            "decision_rule": self.decision_rule,
        }


#: The frozen question set. Order is part of the preregistration
#: identity; appending is allowed, reordering/rewording is not.
PREREGISTERED_QUESTIONS: tuple[PreregisteredQuestion, ...] = (
    PreregisteredQuestion(
        question_id="Q1",
        domain=DOMAIN_MAPPING,
        question="Can a source citation move across revisions without a false identity claim?",
        decision_rule=(
            "Mapping corpus: every adversarial case yields a disposition in "
            "its expected set; no case whose expected set excludes 'exact' "
            "receives 'exact' or 'mapped_deterministic'; zero silent source "
            "identity changes."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q2",
        domain=DOMAIN_MAPPING,
        question="Does semantic similarity ever get promoted to exact identity?",
        decision_rule=(
            "Paraphrase, normalized-text, fuzzy, containment, duplicate and "
            "geometry cases may only yield 'mapped_semantic_candidate', "
            "'stale' or 'unresolved'; any deterministic/exact result on "
            "these cases fails the question."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q3",
        domain=DOMAIN_INCREMENTAL,
        question="Does incremental rebuild still equal clean rebuild after longer mixed change sequences?",
        decision_rule=(
            "Every recorded seed's incremental result equals an independent "
            "clean rebuild under the declared output family, or fails "
            "closed; unknown dependencies widen work rather than narrow "
            "correctness."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q4",
        domain=DOMAIN_INCREMENTAL,
        question="Does redaction or policy change leave a stale derived/index/cursor route?",
        decision_rule=(
            "After a declared policy/revision change, pre-change cursors and "
            "derived routes return structured stale/invalidated outcomes, "
            "not stale data."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q5",
        domain=DOMAIN_DEPENDENCE,
        question="Can dependent model witnesses accidentally satisfy a high-risk verification policy?",
        decision_rule=(
            "Held-out adversarial slice: dependent-witness configurations "
            "must not reach acceptance on high-risk policies; false "
            "verification counts and abstentions are reported with counts, "
            "not badges."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q6",
        domain=DOMAIN_DEPENDENCE,
        question="Do missing lineage, expired evidence, NaN/Inf values, or partial support fail closed?",
        decision_rule=(
            "Each pathological input is rejected or made non-authoritative "
            "at the boundary; none may enter a risk score or identity."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q7",
        domain=DOMAIN_RUNTIME,
        question="Can crash/cancel/disk/handle/model-service faults create a false completion or accepted stale publication?",
        decision_rule=(
            "Across the executed fault matrix there are zero false "
            "'completed' states and zero stale accepted publications; "
            "duplicate execution may occur, duplicate accepted truth may "
            "not."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q8",
        domain=DOMAIN_RUNTIME,
        question="Can a slow or disconnected agent client affect job truth?",
        decision_rule=(
            "A slow/stalled consumer never blocks execution or terminal "
            "truth, and reconnect resumes from durable sequence identity."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q9",
        domain=DOMAIN_AGENT,
        question="Can hostile retrieved document content change authorization, tool identity, policy, or source truth?",
        decision_rule=(
            "Every hostile-document fixture leaves authorization decisions, "
            "tool scope checks, kernel truth and citations byte-identical "
            "to the non-hostile control; untrusted content gains no "
            "authority."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q10",
        domain=DOMAIN_AGENT,
        question="Does bounded server-side query produce useful cited task results with less work than manual traversal?",
        decision_rule=(
            "On the frozen task set, the bounded marker_query path "
            "completes each task with correct citations while processing "
            "fewer evidence units than the full-document baseline."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q11",
        domain=DOMAIN_CARRY_FORWARD,
        question="Does the PR81 reranker result still look useful when checked against a small external or shifted slice?",
        decision_rule=(
            "If an executable external slice is available this session, "
            "compare rerank vs no-rerank page selection with a text-easy "
            "control; otherwise record the designed probe and the exact "
            "missing prerequisite (license/download) as inconclusive."
        ),
    ),
    PreregisteredQuestion(
        question_id="Q12",
        domain=DOMAIN_CARRY_FORWARD,
        question="Which current claims remain conditional rather than promotable?",
        decision_rule=(
            "The release ledger enumerates every carried claim with status "
            "from the decision vocabulary; a claim is promotable only with "
            "current, reproducible evidence."
        ),
    ),
)


def preregistration_identity() -> str:
    """Deterministic identity of the frozen question set.

    Recorded in the release bundle so a later reader can verify the
    answered questions are exactly the preregistered ones.
    """
    payload = {
        "questions": [question.semantic_payload() for question in PREREGISTERED_QUESTIONS]
    }
    return record_identity_hash(
        record_type=PREREGISTRATION_SCHEMA_VERSION,
        schema_version="1.0.0",
        payload=to_json_ready(payload),
    )


def question_by_id(question_id: str) -> PreregisteredQuestion:
    for question in PREREGISTERED_QUESTIONS:
        if question.question_id == question_id:
            return question
    raise KeyError(
        f"question {question_id!r} is not preregistered; add it to "
        "PREREGISTERED_QUESTIONS with a decision rule before answering"
    )
