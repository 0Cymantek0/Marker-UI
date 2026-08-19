"""Extraction result contract: honest, machine-readable outcomes (PR80A).

The result model separates *what the evidence supports* from *what the
run produced*. Every material field carries its full candidate set and
the evidence citations behind each candidate, so a reviewer can always
see both agreeing and conflicting readings instead of a final value
with hidden history.

Outcome honesty is structural: run/field/item statuses form a closed
vocabulary, and serialization round-trips deterministically so a stored
result's identity is comparable across processes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.utils.canonical import record_identity_hash, to_json_ready

RESULT_SCHEMA_VERSION = "marker.extraction.result.v1"

#: Field/row-level outcomes. ``accepted`` requires grounded support;
#: ``corrected`` records a review correction (human-sourced, never
#: authority-bearing); ``rejected`` records an explicit adjudication
#: against the value; ``unresolved`` preserves a live conflict;
#: ``review_required`` escalates per policy; ``missing`` never invents a
#: value; ``invalid`` marks structurally bad candidates.
FIELD_OUTCOME_ACCEPTED = "accepted"
FIELD_OUTCOME_CORRECTED = "corrected"
FIELD_OUTCOME_REJECTED = "rejected"
FIELD_OUTCOME_UNRESOLVED = "unresolved"
FIELD_OUTCOME_REVIEW_REQUIRED = "review_required"
FIELD_OUTCOME_MISSING = "missing"
FIELD_OUTCOME_INVALID = "invalid"

FIELD_OUTCOMES = frozenset(
    {
        FIELD_OUTCOME_ACCEPTED,
        FIELD_OUTCOME_CORRECTED,
        FIELD_OUTCOME_REJECTED,
        FIELD_OUTCOME_UNRESOLVED,
        FIELD_OUTCOME_REVIEW_REQUIRED,
        FIELD_OUTCOME_MISSING,
        FIELD_OUTCOME_INVALID,
    }
)

#: Usable outcomes: the value may flow downstream (with ``corrected``
#: still carrying its human-source qualifier).
USABLE_FIELD_OUTCOMES = frozenset({FIELD_OUTCOME_ACCEPTED, FIELD_OUTCOME_CORRECTED})

#: Run-level statuses. Mirrors the outcome-honesty requirements of the
#: PR80A plan: success, partial usability, review escalation, invalid
#: request, stale context, authorization denial, and execution failure
#: are never conflated.
RUN_ACCEPTED = "accepted"
RUN_PARTIAL = "partial"
RUN_REVIEW_REQUIRED = "review_required"
RUN_INVALID_REQUEST = "invalid_request"
RUN_STALE_CONTEXT = "stale_context"
RUN_DENIED = "policy_fail_closed"
RUN_EXECUTION_FAILURE = "execution_failure"

RUN_STATUSES = frozenset(
    {
        RUN_ACCEPTED,
        RUN_PARTIAL,
        RUN_REVIEW_REQUIRED,
        RUN_INVALID_REQUEST,
        RUN_STALE_CONTEXT,
        RUN_DENIED,
        RUN_EXECUTION_FAILURE,
    }
)

#: Invariant findings: an invariant can only pass when the values it
#: compares were actually accepted — a document-level total is never
#: proof that incomplete row evidence was fine.
INVARIANT_SATISFIED = "satisfied"
INVARIANT_VIOLATED = "violated"
INVARIANT_NOT_EVALUABLE = "not_evaluable"

INVARIANT_FINDINGS = frozenset(
    {INVARIANT_SATISFIED, INVARIANT_VIOLATED, INVARIANT_NOT_EVALUABLE}
)


@dataclass(frozen=True)
class EvidenceCitation:
    """One evidence unit a candidate is grounded in.

    Carries the full authoritative locator chain — the kernel record,
    its revision, and the publication/generation the packet pinned —
    plus the packet identity that served it, so staleness detection
    never depends on the extraction's own memory.
    """

    record_id: str
    revision_ref: str
    text_hash: str
    node_id: str | None
    publication_set_id: str
    materialized_generation_id: str
    packet_identity_id: str
    op: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "revision_ref": self.revision_ref,
            "text_hash": self.text_hash,
            "node_id": self.node_id,
            "publication_set_id": self.publication_set_id,
            "materialized_generation_id": self.materialized_generation_id,
            "packet_identity_id": self.packet_identity_id,
            "op": self.op,
        }

    @property
    def witness_key(self) -> tuple[str, str]:
        """The independence unit: same record at same revision is ONE witness."""
        return (self.record_id, self.revision_ref)


@dataclass(frozen=True)
class CandidateView:
    """One grounded candidate for one field, as the route produced it.

    ``value`` is the canonically typed parse (string for text/enum,
    ISO date string, decimal string, or plain int string) or ``None``
    when the raw text failed typed parsing — an unparsable candidate is
    evidence too, and reconciliation must see it rather than have it
    silently dropped.
    """

    raw_text: str
    value: str | int | None
    evidence: tuple[EvidenceCitation, ...]
    derivation: Mapping[str, Any]
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "value": self.value,
            "evidence": [cite.to_dict() for cite in self.evidence],
            "derivation": dict(self.derivation),
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True)
class FieldOutcome:
    """The reconciled state of one field (or one row sub-field)."""

    status: str
    value: str | int | None = None
    candidates: tuple[CandidateView, ...] = ()
    winner: str | None = None
    rule: str | None = None
    reason: str | None = None
    review: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in FIELD_OUTCOMES:
            raise ValueError(
                f"invalid field outcome status {self.status!r}; "
                f"allowed: {sorted(FIELD_OUTCOMES)}"
            )

    @property
    def witness_keys(self) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for candidate in self.candidates:
            for cite in candidate.evidence:
                keys.add(cite.witness_key)
        return keys

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "candidates": [c.to_dict() for c in self.candidates],
            "winner": self.winner,
            "rule": self.rule,
            "reason": self.reason,
            "review": dict(self.review),
        }


@dataclass(frozen=True)
class InvariantFinding:
    """The evaluated state of one schema invariant for one run."""

    kind: str
    target: str
    finding: str
    detail: str

    def __post_init__(self) -> None:
        if self.finding not in INVARIANT_FINDINGS:
            raise ValueError(
                f"invalid invariant finding {self.finding!r}; "
                f"allowed: {sorted(INVARIANT_FINDINGS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "finding": self.finding,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ItemOutcome:
    """One reconciled line-item row, identified by its identity keys."""

    identity: Mapping[str, Any]
    status: str
    fields: Mapping[str, FieldOutcome]

    def __post_init__(self) -> None:
        if self.status not in FIELD_OUTCOMES:
            raise ValueError(
                f"invalid item outcome status {self.status!r}; "
                f"allowed: {sorted(FIELD_OUTCOMES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "status": self.status,
            "fields": {name: out.to_dict() for name, out in self.fields.items()},
        }


@dataclass(frozen=True)
class ExtractionContext:
    """The authoritative context a result was computed against."""

    workspace_id: str
    publication_set_id: str
    materialized_generation_id: str
    kernel_snapshot_commit_id: int
    packet_identity_ids: tuple[str, ...]
    policy_id: str
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "publication_set_id": self.publication_set_id,
            "materialized_generation_id": self.materialized_generation_id,
            "kernel_snapshot_commit_id": self.kernel_snapshot_commit_id,
            "packet_identity_ids": list(self.packet_identity_ids),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }


def typed_value_to_json(value: str | int | None) -> str | int | None:
    """Render a typed value for canonical serialization.

    Dates and decimals already travel as canonical strings; ints stay
    ints. ``None`` means "no typed value" (missing or unparsable) and
    is kept distinct from every real value, including ``0``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("booleans are not an extraction value type")
    if isinstance(value, (int, str)):
        return value
    raise ValueError(f"unserializable typed value: {value!r}")


@dataclass(frozen=True)
class ExtractionResult:
    """The complete, auditable outcome of one extraction run."""

    schema_id: str
    schema_version: str
    schema_identity: str
    context: ExtractionContext
    run_status: str
    fields: Mapping[str, FieldOutcome]
    line_items: Mapping[str, tuple[ItemOutcome, ...]]
    invariants: tuple[InvariantFinding, ...]
    error: str | None = None

    def __post_init__(self) -> None:
        if self.run_status not in RUN_STATUSES:
            raise ValueError(
                f"invalid run status {self.run_status!r}; "
                f"allowed: {sorted(RUN_STATUSES)}"
            )

    @property
    def identity(self) -> str:
        """Deterministic identity of this result's semantic content.

        Covers WHAT was extracted and against WHICH publication/policy —
        deliberately excluding the kernel commit head and packet
        identity ids, which describe the retrieval mechanics of one
        visit: a deterministic rerun over the same frozen truth must
        produce the same identity even though the first run's own
        persistence advanced the head.
        """
        return record_identity_hash(
            record_type="marker.extraction.result",
            schema_version=RESULT_SCHEMA_VERSION,
            payload=to_json_ready(self._semantic_payload()),
        )

    def _semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "schema_identity": self.schema_identity,
            "publication": {
                "publication_set_id": self.context.publication_set_id,
                "materialized_generation_id": self.context.materialized_generation_id,
                "policy_id": self.context.policy_id,
                "policy_version": self.context.policy_version,
            },
            "run_status": self.run_status,
            "fields": {name: out.to_dict() for name, out in self.fields.items()},
            "line_items": {
                name: [item.to_dict() for item in items]
                for name, items in self.line_items.items()
            },
            "invariants": [inv.to_dict() for inv in self.invariants],
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "schema_id": self.schema_id,
            "schema_version_label": self.schema_version,
            "schema_identity": self.schema_identity,
            "context": self.context.to_dict(),
            "run_status": self.run_status,
            "fields": {name: out.to_dict() for name, out in self.fields.items()},
            "line_items": {
                name: [item.to_dict() for item in items]
                for name, items in self.line_items.items()
            },
            "invariants": [inv.to_dict() for inv in self.invariants],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Deserialization (stored results must round-trip for review/revalidation)
# ---------------------------------------------------------------------------


def _citation_from_dict(data: Mapping[str, Any]) -> EvidenceCitation:
    return EvidenceCitation(
        record_id=data["record_id"],
        revision_ref=data["revision_ref"],
        text_hash=data["text_hash"],
        node_id=data.get("node_id"),
        publication_set_id=data["publication_set_id"],
        materialized_generation_id=data["materialized_generation_id"],
        packet_identity_id=data["packet_identity_id"],
        op=data["op"],
    )


def _candidate_from_dict(data: Mapping[str, Any]) -> CandidateView:
    return CandidateView(
        raw_text=data["raw_text"],
        value=data["value"],
        evidence=tuple(_citation_from_dict(c) for c in data["evidence"]),
        derivation=dict(data["derivation"]),
        parse_error=data.get("parse_error"),
    )


def _field_outcome_from_dict(data: Mapping[str, Any]) -> FieldOutcome:
    return FieldOutcome(
        status=data["status"],
        value=data.get("value"),
        candidates=tuple(_candidate_from_dict(c) for c in data["candidates"]),
        winner=data.get("winner"),
        rule=data.get("rule"),
        reason=data.get("reason"),
        review=dict(data.get("review") or {}),
    )


def _item_outcome_from_dict(data: Mapping[str, Any]) -> ItemOutcome:
    return ItemOutcome(
        identity=dict(data["identity"]),
        status=data["status"],
        fields={
            name: _field_outcome_from_dict(out) for name, out in data["fields"].items()
        },
    )


def result_from_dict(data: Mapping[str, Any]) -> ExtractionResult:
    """Rebuild a stored result, failing closed on unknown shapes."""
    if not isinstance(data, Mapping):
        raise ValueError(
            f"stored result must be a mapping, got {type(data).__name__}"
        )
    if data.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported result schema_version {data.get('schema_version')!r}"
        )
    context_data = data["context"]
    context = ExtractionContext(
        workspace_id=context_data["workspace_id"],
        publication_set_id=context_data["publication_set_id"],
        materialized_generation_id=context_data["materialized_generation_id"],
        kernel_snapshot_commit_id=int(context_data["kernel_snapshot_commit_id"]),
        packet_identity_ids=tuple(context_data["packet_identity_ids"]),
        policy_id=context_data["policy_id"],
        policy_version=context_data["policy_version"],
    )
    return ExtractionResult(
        schema_id=data["schema_id"],
        schema_version=data["schema_version_label"],
        schema_identity=data["schema_identity"],
        context=context,
        run_status=data["run_status"],
        fields={
            name: _field_outcome_from_dict(out)
            for name, out in data["fields"].items()
        },
        line_items={
            name: tuple(_item_outcome_from_dict(item) for item in items)
            for name, items in data["line_items"].items()
        },
        invariants=tuple(
            InvariantFinding(
                kind=inv["kind"],
                target=inv["target"],
                finding=inv["finding"],
                detail=inv["detail"],
            )
            for inv in data["invariants"]
        ),
        error=data.get("error"),
    )
