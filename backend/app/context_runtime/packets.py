"""EvidencePacket representation, structural budgets, and identity (PR77/PR78).

An EvidencePacket is retrieval/context evidence with explicit
provenance — never an answer-correctness or entailment claim. Assembly
operates on indivisible structural units: a unit is either included
whole or omitted with an explicit reason; output is never cut through
the middle of a unit while being presented as valid.

Packet identity is deterministic over semantic dimensions only
(normalized query, publication/generation attribution, evidence
locators and content hashes, omissions, budget profile, the
security/verifier/redaction/serialization context, and — since PR78 —
the trusted effective-authorization identity). Runtime-only values
never enter identity, so identical state plus identical inputs
reproduce the identical packet, and any authorization change that can
change what evidence is legally visible invalidates reuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.utils.canonical import (
    canonical_json_bytes,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "EVIDENCE_PACKET_SCHEMA_VERSION",
    "BudgetReport",
    "EvidenceLocator",
    "EvidencePacket",
    "EvidenceUnit",
    "CandidateUnit",
    "OmittedEvidence",
    "assemble_packet",
    "packet_identity_dimensions",
    "to_json",
]

#: Framing identity of the packet representation itself; changing the
#: packet shape must change this version (and therefore every identity).
EVIDENCE_PACKET_SCHEMA_VERSION = "marker.evidence_packet.v1"
_PACKET_RECORD_TYPE = "marker.context_runtime.evidence_packet"
_PACKET_ID_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class EvidenceLocator:
    """How one evidence unit resolves back to source truth."""

    publication_set_id: str
    materialized_generation_id: str
    lexical_generation_id: str
    record_id: str
    view_id: str
    node_id: str | None
    revision_ref: str
    text_hash: str
    row_index: int | None

    def identity_view(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "view_id": self.view_id,
            "node_id": self.node_id,
            "revision_ref": self.revision_ref,
            "text_hash": self.text_hash,
            "row_index": self.row_index,
        }


@dataclass(frozen=True)
class CandidateUnit:
    """A retrieval candidate offered to packet assembly. Carries the
    full provenance of where it came from plus its indivisible
    serialized cost."""

    operation_index: int
    op: str
    locator: EvidenceLocator
    text: str | None
    rank: float | None


@dataclass(frozen=True)
class EvidenceUnit:
    """One indivisible included evidence unit."""

    operation_index: int
    op: str
    locator: EvidenceLocator
    text: str | None
    rank: float | None
    chars: int

    def view(self) -> dict[str, Any]:
        return {
            "operation_index": self.operation_index,
            "op": self.op,
            **self.locator.identity_view(),
            "publication_set_id": self.locator.publication_set_id,
            "materialized_generation_id": self.locator.materialized_generation_id,
            "lexical_generation_id": self.locator.lexical_generation_id,
            "text": self.text,
            "rank": self.rank,
            "chars": self.chars,
        }


@dataclass(frozen=True)
class OmittedEvidence:
    """An explicit omission: what was withheld and why. Omitted units
    are never silently presented as if the search were exhaustive."""

    operation_index: int
    op: str
    reason: str
    detail: str

    def view(self) -> dict[str, Any]:
        return {
            "operation_index": self.operation_index,
            "op": self.op,
            "reason": self.reason,
            "detail": self.detail,
        }


#: Omission reasons that mark a budget-limited (partial) execution.
_BUDGET_REASONS = frozenset(
    {"candidate_budget", "output_budget", "unit_budget", "unit_too_large"}
)


@dataclass(frozen=True)
class BudgetReport:
    """What the execution was allowed and what it consumed."""

    max_operations: int
    max_candidates: int
    max_evidence_units: int
    max_output_chars: int
    operations_executed: int
    candidates_considered: int
    units_included: int
    units_omitted: int
    output_chars: int
    truncated: bool

    def view(self) -> dict[str, Any]:
        return {
            "max_operations": self.max_operations,
            "max_candidates": self.max_candidates,
            "max_evidence_units": self.max_evidence_units,
            "max_output_chars": self.max_output_chars,
            "operations_executed": self.operations_executed,
            "candidates_considered": self.candidates_considered,
            "units_included": self.units_included,
            "units_omitted": self.units_omitted,
            "output_chars": self.output_chars,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class EvidencePacket:
    """A bounded, attributable evidence response over one pinned
    PublicationSet (or an explicit empty response for an unpublished
    workspace). Contains no timestamps or runtime-only values: the
    packet is a pure function of request plus published state plus
    effective authorization state. ``authorization`` is the
    caller-safe identity view of the trusted authorization that shaped
    delivery (profile, assurance, epoch, deny revision, policy digest)
    — enough to invalidate reuse, never enough to reveal the domain or
    denial topology behind the digest."""

    schema_version: str
    status: str  # "complete" | "partial"
    publication_status: str  # "published" | "unpublished"
    query: dict[str, Any]
    publication: dict[str, Any] | None
    evidence: tuple[EvidenceUnit, ...]
    omitted: tuple[OmittedEvidence, ...]
    budget: BudgetReport
    context: dict[str, Any]
    authorization: dict[str, Any] | None
    identity_dimensions: dict[str, Any]
    identity_id: str

    @property
    def partial(self) -> bool:
        return self.status == "partial"


def _unit_cost(candidate: CandidateUnit, include_text: bool) -> int:
    """Serialized size of one candidate unit under canonical JSON — the
    indivisible structural cost used for output budgeting. The bm25
    ``rank`` float is excluded: the canonical identity contract rejects
    floats, and it does not change structural size meaningfully."""
    view = {
        "operation_index": candidate.operation_index,
        "op": candidate.op,
        **candidate.locator.identity_view(),
        "text": candidate.text if include_text else None,
    }
    return len(canonical_json_bytes(to_json_ready(view)))


def _dedupe_key(candidate: CandidateUnit) -> tuple:
    locator = candidate.locator
    return (
        locator.record_id,
        locator.view_id,
        locator.node_id,
        locator.revision_ref,
        locator.text_hash,
    )


def assemble_packet(
    *,
    query: Mapping[str, Any],
    publication: Mapping[str, Any] | None,
    publication_status: str,
    candidates: Sequence[CandidateUnit],
    pre_omissions: Iterable[OmittedEvidence],
    budget_view: Mapping[str, int],
    context: Mapping[str, Any],
    include_text: bool,
    operations_executed: int,
    candidates_considered: int,
    authorization: Mapping[str, Any] | None = None,
) -> EvidencePacket:
    """Turn retrieval candidates into a structurally bounded packet.

    Budget rules (whole-unit only):

    - a unit already selected (same source locator and content hash)
      is deduplicated with an explicit ``duplicate`` omission;
    - a unit is included only if the unit count and cumulative
      serialized size stay within budget — otherwise it is omitted
      whole with ``output_budget`` / ``unit_budget``;
    - a single unit larger than the entire output budget is omitted
      with ``unit_too_large`` and never cut mid-unit;
    - any budget-driven omission marks the packet ``partial``.
    """
    included: list[EvidenceUnit] = []
    omitted: list[OmittedEvidence] = list(pre_omissions)
    seen: set[tuple] = set()
    output_chars = 0
    max_units = int(budget_view["max_evidence_units"])
    max_output_chars = int(budget_view["max_output_chars"])
    truncated = any(o.reason in _BUDGET_REASONS for o in omitted)

    for candidate in candidates:
        key = _dedupe_key(candidate)
        if key in seen:
            omitted.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="duplicate",
                    detail="unit already selected through an earlier operation",
                )
            )
            continue
        cost = _unit_cost(candidate, include_text)
        if len(included) >= max_units:
            omitted.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="unit_budget",
                    detail=(
                        f"evidence unit count reached max_evidence_units="
                        f"{max_units}"
                    ),
                )
            )
            truncated = True
            continue
        if cost > max_output_chars:
            omitted.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="unit_too_large",
                    detail=(
                        f"unit serialized size {cost} exceeds the whole "
                        f"max_output_chars={max_output_chars} budget; "
                        "refusing to cut the unit mid-structure"
                    ),
                )
            )
            truncated = True
            continue
        if output_chars + cost > max_output_chars:
            omitted.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="output_budget",
                    detail=(
                        f"including this unit would exceed max_output_chars="
                        f"{max_output_chars} (already {output_chars}, unit "
                        f"{cost}); unit withheld whole"
                    ),
                )
            )
            truncated = True
            continue
        seen.add(key)
        output_chars += cost
        included.append(
            EvidenceUnit(
                operation_index=candidate.operation_index,
                op=candidate.op,
                locator=candidate.locator,
                text=candidate.text if include_text else None,
                rank=candidate.rank,
                chars=cost,
            )
        )

    report = BudgetReport(
        max_operations=int(budget_view["max_operations"]),
        max_candidates=int(budget_view["max_candidates"]),
        max_evidence_units=max_units,
        max_output_chars=max_output_chars,
        operations_executed=operations_executed,
        candidates_considered=candidates_considered,
        units_included=len(included),
        units_omitted=len(omitted),
        output_chars=output_chars,
        truncated=truncated,
    )
    identity_dimensions = packet_identity_dimensions(
        query=query,
        publication=publication,
        evidence=included,
        omitted=omitted,
        budget_view=budget_view,
        context=context,
        authorization=authorization,
    )
    identity_id = record_identity_hash(
        record_type=_PACKET_RECORD_TYPE,
        schema_version=_PACKET_ID_SCHEMA_VERSION,
        payload=identity_dimensions,
    )
    return EvidencePacket(
        schema_version=EVIDENCE_PACKET_SCHEMA_VERSION,
        status="partial" if truncated else "complete",
        publication_status=publication_status,
        query=dict(query),
        publication=dict(publication) if publication is not None else None,
        evidence=tuple(included),
        omitted=tuple(omitted),
        budget=report,
        context=dict(context),
        authorization=dict(authorization) if authorization is not None else None,
        identity_dimensions=identity_dimensions,
        identity_id=identity_id,
    )


_PUBLICATION_IDENTITY_KEYS = (
    "publication_set_id",
    "workspace_id",
    "profile",
    "kernel_commit_id",
    "snapshot_id",
    "materialized_generation_id",
    "lexical_generation_id",
    "tokenizer",
    "vector_generation_id",
    "content_digest",
)


def packet_identity_dimensions(
    *,
    query: Mapping[str, Any],
    publication: Mapping[str, Any] | None,
    evidence: Sequence[EvidenceUnit],
    omitted: Sequence[OmittedEvidence],
    budget_view: Mapping[str, int],
    context: Mapping[str, Any],
    authorization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The deterministic semantic dimensions of a packet.

    Reuse is safe only while *all* of these match: normalized query,
    publication/member generation identity (including the set's content
    digest), selected evidence locators + content hashes and their
    order, explicit omissions, the budget profile that shaped the
    selection, the caller-supplied security / verifier / redaction
    / serialization identities, and the trusted effective-authorization
    identity (profile, assurance, epoch, deny revision, policy digest).
    Any relevant change must change the identity — reuse across a
    changed dimension is a bug. Authorization changes therefore
    invalidate stale cached packets even when content and request are
    byte-identical.
    """
    publication_view = None
    if publication is not None:
        publication_view = {key: publication.get(key) for key in _PUBLICATION_IDENTITY_KEYS}
    return {
        "packet_schema_version": EVIDENCE_PACKET_SCHEMA_VERSION,
        "query": dict(query),
        "publication": publication_view,
        "evidence": [unit.locator.identity_view() for unit in evidence],
        "omitted": [
            {
                "operation_index": o.operation_index,
                "op": o.op,
                "reason": o.reason,
            }
            for o in omitted
        ],
        "budget": {
            "max_operations": int(budget_view["max_operations"]),
            "max_candidates": int(budget_view["max_candidates"]),
            "max_evidence_units": int(budget_view["max_evidence_units"]),
            "max_output_chars": int(budget_view["max_output_chars"]),
        },
        "context": dict(context),
        "authorization": dict(authorization) if authorization is not None else None,
    }


def to_json(packet: EvidencePacket) -> dict[str, Any]:
    """Serializable view of a packet (deterministic; no runtime noise)."""
    return {
        "schema_version": packet.schema_version,
        "status": packet.status,
        "publication_status": packet.publication_status,
        "query": packet.query,
        "publication": packet.publication,
        "evidence": [unit.view() for unit in packet.evidence],
        "omitted": [o.view() for o in packet.omitted],
        "budget": packet.budget.view(),
        "context": packet.context,
        "authorization": packet.authorization,
        "identity_id": packet.identity_id,
    }
