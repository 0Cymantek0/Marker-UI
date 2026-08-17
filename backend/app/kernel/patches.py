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
preconditions are live since PR74: ``required_claims`` carries typed
:class:`~app.kernel.proofs.ClaimRequirement` entries evaluated
authoritatively against committed assessment state inside the commit
transaction (see :mod:`app.kernel.proofs`); the PR73-era placeholder
key ``required_claim_refs`` is accepted on rematerialization only when
empty and points to the new field.

Operations stay domain-specific payloads behind this small envelope;
there is deliberately no universal patch language. v1 ships three
operations: ``replace_text`` (the declared reversible tracer),
``split_node`` (mirrors the PR72 bounded specialist split), and
``rebase_source`` (rebuild the view against a new source revision by
replaying accepted patches under their preconditions).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from sqlalchemy import select

from app.kernel.errors import (
    BeforeHashMismatchError,
    InvalidViewAdvancementError,
    KernelError,
    MissingViewTargetError,
    SourceRevisionMismatchError,
    StaleBaseRevisionError,
)
from app.kernel.proofs import ClaimRequirement, evaluate_claim_requirements
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

    ``required_claims`` (PR74) gates the patch on claim/assessment
    state: each :class:`~app.kernel.proofs.ClaimRequirement` must be
    satisfied by committed, in-policy, fresh, structurally valid
    assessment state when the commit transaction evaluates the
    advancement — missing, stale, wrong-assertion, policy-mismatched,
    or proof-invalid assessments fail closed and roll back the entire
    patch commit.
    """

    base_revision_id: str | None
    target_checks: tuple[TargetCheck, ...] = ()
    required_source_revision_refs: tuple[str, ...] = ()
    required_claims: tuple[ClaimRequirement, ...] = ()

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
        claim_reqs = tuple(self.required_claims)
        for req in claim_reqs:
            if not isinstance(req, ClaimRequirement):
                raise KernelError(
                    f"required_claims must be ClaimRequirement, got {type(req).__name__}"
                )
        object.__setattr__(self, "required_claims", claim_reqs)

    def canonical_value(self) -> dict[str, Any]:
        return {
            "base_revision_id": self.base_revision_id,
            "target_checks": [
                check.canonical_value() for check in sorted(
                    self.target_checks, key=lambda c: c.node_id
                )
            ],
            "required_source_revision_refs": sorted(self.required_source_revision_refs),
            "required_claims": [req.canonical_value() for req in self.required_claims],
        }

    @classmethod
    def from_canonical(cls, value: Mapping[str, Any]) -> PatchPreconditions:
        if not isinstance(value, Mapping):
            raise KernelError(f"preconditions must be a mapping, got {value!r}")
        allowed = {
            "base_revision_id",
            "target_checks",
            "required_source_revision_refs",
            # PR73 placeholder key: only its empty form was ever
            # committable, so accepting empty payloads keeps stored
            # proposals rematerializable; non-empty values point at the
            # PR74 field instead of failing with an opaque message.
            "required_claim_refs",
            "required_claims",
        }
        unknown = set(value) - allowed
        if unknown:
            raise KernelError(f"unknown precondition fields {sorted(unknown)}")
        legacy_claim_refs = value.get("required_claim_refs") or []
        if legacy_claim_refs:
            raise KernelError(
                "required_claim_refs was the PR73 fail-closed placeholder; use "
                "required_claims (typed ClaimRequirement entries) instead"
            )
        return cls(
            base_revision_id=value.get("base_revision_id"),
            target_checks=tuple(
                TargetCheck(**check) for check in value.get("target_checks") or []
            ),
            required_source_revision_refs=tuple(
                value.get("required_source_revision_refs") or ()
            ),
            required_claims=tuple(
                ClaimRequirement.from_canonical(req)
                for req in value.get("required_claims") or []
            ),
        )


def evaluate_preconditions(
    current_view: ViewDocumentRecord, preconditions: PatchPreconditions
) -> None:
    """Evaluate every view-local precondition against one current view.

    Pure: raises the typed conflict for the FIRST violated precondition
    (stale-base comparison itself belongs to the view head, not to a
    single document). This is the same evaluation the commit transaction
    runs authoritatively. Claim requirements are deliberately NOT
    evaluated here — they need committed assessment state, and their
    authoritative evaluation lives in
    :func:`app.kernel.proofs.evaluate_claim_requirements`, called
    inside the commit transaction by ``check_view_advancement``.
    """
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
    #: Logical view this revision belongs to. The default keeps the
    #: one-document workspaces of earlier slices; distinct view ids let
    #: one workspace carry multiple view documents (PR78 multi-domain
    #: corpora) with independent view heads.
    view_id: str = DEFAULT_VIEW_ID
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.content_revision_ref, field_name="content_revision_ref")
        if not isinstance(self.graph, ReadingOrderGraph):
            raise KernelError(
                f"graph must be a ReadingOrderGraph, got {type(self.graph).__name__}"
            )
        if not isinstance(self.view_id, str) or not VIEW_ID_PATTERN.match(self.view_id):
            raise KernelError(
                f"invalid view_id: {self.view_id!r} must match "
                f"{VIEW_ID_PATTERN.pattern}"
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
        payload: dict[str, Any] = {
            "content_revision_ref": self.content_revision_ref,
            "graph": self.graph.canonical_payload(),
            "texts": dict(self.texts),
        }
        # Included only when non-default so pre-PR78 stored view rows
        # keep verifying byte-for-byte against their recorded identity.
        if self.view_id != DEFAULT_VIEW_ID:
            payload["view_id"] = self.view_id
        return payload

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
        allowed = {"content_revision_ref", "graph", "texts", "view_id"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown view payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            content_revision_ref=payload["content_revision_ref"],
            graph=ReadingOrderGraph.from_payload(payload["graph"]),
            texts=dict(payload.get("texts") or {}),
            view_id=str(payload.get("view_id") or DEFAULT_VIEW_ID),
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
    trusting the proposal's claims. The proposal link is the proposal's
    *semantic* identity (never its event id); the same-commit grouping
    in :func:`app.kernel.patching.load_view_history` and the lineage
    edges carry record-id linkage.
    """

    record_class: ClassVar[str] = "patch_outcome"
    record_type: ClassVar[str] = RECORD_TYPE_PATCH_OUTCOME
    schema_version: ClassVar[str] = PATCH_SCHEMA_VERSION

    proposal_identity: str
    outcome: str
    observed: Mapping[str, Any] = field(default_factory=dict)
    resulting_revision_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
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

    Exactly one of two forms is valid (validated at construction, and
    re-validated against durable state inside the commit transaction):

    * **genesis** — ``base_revision_id is None`` and no proposal: the
      first revision of a view. The head row is inserted; a second
      genesis for an initialized view is a stale-base conflict.
    * **proposal** — ``base_revision_id`` + ``proposal_record_id``: the
      proposal record must be in the same batch. The commit evaluates
      its preconditions against the current revision and *independently
      recomputes* the result — a view patch by re-applying its
      operations to the current view, a ``rebase_source`` proposal by
      replaying its declared proposals from its declared source facts
      (the clean-rebuild oracle runs transactionally). The recomputed
      revision must equal ``new_revision_id`` exactly.

    The head flip is a conditional update under the SQLite writer lock
    the commit already holds, so advancement linearizes with the commit:
    either the whole batch (records + flip) lands or none of it does.
    """

    new_revision_id: str
    view_id: str = DEFAULT_VIEW_ID
    base_revision_id: str | None = None
    proposal_record_id: str | None = None

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
            if self.proposal_record_id is not None:
                raise InvalidViewAdvancementError(
                    "genesis advancement initializes a view and carries no proposal"
                )
        elif self.proposal_record_id is None:
            raise InvalidViewAdvancementError(
                "an advancing request must name the proposal it applies; the "
                "head never moves on unvalidated state"
            )


# ---------------------------------------------------------------------------
# Transactional advancement evaluation (runs inside the commit transaction)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedViewRef:
    """Batch-side view of one prepared record (class, identity, payload).

    Decouples the evaluator from the commit service's internal types
    while giving it exactly what verification needs.
    """

    record_id: str
    record_class: str
    identity_hash: str
    payload_json: str


@dataclass(frozen=True)
class ViewFlip:
    """The head movement the commit must perform after a passing check.

    ``insert`` creates the first head row for the view (genesis);
    ``update`` conditionally replaces ``expected_base_revision_id`` and
    must affect exactly one row or the whole commit fails.
    """

    kind: str  # "insert" | "update"
    workspace_id: str
    view_id: str
    expected_base_revision_id: str | None
    new_revision_id: str
    kernel_commit_id: int


async def _load_view_document(session, workspace_id: str, revision_id: str):
    from app.kernel.models import KernelRecord as KernelRecordRow

    row = (
        await session.execute(
            select(KernelRecordRow.payload_json).where(
                KernelRecordRow.workspace_id == workspace_id,
                KernelRecordRow.identity_hash == revision_id,
                KernelRecordRow.record_class == "view_document",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise InvalidViewAdvancementError(
            f"view head names revision {revision_id!r} but no committed view "
            "document record carries that identity; refusing to advance over "
            "unverifiable state"
        )
    return ViewDocumentRecord.from_payload(json.loads(row), record_id="head-view")


async def check_view_advancement(
    session,
    *,
    workspace_id: str,
    advancement: ViewAdvancement,
    prepared_records: Mapping[str, PreparedViewRef],
    next_commit_id: int,
) -> ViewFlip:
    """Authoritative in-transaction evaluation of one view advancement.

    Runs under the writer lock the commit service already holds. Every
    precondition is checked against durable current state, and the
    result revision is *independently recomputed* — never trusted from
    the advancement request. Returns the flip to execute; any violation
    raises a typed conflict and the entire batch rolls back.
    """
    from app.kernel.models import KernelRecord as KernelRecordRow
    from app.kernel.models import KernelViewHead

    advanced_in_batch = [
        ref
        for ref in prepared_records.values()
        if ref.record_class == "view_document"
        and ref.identity_hash == advancement.new_revision_id
    ]
    if not advanced_in_batch:
        # A head may also move BACK to an already-committed revision —
        # deterministic reversal — but only to state this workspace has
        # actually committed; it never names fabricated identity.
        row = (
            await session.execute(
                select(KernelRecordRow.id).where(
                    KernelRecordRow.workspace_id == workspace_id,
                    KernelRecordRow.identity_hash == advancement.new_revision_id,
                    KernelRecordRow.record_class == "view_document",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise InvalidViewAdvancementError(
                "the advanced revision must be committed in the same batch or "
                "already exist as committed view state; a head never names "
                "state the kernel cannot prove"
            )

    head = (
        await session.execute(
            select(KernelViewHead).where(
                KernelViewHead.workspace_id == workspace_id,
                KernelViewHead.view_id == advancement.view_id,
            )
        )
    ).scalar_one_or_none()

    if advancement.base_revision_id is None:
        if head is not None:
            raise StaleBaseRevisionError(
                expected_base_revision_id=None,
                observed_base_revision_id=head.current_revision_id,
            )
        return ViewFlip(
            kind="insert",
            workspace_id=workspace_id,
            view_id=advancement.view_id,
            expected_base_revision_id=None,
            new_revision_id=advancement.new_revision_id,
            kernel_commit_id=next_commit_id,
        )

    if head is None:
        raise StaleBaseRevisionError(
            expected_base_revision_id=advancement.base_revision_id,
            observed_base_revision_id=None,
        )
    if head.current_revision_id != advancement.base_revision_id:
        raise StaleBaseRevisionError(
            expected_base_revision_id=advancement.base_revision_id,
            observed_base_revision_id=head.current_revision_id,
        )
    current_view = await _load_view_document(
        session, workspace_id, head.current_revision_id
    )

    proposal_ref = prepared_records.get(advancement.proposal_record_id or "")
    if (
        proposal_ref is None
        or proposal_ref.record_class != "patch_proposal"
    ):
        raise InvalidViewAdvancementError(
            "advancement names a proposal that is not part of this batch"
        )
    proposal = PatchProposalRecord.from_payload(
        json.loads(proposal_ref.payload_json),
        record_id=advancement.proposal_record_id or "proposal",
    )
    if proposal.preconditions.base_revision_id != advancement.base_revision_id:
        raise InvalidViewAdvancementError(
            "the proposal targets a different base revision than the "
            "advancement declares"
    )
    # PR74 claim preconditions: evaluated authoritatively here, under
    # the writer lock, against committed assessment state — a missing,
    # stale, policy-mismatched, or proof-invalid assessment raises the
    # typed conflict and rolls the whole patch commit back.
    await evaluate_claim_requirements(
        session,
        workspace_id,
        proposal.preconditions.required_claims,
        current_head=next_commit_id - 1,
    )

    rebase_ops = [
        op for op in proposal.operations if op.op_type == OP_TYPE_REBASE_SOURCE
    ]
    if rebase_ops:
        if len(proposal.operations) != 1:
            raise InvalidViewAdvancementError(
                "a rebase proposal carries exactly one rebase_source operation"
            )
        replay: dict[str, PatchProposalRecord] = {}
        for ref in rebase_ops[0].params["replay_proposal_refs"]:
            row = (
                await session.execute(
                    select(KernelRecordRow.payload_json).where(
                        KernelRecordRow.id == ref,
                        KernelRecordRow.workspace_id == workspace_id,
                        KernelRecordRow.record_class == "patch_proposal",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise InvalidViewAdvancementError(
                    f"rebase references proposal {ref!r} which is not committed "
                    "in this workspace"
                )
            replay[ref] = PatchProposalRecord.from_payload(
                json.loads(row), record_id=ref
            )
        verified = apply_rebase_source(rebase_ops[0], replay).view
    else:
        evaluate_preconditions(current_view, proposal.preconditions)
        graph, texts = current_view.graph, dict(current_view.texts)
        for op in proposal.operations:
            graph, texts = apply_operation(graph, texts, op)
        verified = ViewDocumentRecord(
            record_id="verified-view",
            content_revision_ref=current_view.content_revision_ref,
            graph=graph,
            texts=texts,
        )

    if verified.view_revision_id() != advancement.new_revision_id:
        raise InvalidViewAdvancementError(
            "independently recomputed revision "
            f"{verified.view_revision_id()} does not equal the advanced "
            f"revision {advancement.new_revision_id}; the head never moves to "
            "state the proposal does not prove"
        )
    return ViewFlip(
        kind="update",
        workspace_id=workspace_id,
        view_id=advancement.view_id,
        expected_base_revision_id=head.current_revision_id,
        new_revision_id=advancement.new_revision_id,
        kernel_commit_id=next_commit_id,
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
