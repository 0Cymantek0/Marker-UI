"""Conflict-aware patch contract & immutable view revisions (V3.2 PR73).

A derived document view is a first-class kernel artifact: one
:class:`ViewDocumentRecord` is one immutable *view revision* — the
reading-order graph plus the text value of every content node, bound to
the content revision it was derived from. Mutating that state goes
through patches, and a patch is a *conditional proposal*, not a command:

* :class:`PatchProposalRecord` names the base view revision it targets,
  the target nodes it intends to change with the canonical before-value
  hash of each, the source revisions it requires, and a small ordered
  list of domain operations;
* acceptance is all-or-conflict: every precondition is evaluated against
  current authoritative state inside the kernel commit transaction (see
  :mod:`app.kernel.commit`), and a false precondition rolls the whole
  batch back — a stale or conflicting patch never partially applies;
* an accepted patch produces a NEW immutable :class:`ViewDocumentRecord`
  (never rewrites the base) plus a :class:`PatchOutcomeRecord` recording
  the decision lineage.

Identity rules follow the kernel contract: semantic identity comes from
``identity_payload()`` through the PR61 canonical utilities; ``record_id``
is an event id and never identity; identity-affecting payloads reject
unknown fields on rematerialization (fail closed). Claim-assessment
preconditions are deliberately deferred to PR74 — the field exists, is
versioned with the schema, and non-empty values are rejected at
construction rather than silently ignored.

Operations stay domain-specific payloads behind this small envelope;
there is deliberately no universal patch language. v1 ships three
operations: ``replace_text`` (the declared reversible tracer),
``split_node`` (mirrors the PR72 bounded specialist split), and
``rebase_source`` (rebuild the view against a new source revision by
replaying accepted patches under their preconditions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from app.kernel.errors import (
    BeforeHashMismatchError,
    InvalidViewAdvancementError,
    KernelError,
    MissingViewTargetError,
    SourceRevisionMismatchError,
)
from app.kernel.reading_order import (
    NODE_KIND_CONTENT,
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    split_node,
)
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import payload_byte_hash, record_identity_hash

PATCH_SCHEMA_VERSION = "1.0.0"

RECORD_TYPE_VIEW_DOCUMENT = "marker.kernel.view_document.v1"
RECORD_TYPE_PATCH_PROPOSAL = "marker.kernel.patch_proposal.v1"
RECORD_TYPE_PATCH_OUTCOME = "marker.kernel.patch_outcome.v1"

#: The single view id this slice manages per workspace. The head table
#: is keyed by (workspace, view) so named views can arrive later without
#: a migration; v1 services pin this id.
DEFAULT_VIEW_ID = "document"

VIEW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

OP_TYPE_REPLACE_TEXT = "replace_text"
OP_TYPE_SPLIT_NODE = "split_node"
OP_TYPE_REBASE_SOURCE = "rebase_source"
OP_TYPES = frozenset({OP_TYPE_REPLACE_TEXT, OP_TYPE_SPLIT_NODE, OP_TYPE_REBASE_SOURCE})

PATCH_OUTCOME_ACCEPTED = "accepted"
PATCH_OUTCOMES = frozenset({PATCH_OUTCOME_ACCEPTED})


def view_text_hash(text: str) -> str:
    """Canonical before/after value hash for one view node text.

    Deterministic over the exact Unicode text (no normalization); the
    same discipline as every other kernel identity input.
    """
    if not isinstance(text, str):
        raise KernelError(f"view text must be str, got {type(text).__name__}")
    return payload_byte_hash(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchOperation:
    """One domain-specific patch operation behind a versioned envelope.

    ``params`` are validated and normalized per ``op_type`` at
    construction; ``canonical_value()`` is the normalized, deterministic
    form that enters proposal identity (children sorted by node id,
    resolved anchor refs).
    """

    op_type: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.op_type not in OP_TYPES:
            raise KernelError(
                f"unknown patch operation type {self.op_type!r}; "
                f"allowed: {sorted(OP_TYPES)}"
            )
        normalized = self._normalize(dict(self.params))
        object.__setattr__(self, "params", normalized)

    # -- replace_text ------------------------------------------------------

    @classmethod
    def replace_text(cls, *, node_id: str, after_text: str) -> PatchOperation:
        return cls(
            op_type=OP_TYPE_REPLACE_TEXT,
            params={"node_id": node_id, "after_text": after_text},
        )

    # -- split_node ----------------------------------------------------------

    @classmethod
    def split_node(
        cls,
        *,
        node_id: str,
        children: Any,
        child_order: Any = None,
        producer: str = "patch",
    ) -> PatchOperation:
        return cls(
            op_type=OP_TYPE_SPLIT_NODE,
            params={
                "node_id": node_id,
                "children": children,
                "child_order": child_order,
                "producer": producer,
            },
        )

    # -- rebase_source -------------------------------------------------------

    @classmethod
    def rebase_source(
        cls,
        *,
        new_content_revision_ref: str,
        source_graph: ReadingOrderGraph,
        source_texts: Mapping[str, str],
        replay_proposal_refs: Any = (),
    ) -> PatchOperation:
        return cls(
            op_type=OP_TYPE_REBASE_SOURCE,
            params={
                "new_content_revision_ref": new_content_revision_ref,
                "source_graph": source_graph,
                "source_texts": dict(source_texts),
                "replay_proposal_refs": replay_proposal_refs,
            },
        )

    # -- validation / normalization -------------------------------------------

    def _normalize(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.op_type == OP_TYPE_REPLACE_TEXT:
            unknown = set(params) - {"node_id", "after_text"}
            if unknown:
                raise KernelError(f"unknown replace_text fields {sorted(unknown)}")
            try:
                node_id, after_text = params["node_id"], params["after_text"]
            except KeyError as exc:
                raise KernelError(f"replace_text is missing {exc.args[0]!r}") from None
            validate_record_ref(node_id, field_name="node_id")
            if not isinstance(after_text, str):
                raise KernelError(
                    f"replace_text after_text must be str, got {type(after_text).__name__}"
                )
            return {"node_id": node_id, "after_text": after_text}

        if self.op_type == OP_TYPE_SPLIT_NODE:
            unknown = set(params) - {"node_id", "children", "child_order", "producer"}
            if unknown:
                raise KernelError(f"unknown split_node fields {sorted(unknown)}")
            try:
                node_id = params["node_id"]
                children = params["children"]
            except KeyError as exc:
                raise KernelError(f"split_node is missing {exc.args[0]!r}") from None
            validate_record_ref(node_id, field_name="node_id")
            child_list = list(children) if not isinstance(children, str) else None
            if not child_list:
                raise KernelError("split_node requires a non-empty children list")
            seen: set[str] = set()
            normalized_children: list[dict[str, Any]] = []
            for child in child_list:
                if not isinstance(child, Mapping):
                    raise KernelError(
                        f"split children must be mappings, got {type(child).__name__}"
                    )
                unknown_child = set(child) - {"node_id", "text", "anchor_ref"}
                if unknown_child:
                    raise KernelError(
                        f"unknown split child fields {sorted(unknown_child)}"
                    )
                child_id = child["node_id"]
                text = child["text"]
                validate_record_ref(child_id, field_name="child node_id")
                if child_id in seen:
                    raise KernelError(f"duplicate split child id {child_id!r}")
                seen.add(child_id)
                if not isinstance(text, str):
                    raise KernelError(
                        f"split child {child_id!r} text must be str, "
                        f"got {type(text).__name__}"
                    )
                anchor_ref = child.get("anchor_ref")
                if anchor_ref is not None:
                    validate_record_ref(anchor_ref, field_name="child anchor_ref")
                normalized_children.append(
                    {
                        "node_id": child_id,
                        "text": text,
                        "anchor_ref": anchor_ref,
                    }
                )
            # Children are an unordered replacement set: normalize by id so
            # member order never changes identity. Ordering evidence lives
            # only in child_order.
            normalized_children.sort(key=lambda c: c["node_id"])
            child_order = params.get("child_order")
            if child_order is not None:
                child_order = list(child_order)
                if sorted(child_order) != sorted(seen):
                    raise KernelError(
                        "child_order must be a permutation of the child ids"
                    )
            producer = params.get("producer", "patch")
            if not isinstance(producer, str) or not producer:
                raise KernelError(f"invalid split producer: {producer!r}")
            result: dict[str, Any] = {
                "node_id": node_id,
                "children": normalized_children,
                "producer": producer,
            }
            if child_order is not None:
                result["child_order"] = child_order
            return result

        # rebase_source
        unknown = set(params) - {
            "new_content_revision_ref",
            "source_graph",
            "source_texts",
            "replay_proposal_refs",
        }
        if unknown:
            raise KernelError(f"unknown rebase_source fields {sorted(unknown)}")
        try:
            new_ref = params["new_content_revision_ref"]
            source_graph = params["source_graph"]
            source_texts = params["source_texts"]
        except KeyError as exc:
            raise KernelError(f"rebase_source is missing {exc.args[0]!r}") from None
        validate_record_ref(new_ref, field_name="new_content_revision_ref")
        if not isinstance(source_graph, ReadingOrderGraph):
            raise KernelError(
                f"source_graph must be a ReadingOrderGraph, got {type(source_graph).__name__}"
            )
        if not isinstance(source_texts, Mapping):
            raise KernelError("source_texts must be a mapping")
        replay_refs = list(params.get("replay_proposal_refs") or ())
        for ref in replay_refs:
            validate_record_ref(ref, field_name="replay proposal ref")
        if len(set(replay_refs)) != len(replay_refs):
            raise KernelError("replay_proposal_refs contains duplicates")
        return {
            "new_content_revision_ref": new_ref,
            "source_graph": source_graph,
            "source_texts": dict(source_texts),
            "replay_proposal_refs": tuple(replay_refs),
        }

    def canonical_value(self) -> dict[str, Any]:
        params = dict(self.params)
        if self.op_type == OP_TYPE_REBASE_SOURCE:
            params["source_graph"] = self.params["source_graph"].canonical_payload()
            params["replay_proposal_refs"] = list(self.params["replay_proposal_refs"])
        elif self.op_type == OP_TYPE_SPLIT_NODE:
            params["children"] = [dict(child) for child in self.params["children"]]
            if "child_order" in self.params:
                params["child_order"] = list(self.params["child_order"])
        return {"op_type": self.op_type, "params": params}

    @classmethod
    def from_canonical(cls, value: Mapping[str, Any]) -> PatchOperation:
        if not isinstance(value, Mapping):
            raise KernelError(f"patch operation must be a mapping, got {value!r}")
        allowed = {"op_type", "params"}
        unknown = set(value) - allowed
        if unknown:
            raise KernelError(f"unknown patch operation fields {sorted(unknown)}")
        op_type = value.get("op_type")
        params = dict(value.get("params") or {})
        if op_type == OP_TYPE_REBASE_SOURCE:
            params["source_graph"] = ReadingOrderGraph.from_payload(
                params["source_graph"]
            )
            params["replay_proposal_refs"] = tuple(
                params.get("replay_proposal_refs") or ()
            )
        elif op_type == OP_TYPE_SPLIT_NODE:
            params["children"] = [
                dict(child) for child in params.get("children") or []
            ]
            if "child_order" in params and params["child_order"] is not None:
                params["child_order"] = list(params["child_order"])
        return cls(op_type=op_type, params=params)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetCheck:
    """One node-level before-value precondition.

    ``before_hash`` is :func:`view_text_hash` of the exact text the
    proposer believed the node held. The node id still existing is never
    sufficient for the patch to apply.
    """

    node_id: str
    before_hash: str

    def __post_init__(self) -> None:
        validate_record_ref(self.node_id, field_name="node_id")
        if not isinstance(self.before_hash, str) or not HASH_PATTERN.match(
            self.before_hash
        ):
            raise KernelError(
                f"invalid before_hash: {self.before_hash!r} must match "
                f"{HASH_PATTERN.pattern}"
            )

    def canonical_value(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "before_hash": self.before_hash}


@dataclass(frozen=True)
class PatchPreconditions:
    """The truthful precondition set enforceable in this slice.

    ``base_revision_id`` is the identity of the exact prior view revision
    the proposer read (RFC 9110 ``If-Match`` discipline: state-changing
    requests must carry the strong validator they observed).

    ``required_claim_refs`` is deliberately deferred to PR74: the field
    exists so the contract is extensible without a schema break, and
    non-empty values fail closed at construction rather than being
    silently ignored.
    """

    base_revision_id: str | None
    target_checks: tuple[TargetCheck, ...] = ()
    required_source_revision_refs: tuple[str, ...] = ()
    required_claim_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.base_revision_id is not None:
            if not isinstance(self.base_revision_id, str) or not HASH_PATTERN.match(
                self.base_revision_id
            ):
                raise KernelError(
                    f"invalid base_revision_id: {self.base_revision_id!r} must "
                    f"match {HASH_PATTERN.pattern}"
                )
        checks = tuple(self.target_checks)
        seen: set[str] = set()
        for check in checks:
            if not isinstance(check, TargetCheck):
                raise KernelError(
                    f"target_checks must be TargetCheck, got {type(check).__name__}"
                )
            if check.node_id in seen:
                raise KernelError(f"duplicate target check for {check.node_id!r}")
            seen.add(check.node_id)
        object.__setattr__(self, "target_checks", checks)
        source_refs = tuple(self.required_source_revision_refs)
        for ref in source_refs:
            validate_record_ref(ref, field_name="required source revision ref")
        object.__setattr__(self, "required_source_revision_refs", source_refs)
        claim_refs = tuple(self.required_claim_refs)
        if claim_refs:
            raise KernelError(
                "claim-assessment preconditions arrive with PR74; this slice "
                "fails closed instead of accepting a precondition it cannot "
                "evaluate"
            )

    def canonical_value(self) -> dict[str, Any]:
        return {
            "base_revision_id": self.base_revision_id,
            "target_checks": [
                check.canonical_value() for check in sorted(
                    self.target_checks, key=lambda c: c.node_id
                )
            ],
            "required_source_revision_refs": sorted(self.required_source_revision_refs),
            "required_claim_refs": [],
        }

    @classmethod
    def from_canonical(cls, value: Mapping[str, Any]) -> PatchPreconditions:
        if not isinstance(value, Mapping):
            raise KernelError(f"preconditions must be a mapping, got {value!r}")
        allowed = {
            "base_revision_id",
            "target_checks",
            "required_source_revision_refs",
            "required_claim_refs",
        }
        unknown = set(value) - allowed
        if unknown:
            raise KernelError(f"unknown precondition fields {sorted(unknown)}")
        claim_refs = value.get("required_claim_refs") or []
        if claim_refs:
            raise KernelError(
                "claim-assessment preconditions arrive with PR74; refusing "
                "to rematerialize a precondition this slice cannot evaluate"
            )
        return cls(
            base_revision_id=value.get("base_revision_id"),
            target_checks=tuple(
                TargetCheck(**check) for check in value.get("target_checks") or []
            ),
            required_source_revision_refs=tuple(
                value.get("required_source_revision_refs") or ()
            ),
        )


def evaluate_preconditions(
    current_view: ViewDocumentRecord, preconditions: PatchPreconditions
) -> None:
    """Evaluate every enforceable precondition against one current view.

    Pure: raises the typed conflict for the FIRST violated precondition
    (stale-base comparison itself belongs to the view head, not to a
    single document). This is the same evaluation the commit transaction
    runs authoritatively.
    """
    if preconditions.required_claim_refs:
        raise KernelError(
            "claim-assessment preconditions arrive with PR74; never accepted here"
        )
    if preconditions.required_source_revision_refs:
        required = tuple(preconditions.required_source_revision_refs)
        observed = current_view.content_revision_ref
        if observed not in required:
            raise SourceRevisionMismatchError(
                required_refs=required, observed_ref=observed
            )
    for check in sorted(preconditions.target_checks, key=lambda c: c.node_id):
        try:
            current_text = current_view.text_of(check.node_id)
        except KernelError:
            raise MissingViewTargetError(
                node_id=check.node_id,
                view_revision_id=current_view.view_revision_id(),
            ) from None
        observed_hash = view_text_hash(current_text)
        if observed_hash != check.before_hash:
            raise BeforeHashMismatchError(
                node_id=check.node_id,
                expected_hash=check.before_hash,
                observed_hash=observed_hash,
            )


# ---------------------------------------------------------------------------
# Immutable view revision record
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class ViewDocumentRecord(KernelRecord):
    """One immutable derived view revision.

    Identity covers the source content revision, the canonical reading
    graph, and the text of every content node — so a view revision id is
    simultaneously the deterministic content digest of the declared
    view. ``evidence`` is lineage metadata only.
    """

    record_class: ClassVar[str] = "view_document"
    record_type: ClassVar[str] = RECORD_TYPE_VIEW_DOCUMENT
    schema_version: ClassVar[str] = PATCH_SCHEMA_VERSION

    content_revision_ref: str
    graph: ReadingOrderGraph
    texts: Mapping[str, str] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.content_revision_ref, field_name="content_revision_ref")
        if not isinstance(self.graph, ReadingOrderGraph):
            raise KernelError(
                f"graph must be a ReadingOrderGraph, got {type(self.graph).__name__}"
            )
        if not isinstance(self.texts, Mapping):
            raise KernelError(f"texts must be a mapping, got {self.texts!r}")
        content_nodes = {
            node.node_id for node in self.graph.nodes if node.kind == NODE_KIND_CONTENT
        }
        for node_id, text in self.texts.items():
            validate_record_ref(node_id, field_name="text node id")
            if not isinstance(text, str):
                raise KernelError(
                    f"text of {node_id!r} must be str, got {type(text).__name__}"
                )
            if node_id not in content_nodes:
                raise KernelError(
                    f"texts carries {node_id!r} which is not a content node of "
                    "the graph; a view never fabricates node state"
                )
        missing = sorted(content_nodes - set(self.texts))
        if missing:
            raise KernelError(
                f"view is incomplete: content nodes without a text value: {missing}"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "content_revision_ref": self.content_revision_ref,
            "graph": self.graph.canonical_payload(),
            "texts": dict(self.texts),
        }

    def view_revision_id(self) -> str:
        """Deterministic identity/digest of this declared view state."""
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=self.identity_payload(),
        )

    def text_of(self, node_id: str) -> str:
        node = self.graph.node(node_id)  # raises KernelError for unknown ids
        if node.kind != NODE_KIND_CONTENT:
            raise KernelError(f"node {node_id!r} is a region; regions carry no text")
        return self.texts[node_id]

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> ViewDocumentRecord:
        if not isinstance(payload, Mapping):
            raise KernelError(f"view payload must be a mapping, got {payload!r}")
        allowed = {"content_revision_ref", "graph", "texts"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown view payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            content_revision_ref=payload["content_revision_ref"],
            graph=ReadingOrderGraph.from_payload(payload["graph"]),
            texts=dict(payload.get("texts") or {}),
        )


# ---------------------------------------------------------------------------
# Proposal & outcome records
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class PatchProposalRecord(KernelRecord):
    """One conditional proposal against an identified prior view revision.

    Identity covers the preconditions and the normalized operation list
    (order matters: operations apply sequentially, and commutativity is
    never assumed). ``producer`` is evidence-only.
    """

    record_class: ClassVar[str] = "patch_proposal"
    record_type: ClassVar[str] = RECORD_TYPE_PATCH_PROPOSAL
    schema_version: ClassVar[str] = PATCH_SCHEMA_VERSION

    preconditions: PatchPreconditions
    operations: tuple[PatchOperation, ...] = ()
    producer: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.preconditions, PatchPreconditions):
            raise KernelError(
                "preconditions must be PatchPreconditions, got "
                f"{type(self.preconditions).__name__}"
            )
        ops = tuple(self.operations)
        for op in ops:
            if not isinstance(op, PatchOperation):
                raise KernelError(
                    f"operations must be PatchOperation, got {type(op).__name__}"
                )
        self.operations = ops
        if not isinstance(self.producer, Mapping):
            raise KernelError(f"producer must be a mapping, got {self.producer!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "preconditions": self.preconditions.canonical_value(),
            "operations": [op.canonical_value() for op in self.operations],
        }

    def proposal_id(self) -> str:
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=self.identity_payload(),
        )

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> PatchProposalRecord:
        if not isinstance(payload, Mapping):
            raise KernelError(f"proposal payload must be a mapping, got {payload!r}")
        allowed = {"preconditions", "operations"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown proposal payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            preconditions=PatchPreconditions.from_canonical(payload["preconditions"]),
            operations=tuple(
                PatchOperation.from_canonical(op)
                for op in payload.get("operations") or []
            ),
        )


@dataclass(kw_only=True)
class PatchOutcomeRecord(KernelRecord):
    """The durable decision lineage entry for one proposal.

    Only accepted outcomes are committed in this slice (a rejected patch
    raises its typed conflict and leaves nothing behind). ``observed``
    records what was true at evaluation — the matched base revision and
    source binding — so history can explain the acceptance without
    trusting the proposal's claims. Identity uses the proposal's
    *semantic* identity (never its event id).
    """

    record_class: ClassVar[str] = "patch_outcome"
    record_type: ClassVar[str] = RECORD_TYPE_PATCH_OUTCOME
    schema_version: ClassVar[str] = PATCH_SCHEMA_VERSION

    proposal_ref: str
    proposal_identity: str
    outcome: str
    observed: Mapping[str, Any] = field(default_factory=dict)
    resulting_revision_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.proposal_ref, field_name="proposal_ref")
        if not isinstance(self.proposal_identity, str) or not HASH_PATTERN.match(
            self.proposal_identity
        ):
            raise KernelError(
                f"invalid proposal_identity: {self.proposal_identity!r} must "
                f"match {HASH_PATTERN.pattern}"
            )
        if self.outcome not in PATCH_OUTCOMES:
            raise KernelError(
                f"invalid outcome {self.outcome!r}; allowed: {sorted(PATCH_OUTCOMES)}"
            )
        if self.outcome == PATCH_OUTCOME_ACCEPTED and not (
            isinstance(self.resulting_revision_id, str)
            and HASH_PATTERN.match(self.resulting_revision_id)
        ):
            raise KernelError(
                "an accepted outcome must name its resulting view revision"
            )
        if not isinstance(self.observed, Mapping):
            raise KernelError(f"observed must be a mapping, got {self.observed!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "proposal_identity": self.proposal_identity,
            "outcome": self.outcome,
            "observed": dict(self.observed),
            "resulting_revision_id": self.resulting_revision_id,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> PatchOutcomeRecord:
        if not isinstance(payload, Mapping):
            raise KernelError(f"outcome payload must be a mapping, got {payload!r}")
        allowed = {
            "proposal_ref",
            "proposal_identity",
            "outcome",
            "observed",
            "resulting_revision_id",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown outcome payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            proposal_ref=payload["proposal_ref"],
            proposal_identity=payload["proposal_identity"],
            outcome=payload["outcome"],
            observed=dict(payload.get("observed") or {}),
            resulting_revision_id=payload.get("resulting_revision_id"),
        )


# ---------------------------------------------------------------------------
# View advancement (the commit-transaction seam input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewAdvancement:
    """Request to move a view's current revision inside a kernel commit.

    Exactly one of three forms is valid (validated at construction, and
    re-validated against durable state inside the commit transaction):

    * **genesis** — ``base_revision_id is None`` and no proposal: the
      first revision of a view. The head row is inserted; a second
      genesis for an initialized view is a stale-base conflict.
    * **patch** — ``base_revision_id`` + ``proposal_record_id``: the
      proposal record must be in the same batch; the commit evaluates
      its preconditions against the current revision and independently
      re-applies its operations to verify the result revision.
    * **rebuild** — ``base_revision_id`` + ``verified_rebuild=True``: a
      source rebase whose new revision the commit verifies by replaying
      the rebase operation's declared proposals from its declared source
      facts (the clean-rebuild oracle runs transactionally).

    The head flip is a conditional update under the SQLite writer lock
    the commit already holds, so advancement linearizes with the commit:
    either the whole batch (records + flip) lands or none of it does.
    """

    new_revision_id: str
    view_id: str = DEFAULT_VIEW_ID
    base_revision_id: str | None = None
    proposal_record_id: str | None = None
    verified_rebuild: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.view_id, str) or not VIEW_ID_PATTERN.match(self.view_id):
            raise InvalidViewAdvancementError(
                f"invalid view_id: {self.view_id!r} must match "
                f"{VIEW_ID_PATTERN.pattern}"
            )
        for name, value in (
            ("new_revision_id", self.new_revision_id),
            ("base_revision_id", self.base_revision_id),
        ):
            if value is not None and not HASH_PATTERN.match(value):
                raise InvalidViewAdvancementError(
                    f"invalid {name}: {value!r} must match {HASH_PATTERN.pattern}"
                )
        if self.proposal_record_id is not None:
            validate_record_ref(
                self.proposal_record_id, field_name="proposal_record_id"
            )
        if self.base_revision_id is None:
            if self.proposal_record_id is not None or self.verified_rebuild:
                raise InvalidViewAdvancementError(
                    "genesis advancement initializes a view and carries neither "
                    "a proposal nor a rebuild verification flag"
                )
        elif self.proposal_record_id is None and not self.verified_rebuild:
            raise InvalidViewAdvancementError(
                "an advancing request must name the proposal it applies or "
                "declare itself a verified rebuild; the head never moves on "
                "unvalidated state"
            )
        elif self.proposal_record_id is not None and self.verified_rebuild:
            raise InvalidViewAdvancementError(
                "an advancement is either a patch proposal or a verified "
                "rebuild, never both"
            )


# ---------------------------------------------------------------------------
# Pure operation application
# ---------------------------------------------------------------------------


def apply_operation(
    graph: ReadingOrderGraph,
    texts: Mapping[str, str],
    operation: PatchOperation,
) -> tuple[ReadingOrderGraph, dict[str, str]]:
    """Apply one operation to a view state, returning the new state.

    Pure and total on valid input: missing targets raise
    :class:`MissingViewTargetError`, structural split failures raise
    ``KernelError``/``OrderConflictError`` from the reading-order domain,
    and the inputs are never mutated.
    """
    if operation.op_type == OP_TYPE_REPLACE_TEXT:
        params = operation.params
        try:
            node = graph.node(params["node_id"])
        except KernelError:
            raise MissingViewTargetError(node_id=params["node_id"]) from None
        if node.kind != NODE_KIND_CONTENT:
            raise KernelError(
                f"replace_text target {params['node_id']!r} is a region; "
                "regions carry no text"
            )
        new_texts = dict(texts)
        new_texts[params["node_id"]] = params["after_text"]
        return graph, new_texts

    if operation.op_type == OP_TYPE_SPLIT_NODE:
        params = operation.params
        node_id = params["node_id"]
        try:
            target = graph.node(node_id)
        except KernelError:
            raise MissingViewTargetError(node_id=node_id) from None
        if target.kind != NODE_KIND_CONTENT:
            raise KernelError(
                f"split target {node_id!r} is a region; only content nodes split"
            )
        children = [
            OrderNode(
                node_id=child["node_id"],
                kind=NODE_KIND_CONTENT,
                anchor_ref=(
                    child["anchor_ref"]
                    if child["anchor_ref"] is not None
                    else target.anchor_ref
                ),
            )
            for child in params["children"]
        ]
        result = split_node(
            graph,
            node_id,
            children,
            params.get("child_order"),
            producer=params["producer"],
        )
        new_texts = dict(texts)
        del new_texts[node_id]
        for child in params["children"]:
            new_texts[child["node_id"]] = child["text"]
        return result.graph, new_texts

    raise KernelError(
        f"operation {operation.op_type!r} applies against a current view, "
        "not during replay; use apply_rebase_source for source rebuilds"
    )


@dataclass(frozen=True)
class RebaseReplayResult:
    """Outcome of replaying proposals against a fresh source-derived base.

    ``view`` is the resulting state; ``applied``/``dropped`` name the
    proposals whose preconditions held/failed, with the typed reason for
    each drop. A dropped patch is honest supersession: its before-value
    claims no longer hold against the new source, so it must be
    re-proposed by a human decision, never silently re-targeted.
    """

    view: ViewDocumentRecord
    applied_refs: tuple[str, ...]
    dropped_refs: tuple[tuple[str, str], ...]


def apply_rebase_source(
    operation: PatchOperation,
    proposals: Mapping[str, PatchProposalRecord],
) -> RebaseReplayResult:
    """Build the rebased view: fresh source base + preconditioned replay.

    The result is a pure function of the operation's declared source
    facts and the named proposals — the same replay the commit
    transaction runs to verify a rebuild advancement, and the same
    replay the clean-rebuild oracle uses.
    """
    if operation.op_type != OP_TYPE_REBASE_SOURCE:
        raise KernelError(f"expected rebase_source, got {operation.op_type!r}")
    params = operation.params
    graph = params["source_graph"]
    texts = dict(params["source_texts"])
    content_nodes = {
        node.node_id for node in graph.nodes if node.kind == NODE_KIND_CONTENT
    }
    if set(texts) != content_nodes:
        raise KernelError(
            "source_texts must cover exactly the content nodes of source_graph"
        )

    missing = [ref for ref in params["replay_proposal_refs"] if ref not in proposals]
    if missing:
        raise KernelError(f"rebase references unknown proposals: {missing}")

    applied: list[str] = []
    dropped: list[tuple[str, str]] = []
    current_revision_ref = params["new_content_revision_ref"]
    for ref in params["replay_proposal_refs"]:
        proposal = proposals[ref]
        # A replayed patch must itself be a view patch (not another
        # rebase): rebuilds chain through explicit rebase proposals.
        replayable = [
            op for op in proposal.operations if op.op_type != OP_TYPE_REBASE_SOURCE
        ]
        if len(replayable) != len(proposal.operations):
            dropped.append((ref, "contains_rebase_operation"))
            continue
        intermediate = ViewDocumentRecord(
            record_id="replay-intermediate",
            content_revision_ref=current_revision_ref,
            graph=graph,
            texts=texts,
        )
        try:
            evaluate_preconditions(intermediate, proposal.preconditions)
            for op in replayable:
                graph, texts = apply_operation(graph, texts, op)
        except KernelError as exc:
            # The dropped proposal may have failed mid-application; the
            # surviving state is exactly the pristine source base plus
            # the prefix of already-applied proposals, recomputed from
            # scratch so replay never depends on partial-application state.
            graph, texts = _replay_without(
                params, proposals, tuple(applied)
            )
            dropped.append((ref, type(exc).__name__))
            continue
        applied.append(ref)

    view = ViewDocumentRecord(
        record_id="rebase-result",
        content_revision_ref=current_revision_ref,
        graph=graph,
        texts=texts,
    )
    return RebaseReplayResult(
        view=view,
        applied_refs=tuple(applied),
        dropped_refs=tuple(dropped),
    )


def _replay_without(
    params: Mapping[str, Any],
    proposals: Mapping[str, PatchProposalRecord],
    refs: tuple[str, ...],
) -> tuple[ReadingOrderGraph, dict[str, str]]:
    """Re-replay the prefix of successfully applied proposals from the
    pristine source base (used to recover state after a dropped proposal
    surfaced mid-replay)."""
    graph = params["source_graph"]
    texts = dict(params["source_texts"])
    for ref in refs:
        proposal = proposals[ref]
        for op in proposal.operations:
            if op.op_type == OP_TYPE_REBASE_SOURCE:
                continue
            graph, texts = apply_operation(graph, texts, op)
    return graph, texts
