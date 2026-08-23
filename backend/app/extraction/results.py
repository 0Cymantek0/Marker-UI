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

#: Specialist-proposal dispositions. A proposal is NEVER source
#: evidence; the disposition states how the authority-aware policy
#: classified it relative to the grounded candidates.
PROPOSAL_UNPROVED_REVIEW = "unproved_review"
PROPOSAL_CORROBORATED = "corroborated"
PROPOSAL_AGREES_WITH_SOURCE = "agrees_with_source"
PROPOSAL_CONFLICTS_WITH_SOURCE = "conflicts_with_source"
PROPOSAL_UNPARSEABLE = "unparseable"

PROPOSAL_DISPOSITIONS = frozenset(
    {
        PROPOSAL_UNPROVED_REVIEW,
        PROPOSAL_CORROBORATED,
        PROPOSAL_AGREES_WITH_SOURCE,
        PROPOSAL_CONFLICTS_WITH_SOURCE,
        PROPOSAL_UNPARSEABLE,
    }
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
class SpecialistProvenance:
    """The authorized context a specialist saw — a disclosure, not evidence.

    Records WHICH workspace/publication/packet/schema the specialist
    was shown and how much context traveled. Seeing source text never
    means the source states the model's normalized or inferred output;
    nothing here can back an :class:`EvidenceCitation`.
    """

    workspace_id: str
    publication_set_id: str
    packet_identity_id: str
    schema_identity: str
    route: str
    contract_version: str
    config_identity: str
    context_fingerprint: str
    context_unit_count: int
    context_char_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "publication_set_id": self.publication_set_id,
            "packet_identity_id": self.packet_identity_id,
            "schema_identity": self.schema_identity,
            "route": self.route,
            "contract_version": self.contract_version,
            "config_identity": self.config_identity,
            "context_fingerprint": self.context_fingerprint,
            "context_unit_count": self.context_unit_count,
            "context_char_count": self.context_char_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpecialistProvenance:
        return cls(
            workspace_id=data["workspace_id"],
            publication_set_id=data["publication_set_id"],
            packet_identity_id=data["packet_identity_id"],
            schema_identity=data["schema_identity"],
            route=data["route"],
            contract_version=data["contract_version"],
            config_identity=data["config_identity"],
            context_fingerprint=data["context_fingerprint"],
            context_unit_count=int(data["context_unit_count"]),
            context_char_count=int(data["context_char_count"]),
        )


@dataclass(frozen=True)
class ProposalView:
    """One trained-specialist proposal as it survives on a field outcome.

    Carries durable producer identity (who generated it, under which
    stable configuration) plus OUR independent typed parse of the raw
    value. Runtime observations (latency, retries, tokens, cache hits)
    are deliberately absent: they live on the lane report and never
    change the semantic meaning of a result.
    """

    producer_id: str
    producer_family: str
    config_identity: str
    value: str | None
    typed_value: str | int | None = None
    parse_error: str | None = None
    flags: tuple[str, ...] = ()
    disposition: str = PROPOSAL_UNPROVED_REVIEW

    def __post_init__(self) -> None:
        if self.disposition not in PROPOSAL_DISPOSITIONS:
            raise ValueError(
                f"invalid proposal disposition {self.disposition!r}; "
                f"allowed: {sorted(PROPOSAL_DISPOSITIONS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer_id": self.producer_id,
            "producer_family": self.producer_family,
            "config_identity": self.config_identity,
            "value": self.value,
            "typed_value": self.typed_value,
            "parse_error": self.parse_error,
            "flags": list(self.flags),
            "disposition": self.disposition,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProposalView:
        return cls(
            producer_id=data["producer_id"],
            producer_family=data["producer_family"],
            config_identity=data["config_identity"],
            value=data.get("value"),
            typed_value=data.get("typed_value"),
            parse_error=data.get("parse_error"),
            flags=tuple(data.get("flags") or ()),
            disposition=data.get("disposition") or PROPOSAL_UNPROVED_REVIEW,
        )


@dataclass(frozen=True)
class FieldOutcome:
    """The reconciled state of one field (or one row sub-field).

    ``proposals`` carries trained-specialist proposals that were NOT
    counted as witnesses: they are attributable, review-usable input,
    never source evidence. The key is omitted from serialization when
    empty so deterministic PR80A-only results keep their exact
    historical identity.
    """

    status: str
    value: str | int | None = None
    candidates: tuple[CandidateView, ...] = ()
    winner: str | None = None
    rule: str | None = None
    reason: str | None = None
    review: Mapping[str, Any] = field(default_factory=dict)
    proposals: tuple[ProposalView, ...] = ()

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
        payload: dict[str, Any] = {
            "status": self.status,
            "value": self.value,
            "candidates": [c.to_dict() for c in self.candidates],
            "winner": self.winner,
            "rule": self.rule,
            "reason": self.reason,
            "review": dict(self.review),
        }
        if self.proposals:
            payload["proposals"] = [p.to_dict() for p in self.proposals]
        return payload


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
class SpecialistRuntime:
    """Runtime observations of one specialist lane call.

    Deliberately excluded from result identity: latency, retry counts,
    token usage, and cache hits describe one visit, not the semantic
    meaning of the extraction. They are recorded for monitoring (NIST
    AI RMF third-party measurement), never for authority.
    """

    latency_ms: int
    attempts: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "from_cache": self.from_cache,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpecialistRuntime:
        return cls(
            latency_ms=int(data["latency_ms"]),
            attempts=int(data["attempts"]),
            prompt_tokens=data.get("prompt_tokens"),
            completion_tokens=data.get("completion_tokens"),
            from_cache=bool(data.get("from_cache", False)),
        )


@dataclass(frozen=True)
class SpecialistLaneReport:
    """The durable, inspectable report of one specialist lane invocation.

    Semantic content (status, producer identity, policy, context
    binding, counts) participates in result identity; runtime
    observations and raw error text do not. ``status`` is a closed
    vocabulary so malformed output, provider failure, replay miss, and
    context refusal are honest, distinguishable states.
    """

    status: str
    policy_id: str
    policy_version: str
    producer_id: str | None = None
    producer_family: str | None = None
    config_identity: str | None = None
    provenance: SpecialistProvenance | None = None
    proposal_count: int = 0
    unknown_fields: tuple[str, ...] = ()
    runtime: SpecialistRuntime | None = None
    error_detail: str | None = None

    def semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "producer_id": self.producer_id,
            "producer_family": self.producer_family,
            "config_identity": self.config_identity,
            "proposal_count": self.proposal_count,
            "unknown_fields": list(self.unknown_fields),
        }
        if self.provenance is not None:
            payload["provenance"] = self.provenance.to_dict()
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = self.semantic_payload()
        if self.runtime is not None:
            payload["runtime"] = self.runtime.to_dict()
        if self.error_detail is not None:
            payload["error_detail"] = self.error_detail
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpecialistLaneReport:
        provenance = data.get("provenance")
        runtime = data.get("runtime")
        return cls(
            status=data["status"],
            policy_id=data["policy_id"],
            policy_version=data["policy_version"],
            producer_id=data.get("producer_id"),
            producer_family=data.get("producer_family"),
            config_identity=data.get("config_identity"),
            provenance=(
                SpecialistProvenance.from_dict(provenance)
                if provenance is not None
                else None
            ),
            proposal_count=int(data.get("proposal_count", 0)),
            unknown_fields=tuple(data.get("unknown_fields") or ()),
            runtime=(
                SpecialistRuntime.from_dict(runtime) if runtime is not None else None
            ),
            error_detail=data.get("error_detail"),
        )


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
    specialist: SpecialistLaneReport | None = None

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
        persistence advanced the head. Specialist runtime observations
        (latency, attempts, tokens, cache hits) and raw provider error
        text are equally mechanics: a replayed specialist response
        yields the same identity as the live one.
        """
        return record_identity_hash(
            record_type="marker.extraction.result",
            schema_version=RESULT_SCHEMA_VERSION,
            payload=to_json_ready(self._semantic_payload()),
        )

    def _semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.specialist is not None:
            payload["specialist"] = self.specialist.semantic_payload()
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.specialist is not None:
            payload["specialist"] = self.specialist.to_dict()
        return payload


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
        proposals=tuple(
            ProposalView.from_dict(p) for p in data.get("proposals") or ()
        ),
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
        specialist=(
            SpecialistLaneReport.from_dict(data["specialist"])
            if data.get("specialist") is not None
            else None
        ),
    )
