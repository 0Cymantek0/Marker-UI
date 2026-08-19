"""Claim proof authority & integrity (V3.2 PR74).

This module gives verification support a first-class, checkable shape on
top of the existing record/edge substrate:

* :class:`ProofSupportRecord` — the authority-bearing support relation.
  Unlike generic lineage/navigation edges (``derived_from``,
  ``depends_on``, ``assesses``, ``observes``, ``evidence_for``), a proof
  support record declares **which rule** allows it to raise authority
  (``authority_rule``) and **how** the evidence contributes (``role``:
  independent ``witness``, explicitly ``derived`` material, or a
  structural ``input`` such as a crop/normalization/topology/source-
  revision dependency).
* :func:`check_batch_proof_integrity` — the commit-boundary validator.
  It runs inside the kernel commit transaction (see
  :mod:`app.kernel.commit`) before any row is inserted, so an invalid
  proof never becomes visible: no records, no edges, no manifest, no
  head movement.
* :func:`evaluate_claim_requirements` — the authoritative evaluation
  behind PR73's claim-dependent patch preconditions.

The proof-reliance graph
------------------------

Reliance flows from a consumer to what it depends on:

* every proof support contributes ``holder_ref -> evidence_ref``;
* every ``derived_from`` edge contributes ``source -> target`` —
  derivation is the channel through which self-support launders, so a
  derived record implicitly relies on its ancestor.

Rules enforced at the authoritative boundary:

1. **Acyclicity** — the reliance graph restricted to paths through at
   least one new (batch) relation must be acyclic. Committed history is
   acyclic by induction (it passed this check when it landed), so any
   new cycle passes through a new relation and is rejected with the
   offending path.
2. **Grounding** — the reliance closure of an authority-bearing
   assessment (or an authority-rule-bearing decision) may never reach a
   claim/assessment/decision record. Claims are the things being
   assessed; support that derives from a claim — directly or through a
   summary/reconciliation chain — is authority laundering, not
   independent support.
3. **Input integrity** — the assessment's declared ``evidence_refs``
   must agree exactly with its support graph; a ``witness`` must carry
   no derivation (independence is structural, not asserted); ``derived``
   evidence must expose its derivation path; no evidence may be an
   authority-consumer record; one support per (holder, evidence) pair.

Deliberately NOT enforced here (documented residual limits): the
empirical/statistical sufficiency of a proof (PR75), domain validators
(table totals, formulas, units), and derivation links a submitter simply
fails to declare — the contract makes hidden inputs *unrepresentable as
authority*, it cannot make authors honest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Sequence

from app.kernel.errors import (
    ClaimPreconditionUnmetError,
    InvalidClaimAssessmentError,
    KernelError,
    ProofCycleError,
    ProofInputIntegrityError,
)
from app.kernel.records import (
    AUTHORITY_BEARING_OUTCOMES,
    EDGE_KIND_DERIVED_FROM,
    KernelEdge,
    KernelRecord,
    validate_record_ref,
)

__all__ = [
    "AUTHORITY_CONSUMER_CLASSES",
    "ProofBatchRecord",
    "ProofSupportRecord",
    "PROOF_ROLE_DERIVED",
    "PROOF_ROLE_INPUT",
    "PROOF_ROLE_WITNESS",
    "PROOF_ROLES",
    "check_batch_proof_integrity",
    "detect_proof_cycle",
    "evaluate_claim_requirements",
    "proof_closure_path_to_authority_consumer",
]

#: Record classes that consume authority rather than provide it. They
#: can never act as evidence, and no authority-bearing proof closure may
#: reach one: reaching the assessed claim is self-support; reaching
#: another claim/assessment/decision is laundering through someone
#: else's unresolved authority.
AUTHORITY_CONSUMER_CLASSES = frozenset(
    {"claim_assertion", "claim_assessment", "decision"}
)

#: Proof-support roles (how one piece of evidence contributes).
PROOF_ROLE_WITNESS = "witness"
PROOF_ROLE_DERIVED = "derived"
PROOF_ROLE_INPUT = "input"
PROOF_ROLES = frozenset({PROOF_ROLE_WITNESS, PROOF_ROLE_DERIVED, PROOF_ROLE_INPUT})

_RECORD_CLASS_PROOF_SUPPORT = "proof_support"


@dataclass(kw_only=True)
class ProofSupportRecord(KernelRecord):
    """One authority-bearing support relation (a proof edge as a record).

    ``holder_ref`` is the assessment (or decision) being supported;
    ``evidence_ref`` is the record providing support; ``role`` declares
    how the evidence contributes (see :data:`PROOF_ROLES`);
    ``authority_rule`` names the rule/policy basis that allows this
    relation to raise authority — an authority-raising relation without
    a declared rule is unrepresentable.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_PROOF_SUPPORT
    record_type: ClassVar[str] = "marker.kernel.proof_support.v1"
    schema_version: ClassVar[str] = "1.0.0"

    holder_ref: str
    evidence_ref: str
    role: str
    authority_rule: str

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.holder_ref, field_name="holder_ref")
        validate_record_ref(self.evidence_ref, field_name="evidence_ref")
        if self.holder_ref == self.evidence_ref:
            raise KernelError("a proof support relation cannot target its own holder")
        if self.role not in PROOF_ROLES:
            raise KernelError(
                f"invalid proof role: {self.role!r}; allowed: {sorted(PROOF_ROLES)}"
            )
        if not isinstance(self.authority_rule, str) or not self.authority_rule:
            raise KernelError(
                f"invalid authority_rule: {self.authority_rule!r}; an "
                "authority-raising relation must declare its rule basis"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "holder_ref": self.holder_ref,
            "evidence_ref": self.evidence_ref,
            "role": self.role,
            "authority_rule": self.authority_rule,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> ProofSupportRecord:
        """Rematerialize a stored proof-support payload (fail-closed)."""
        if not isinstance(payload, Mapping):
            raise KernelError(f"proof support payload must be a mapping, got {payload!r}")
        allowed = {"holder_ref", "evidence_ref", "role", "authority_rule"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown proof support payload fields {sorted(unknown)}")
        try:
            return cls(
                record_id=record_id,
                holder_ref=payload["holder_ref"],
                evidence_ref=payload["evidence_ref"],
                role=payload["role"],
                authority_rule=payload["authority_rule"],
            )
        except KeyError as exc:
            raise KernelError(
                f"proof support payload is missing {exc.args[0]!r}"
            ) from None


@dataclass(frozen=True)
class ProofBatchRecord:
    """Batch-side view of one prepared record (class + stored payload).

    Decouples the validator from the commit service's internal types.
    """

    record_id: str
    record_class: str
    payload_json: str


@dataclass(frozen=True)
class _SupportView:
    """Parsed proof-support relation (batch or committed)."""

    holder_ref: str
    evidence_ref: str
    role: str
    authority_rule: str


@dataclass(frozen=True)
class _AssessmentView:
    """Parsed claim-assessment payload (stored identity shape)."""

    assertion_ref: str
    outcome: str
    policy_id: str
    policy_revision: str
    evidence_refs: tuple[str, ...]
    snapshot_commit_id: int


@dataclass(frozen=True)
class _DecisionView:
    """Parsed decision payload (stored identity shape)."""

    authority_rule: str


def _parse_support(payload_json: str) -> _SupportView:
    payload = json.loads(payload_json)
    return _SupportView(
        holder_ref=payload["holder_ref"],
        evidence_ref=payload["evidence_ref"],
        role=payload["role"],
        authority_rule=payload["authority_rule"],
    )


def _parse_assessment(payload_json: str) -> _AssessmentView:
    payload = json.loads(payload_json)
    policy = payload.get("policy") or {}
    return _AssessmentView(
        assertion_ref=payload["assertion_ref"],
        outcome=payload["outcome"],
        policy_id=policy.get("policy_id", ""),
        policy_revision=policy.get("revision", ""),
        evidence_refs=tuple(payload.get("evidence_refs") or ()),
        snapshot_commit_id=payload.get("snapshot_commit_id", 0),
    )


def _parse_decision(payload_json: str) -> _DecisionView:
    payload = json.loads(payload_json)
    return _DecisionView(authority_rule=payload.get("authority_rule") or "")


@dataclass
class _RelianceGraph:
    """Adjacency over record ids: consumer -> dependencies.

    Edges come from proof supports and ``derived_from`` lineage, batch
    and committed state overlaid. ``new_sources`` are the tails of the
    relations this batch introduces — every cycle rejected here must
    pass through at least one of them, because committed history was
    already acyclic when it was accepted.
    """

    adjacency: dict[str, list[str]] = field(default_factory=dict)
    new_sources: list[str] = field(default_factory=list)

    def add(self, source: str, target: str, *, new: bool) -> None:
        self.adjacency.setdefault(source, []).append(target)
        if new:
            self.new_sources.append(source)

    def outgoing(self, node: str) -> Sequence[str]:
        return self.adjacency.get(node, ())


async def _load_committed_reliance(session, workspace_id: str) -> tuple[_RelianceGraph, dict[str, _SupportView]]:
    """Load committed proof supports and derivation lineage.

    Returns the reliance graph of already-committed state plus the
    committed support relations keyed by their record id (for
    precondition revalidation).
    """
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow
    from app.kernel.models import KernelRecordEdge

    graph = _RelianceGraph()
    lineage_rows = (
        await session.execute(
            select(
                KernelRecordEdge.source_record_id, KernelRecordEdge.target_record_id
            ).where(
                KernelRecordEdge.workspace_id == workspace_id,
                KernelRecordEdge.edge_kind == EDGE_KIND_DERIVED_FROM,
            )
        )
    ).all()
    for source, target in lineage_rows:
        graph.add(source, target, new=False)

    supports: dict[str, _SupportView] = {}
    support_rows = (
        await session.execute(
            select(KernelRecordRow.id, KernelRecordRow.payload_json).where(
                KernelRecordRow.workspace_id == workspace_id,
                KernelRecordRow.record_class == _RECORD_CLASS_PROOF_SUPPORT,
            )
        )
    ).all()
    for record_id, payload_json in support_rows:
        view = _parse_support(payload_json)
        supports[record_id] = view
        graph.add(view.holder_ref, view.evidence_ref, new=False)
    return graph, supports


async def _resolve_record_classes(
    session,
    workspace_id: str,
    refs: set[str],
    batch_classes: Mapping[str, str],
) -> dict[str, str]:
    """Class map for ``refs`` — batch classes win, committed rows fill in.

    Raises the same typed existence/workspace errors the edge validator
    uses, so record-reference integrity fails identically at the
    boundary.
    """
    from app.kernel.errors import (
        CrossWorkspaceReferenceError,
        UnknownRecordReferenceError,
    )
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow

    unknown = sorted(ref for ref in refs if ref not in batch_classes)
    classes = {ref: cls for ref, cls in batch_classes.items() if ref in refs}
    if unknown:
        rows = (
            await session.execute(
                select(
                    KernelRecordRow.id, KernelRecordRow.workspace_id, KernelRecordRow.record_class
                ).where(KernelRecordRow.id.in_(unknown))
            )
        ).all()
        found = {row.id: row for row in rows}
        missing = sorted(ref for ref in unknown if ref not in found)
        if missing:
            raise UnknownRecordReferenceError(
                f"workspace={workspace_id!r}: claim/proof references records not "
                f"visible to this commit: {missing}"
            )
        foreign = sorted(ref for ref, row in found.items() if row.workspace_id != workspace_id)
        if foreign:
            raise CrossWorkspaceReferenceError(
                f"workspace={workspace_id!r}: claim/proof references records of "
                f"other workspaces: {foreign}"
            )
        classes.update({row.id: row.record_class for row in rows})
    return classes


def _check_cycles(graph: _RelianceGraph) -> None:
    """Reject any reliance cycle passing through a batch-introduced edge.

    Iterative colored DFS from each new-edge tail; committed subgraphs
    are acyclic by induction, so marking fully explored nodes black
    across sources is sound. The reported path is the actual loop.
    """
    color: dict[str, int] = {}  # 0 = unvisited/white, 1 = on-stack/gray, 2 = done/black
    for start in graph.new_sources:
        if color.get(start) == 2:
            continue
        path: list[str] = [start]
        color[start] = 1
        iterators: list = [iter(graph.outgoing(start))]
        while iterators:
            advanced = False
            for nxt in iterators[-1]:
                state = color.get(nxt, 0)
                if state == 1:
                    cycle = path[path.index(nxt):] + [nxt]
                    raise ProofCycleError(cycle_path=cycle)
                if state == 0:
                    path.append(nxt)
                    color[nxt] = 1
                    iterators.append(iter(graph.outgoing(nxt)))
                    advanced = True
                    break
                # black: fully explored, no cycle through it
            if not advanced:
                color[path.pop()] = 2
                iterators.pop()


async def _check_grounding(
    session,
    workspace_id: str,
    graph: _RelianceGraph,
    roots: Sequence[tuple[str, str]],
    batch_classes: Mapping[str, str],
) -> None:
    """No authority-bearing closure may reach an authority consumer.

    ``roots`` pairs each authority-bearing holder with a human label for
    error messages. The closure walks reliance edges (proof + derivation)
    over batch and committed state; every reached node's class must stay
    outside :data:`AUTHORITY_CONSUMER_CLASSES`.
    """
    if not roots:
        return
    reachable: dict[str, str | None] = {}  # node -> parent (path reconstruction)
    frontier: list[str] = []
    for holder, _label in roots:
        if holder not in reachable:
            reachable[holder] = None
            frontier.append(holder)
    while frontier:
        node = frontier.pop()
        for nxt in graph.outgoing(node):
            if nxt not in reachable:
                reachable[nxt] = node
                frontier.append(nxt)

    committed_ids = [node for node in reachable if node not in batch_classes]
    classes = dict(batch_classes)
    if committed_ids:
        from sqlalchemy import select

        from app.kernel.models import KernelRecord as KernelRecordRow

        rows = (
            await session.execute(
                select(KernelRecordRow.id, KernelRecordRow.record_class).where(
                    KernelRecordRow.id.in_(committed_ids),
                    KernelRecordRow.workspace_id == workspace_id,
                )
            )
        ).all()
        classes.update({row.id: row.record_class for row in rows})

    for node in reachable:
        if reachable[node] is None:
            # The authority-bearing holders themselves — the consumers
            # being validated, not consumers their proof reached. A
            # root id that is genuinely reached through reliance edges
            # still has a parent below and is checked.
            continue
        if classes.get(node) in AUTHORITY_CONSUMER_CLASSES:
            path = [node]
            parent = reachable[node]
            while parent is not None:
                path.append(parent)
                parent = reachable[parent]
            path.reverse()
            for holder, label in roots:
                if holder in path:
                    raise ProofInputIntegrityError(
                        f"{label} relies on {node!r} ({classes.get(node)}) — the "
                        "proof closure of an authority-bearing result may never "
                        f"reach a claim/assessment/decision record; path: "
                        f"{' -> '.join(path)}"
                    )
            raise ProofInputIntegrityError(
                f"proof closure reaches authority consumer {node!r}; path: "
                f"{' -> '.join(path)}"
            )


async def check_batch_proof_integrity(
    session,
    *,
    workspace_id: str,
    batch_records: Mapping[str, ProofBatchRecord],
    edges: Sequence[KernelEdge],
    current_head: int,
) -> None:
    """Validate claim/proof semantics for one commit batch (authoritative).

    Runs inside the commit transaction before any insert: a violation
    raises a typed error and the whole batch rolls back — records,
    edges, manifest, outbox, view-head movement, and kernel head
    advancement together.
    """
    batch_classes = {rid: rec.record_class for rid, rec in batch_records.items()}

    assessments: dict[str, _AssessmentView] = {}
    decisions: dict[str, _DecisionView] = {}
    batch_supports: dict[str, _SupportView] = {}
    for record_id, rec in batch_records.items():
        if rec.record_class == "claim_assessment":
            assessments[record_id] = _parse_assessment(rec.payload_json)
        elif rec.record_class == "decision":
            decisions[record_id] = _parse_decision(rec.payload_json)
        elif rec.record_class == _RECORD_CLASS_PROOF_SUPPORT:
            view = _parse_support(rec.payload_json)
            if view.role not in PROOF_ROLES:
                raise ProofInputIntegrityError(
                    f"proof support {record_id!r} carries unknown role {view.role!r}"
                )
            if not view.authority_rule:
                raise ProofInputIntegrityError(
                    f"proof support {record_id!r} has no authority rule basis"
                )
            batch_supports[record_id] = view

    # -- reference integrity: assertion/evidence/holder refs resolve ---
    referenced: set[str] = set()
    for view in assessments.values():
        referenced.add(view.assertion_ref)
        referenced.update(view.evidence_refs)
    for view in batch_supports.values():
        referenced.update((view.holder_ref, view.evidence_ref))
    classes = await _resolve_record_classes(
        session, workspace_id, referenced, batch_classes
    )

    # -- snapshot honesty: evidence committed in PRIOR commits must have
    #    been visible at the declared snapshot cut (in-batch evidence
    #    becomes visible atomically with the assessment and is exempt) --
    committed_evidence = {
        ref
        for view in assessments.values()
        for ref in view.evidence_refs
        if ref not in batch_classes
    }
    if committed_evidence:
        from sqlalchemy import select

        from app.kernel.models import KernelRecord as KernelRecordRow

        visibility_rows = (
            await session.execute(
                select(
                    KernelRecordRow.id, KernelRecordRow.kernel_commit_id
                ).where(
                    KernelRecordRow.id.in_(sorted(committed_evidence)),
                    KernelRecordRow.workspace_id == workspace_id,
                )
            )
        ).all()
        evidence_commit = {row.id: row.kernel_commit_id for row in visibility_rows}
        for record_id, view in assessments.items():
            for ref in view.evidence_refs:
                landed = evidence_commit.get(ref)
                if landed is not None and landed > view.snapshot_commit_id:
                    raise InvalidClaimAssessmentError(
                        f"assessment {record_id!r} declares snapshot commit "
                        f"{view.snapshot_commit_id} but its evidence {ref!r} only "
                        f"became visible at commit {landed}; an assessment cannot "
                        "rely on state its declared cut does not contain"
                    )

    # -- reliance graph: committed state overlaid with this batch -------
    graph, _committed_supports = await _load_committed_reliance(session, workspace_id)
    for edge in edges:
        if edge.edge_kind == EDGE_KIND_DERIVED_FROM:
            graph.add(edge.source_ref, edge.target_ref, new=True)
    for view in batch_supports.values():
        graph.add(view.holder_ref, view.evidence_ref, new=True)

    # -- support-structure rules ----------------------------------------
    support_pairs: set[tuple[str, str]] = set()
    supports_by_holder: dict[str, list[_SupportView]] = {}
    for record_id, view in batch_supports.items():
        holder_class = classes.get(view.holder_ref)
        if holder_class not in ("claim_assessment", "decision"):
            raise ProofInputIntegrityError(
                f"proof support {record_id!r} holder {view.holder_ref!r} is a "
                f"{holder_class!r}; only claim assessments and decisions can "
                "hold proof support"
            )
        evidence_class = classes.get(view.evidence_ref)
        if evidence_class in AUTHORITY_CONSUMER_CLASSES:
            raise ProofInputIntegrityError(
                f"proof support {record_id!r} names evidence {view.evidence_ref!r} "
                f"({evidence_class}); authority consumers can never act as evidence"
            )
        pair = (view.holder_ref, view.evidence_ref)
        if pair in support_pairs:
            raise ProofInputIntegrityError(
                f"duplicate proof support for ({view.holder_ref!r}, "
                f"{view.evidence_ref!r}); one relation per holder/evidence pair"
            )
        support_pairs.add(pair)
        supports_by_holder.setdefault(view.holder_ref, []).append(view)
        if view.role == PROOF_ROLE_WITNESS:
            if graph.outgoing(view.evidence_ref):
                raise ProofInputIntegrityError(
                    f"witness {view.evidence_ref!r} carries derivation lineage; "
                    "derived material must be presented with role=derived, never "
                    "as an independent witness"
                )
        elif view.role == PROOF_ROLE_DERIVED:
            if not graph.outgoing(view.evidence_ref):
                raise ProofInputIntegrityError(
                    f"derived evidence {view.evidence_ref!r} exposes no "
                    "derivation path; hidden proof inputs cannot raise authority"
                )

    # -- assessment contract ----------------------------------------------
    grounding_roots: list[tuple[str, str]] = []
    for record_id, view in assessments.items():
        if view.snapshot_commit_id > current_head:
            raise InvalidClaimAssessmentError(
                f"assessment {record_id!r} declares snapshot commit "
                f"{view.snapshot_commit_id} but the current head is "
                f"{current_head}; an assessment can never claim a future cut"
            )
        supports = supports_by_holder.get(record_id, [])
        if view.outcome in AUTHORITY_BEARING_OUTCOMES:
            if not supports:
                raise InvalidClaimAssessmentError(
                    f"assessment {record_id!r} carries authority-bearing outcome "
                    f"{view.outcome!r} without any proof support; authority "
                    "requires a structurally valid proof"
                )
            declared = set(view.evidence_refs)
            supported = {s.evidence_ref for s in supports}
            if declared != supported:
                raise ProofInputIntegrityError(
                    f"assessment {record_id!r} declares evidence {sorted(declared)} "
                    f"but its support graph covers {sorted(supported)}; the "
                    "declared evidence set must agree exactly with the proof"
                )
            grounding_roots.append(
                (record_id, f"authority-bearing assessment {record_id!r}")
            )
        elif supports and set(view.evidence_refs) != {s.evidence_ref for s in supports}:
            # Non-authority outcomes may commit without proof, but a proof
            # they do carry must still agree with the declared evidence.
            raise ProofInputIntegrityError(
                f"assessment {record_id!r} declares evidence "
                f"{sorted(set(view.evidence_refs))} but its support graph covers "
                f"{sorted({s.evidence_ref for s in supports})}; declared evidence "
                "must agree exactly with the proof"
            )

    for record_id, view in decisions.items():
        if view.authority_rule and record_id not in supports_by_holder:
            raise InvalidClaimAssessmentError(
                f"decision {record_id!r} declares authority rule "
                f"{view.authority_rule!r} without proof support; an authority-"
                "raising decision must carry its proof in the same commit"
            )
        if view.authority_rule:
            grounding_roots.append(
                (record_id, f"authority-rule decision {record_id!r}")
            )

    # -- topology: cycles first (cheapest global invariant), then grounding
    _check_cycles(graph)
    await _check_grounding(
        session, workspace_id, graph, grounding_roots, batch_classes
    )


# ---------------------------------------------------------------------------
# Pure topology probes (conformance/determinism surface, no database)
# ---------------------------------------------------------------------------


def detect_proof_cycle(
    supports: Sequence[tuple[str, str]],
    derived_edges: Sequence[tuple[str, str]],
) -> list[str] | None:
    """Cycle probe over declared proof-support and derivation relations.

    Returns the offending reliance path (consumer -> dependency order)
    or ``None`` when the relations are acyclic. Same algorithm the
    commit boundary runs; exposed pure for conformance vectors.
    """
    graph = _RelianceGraph()
    for holder, evidence in supports:
        graph.add(holder, evidence, new=True)
    for source, target in derived_edges:
        graph.add(source, target, new=True)
    try:
        _check_cycles(graph)
    except ProofCycleError as exc:
        return list(exc.cycle_path)
    return None


def proof_closure_path_to_authority_consumer(
    start: str,
    supports: Sequence[tuple[str, str]],
    derived_edges: Sequence[tuple[str, str]],
    classes: Mapping[str, str],
) -> list[str] | None:
    """Grounding probe: reliance closure of ``start`` over the declared
    relations, returning the path to the first authority-consumer node
    reached (or ``None`` when the closure stays grounded)."""
    adjacency: dict[str, list[str]] = {}
    for holder, evidence in supports:
        adjacency.setdefault(holder, []).append(evidence)
    for source, target in derived_edges:
        adjacency.setdefault(source, []).append(target)
    parent: dict[str, str | None] = {start: None}
    frontier = [start]
    while frontier:
        node = frontier.pop()
        for nxt in adjacency.get(node, ()):
            if nxt not in parent:
                parent[nxt] = node
                frontier.append(nxt)
    for node in parent:
        if node == start:
            continue
        if classes.get(node) in AUTHORITY_CONSUMER_CLASSES:
            path = [node]
            cursor = parent[node]
            while cursor is not None:
                path.append(cursor)
                cursor = parent[cursor]
            path.reverse()
            return path
    return None


# ---------------------------------------------------------------------------
# PR73 seam: claim/assessment patch preconditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimRequirement:
    """One claim/assessment precondition a patch requires to hold.

    ``accepted_outcomes`` defaults to the authority-bearing set: a patch
    gates a mutation on proof-backed state, not on any historical
    assessment. ``min_snapshot_commit_id`` declares a freshness floor —
    an assessment computed against an older cut than the patch accepts
    is stale and fails closed.
    """

    assertion_ref: str
    policy_id: str
    policy_revision: str
    accepted_outcomes: tuple[str, ...] = ()
    assessment_ref: str | None = None
    min_snapshot_commit_id: int = 0

    def __post_init__(self) -> None:
        validate_record_ref(self.assertion_ref, field_name="assertion_ref")
        for name in ("policy_id", "policy_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise KernelError(f"invalid {name}: {value!r}")
        outcomes = tuple(self.accepted_outcomes) or tuple(AUTHORITY_BEARING_OUTCOMES)
        object.__setattr__(self, "accepted_outcomes", outcomes)
        if self.assessment_ref is not None:
            validate_record_ref(self.assessment_ref, field_name="assessment_ref")
        if (
            not isinstance(self.min_snapshot_commit_id, int)
            or isinstance(self.min_snapshot_commit_id, bool)
            or self.min_snapshot_commit_id < 0
        ):
            raise KernelError(
                f"invalid min_snapshot_commit_id: {self.min_snapshot_commit_id!r}"
            )

    def canonical_value(self) -> dict[str, Any]:
        return {
            "assertion_ref": self.assertion_ref,
            "policy_id": self.policy_id,
            "policy_revision": self.policy_revision,
            "accepted_outcomes": sorted(self.accepted_outcomes),
            "assessment_ref": self.assessment_ref,
            "min_snapshot_commit_id": self.min_snapshot_commit_id,
        }

    @classmethod
    def from_canonical(cls, value: Mapping[str, Any]) -> ClaimRequirement:
        if not isinstance(value, Mapping):
            raise KernelError(f"claim requirement must be a mapping, got {value!r}")
        allowed = {
            "assertion_ref",
            "policy_id",
            "policy_revision",
            "accepted_outcomes",
            "assessment_ref",
            "min_snapshot_commit_id",
        }
        unknown = set(value) - allowed
        if unknown:
            raise KernelError(f"unknown claim requirement fields {sorted(unknown)}")
        try:
            return cls(
                assertion_ref=value["assertion_ref"],
                policy_id=value["policy_id"],
                policy_revision=value["policy_revision"],
                accepted_outcomes=tuple(value.get("accepted_outcomes") or ()),
                assessment_ref=value.get("assessment_ref"),
                min_snapshot_commit_id=value.get("min_snapshot_commit_id", 0),
            )
        except KeyError as exc:
            raise KernelError(
                f"claim requirement is missing {exc.args[0]!r}"
            ) from None


async def evaluate_claim_requirements(
    session,
    workspace_id: str,
    requirements: Sequence[ClaimRequirement],
    *,
    current_head: int,
) -> None:
    """Authoritatively evaluate claim preconditions against committed state.

    Runs inside the commit transaction (called from the view-advancement
    check): every requirement must be satisfied by committed, in-policy,
    fresh, structurally valid assessment state, or the typed conflict
    rolls the whole patch back. Resolution is deterministic — the pinned
    ``assessment_ref`` when given, else the latest committed assessment
    of that assertion under that exact policy (linear commit order, no
    wall-clock participation).
    """
    if not requirements:
        return
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow

    rows = (
        await session.execute(
            select(
                KernelRecordRow.id,
                KernelRecordRow.kernel_commit_id,
                KernelRecordRow.payload_json,
            ).where(
                KernelRecordRow.workspace_id == workspace_id,
                KernelRecordRow.record_class == "claim_assessment",
            )
        )
    ).all()
    committed: dict[str, tuple[int, _AssessmentView]] = {}
    for record_id, commit_id, payload_json in rows:
        committed[record_id] = (commit_id, _parse_assessment(payload_json))

    def _unmet(requirement: ClaimRequirement, reason: str) -> ClaimPreconditionUnmetError:
        return ClaimPreconditionUnmetError(
            f"assertion={requirement.assertion_ref!r} "
            f"policy={requirement.policy_id}/{requirement.policy_revision} "
            f"assessment={requirement.assessment_ref or '<latest>'}: {reason}"
        )

    validated_holders: list[str] = []
    for requirement in requirements:
        accepted = requirement.accepted_outcomes or tuple(AUTHORITY_BEARING_OUTCOMES)
        if requirement.assessment_ref is not None:
            entry = committed.get(requirement.assessment_ref)
            if entry is None:
                raise _unmet(requirement, "the pinned assessment is not committed")
            _commit_id, view = entry
        else:
            candidates = [
                (commit_id, record_id, view)
                for record_id, (commit_id, view) in committed.items()
                if view.assertion_ref == requirement.assertion_ref
                and view.policy_id == requirement.policy_id
                and view.policy_revision == requirement.policy_revision
            ]
            if not candidates:
                raise _unmet(
                    requirement,
                    "no committed assessment of this assertion under this policy",
                )
            candidates.sort(key=lambda item: (item[0], item[1]))
            _commit_id, record_id, view = candidates[-1]
            requirement = ClaimRequirement(
                assertion_ref=requirement.assertion_ref,
                policy_id=requirement.policy_id,
                policy_revision=requirement.policy_revision,
                accepted_outcomes=accepted,
                assessment_ref=record_id,
                min_snapshot_commit_id=requirement.min_snapshot_commit_id,
            )
        if view.assertion_ref != requirement.assertion_ref:
            raise _unmet(requirement, "the assessment targets a different assertion")
        if (
            view.policy_id != requirement.policy_id
            or view.policy_revision != requirement.policy_revision
        ):
            raise _unmet(requirement, "policy/policy-revision mismatch")
        if view.outcome not in accepted:
            raise _unmet(
                requirement,
                f"outcome {view.outcome!r} is not in the accepted set "
                f"{sorted(accepted)}",
            )
        if view.snapshot_commit_id < requirement.min_snapshot_commit_id:
            raise _unmet(
                requirement,
                f"assessment snapshot {view.snapshot_commit_id} predates the "
                f"required cut {requirement.min_snapshot_commit_id}",
            )
        if view.snapshot_commit_id > current_head:
            raise _unmet(
                requirement,
                f"assessment snapshot {view.snapshot_commit_id} names a cut "
                f"beyond the current head {current_head}",
            )
        validated_holders.append(requirement.assessment_ref or "")

    if not validated_holders:
        return

    # Structural revalidation: the assessment's proof must still hold at
    # the current cut — supports still committed, evidence agreement
    # intact, closure still grounded (a later commit may have attached a
    # derivation edge that launders the original proof).
    graph, committed_supports = await _load_committed_reliance(session, workspace_id)
    for holder in validated_holders:
        entry = committed.get(holder)
        if entry is None:
            continue
        _commit_id, view = entry
        if view.outcome not in AUTHORITY_BEARING_OUTCOMES:
            continue
        context = ClaimRequirement(
            assertion_ref=view.assertion_ref,
            policy_id=view.policy_id,
            policy_revision=view.policy_revision,
            assessment_ref=holder,
        )
        supports = [
            support for support in committed_supports.values()
            if support.holder_ref == holder
        ]
        if not supports:
            raise _unmet(context, "the assessment's proof supports are no longer committed")
        if set(view.evidence_refs) != {s.evidence_ref for s in supports}:
            raise _unmet(
                context, "declared evidence and support graph no longer agree"
            )
        try:
            await _check_grounding(
                session,
                workspace_id,
                graph,
                [(holder, f"assessment {holder!r}")],
                {holder: "claim_assessment"},
            )
        except ProofInputIntegrityError as exc:
            # A proof that was valid at its commit can be tainted by
            # later commits (e.g. a new derivation edge laundering it);
            # at precondition time that must surface as the typed patch
            # conflict, not as a batch-validation error.
            raise _unmet(context, str(exc)) from None
