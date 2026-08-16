"""Partial-order reading-order primitives (V3.2 TB2 Slice B / PR72).

Reading order is evidence, not a global sequence. The graph stores
only what producers actually observed:

* ``contains``   — containment (a strict single-parent tree);
* ``before``     — a before/after constraint with producer, confidence,
  and evidence state (``asserted`` or ``unresolved``);
* ``member_of``  — column/region membership (target must be a region);
* ``continues``  — flow-break/continuation hypothesis across boundaries.

Rules (master-plan amendment 8C.2):

* unknown total order is NOT invented: nodes without constraints stay
  unordered in the graph; a linearization is a policy view that reports
  every tie it broke;
* contradictory asserted constraints raise :class:`OrderConflictError`;
  an ``unresolved`` edge is a representable alternative, never silently
  promoted;
* ordering between a containment ancestor and descendant is a category
  error and is rejected;
* serialization is canonical and deterministic regardless of insertion
  order, dictionary iteration, or process hash seed: nodes are sorted
  by id, edges by their canonical JSON bytes.

Durability: :class:`ReadingOrderRecord` wraps the canonical graph
payload in one kernel record (``marker.kernel.reading_order.v1``).
Kernel dependency edges cannot carry producer/confidence/state (the
edge table has no payload column), so the graph lives in the record
payload and rematerializes via :meth:`ReadingOrderGraph.from_payload`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Mapping

from app.kernel.errors import KernelError, OrderConflictError
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import (
    DecimalValue,
    canonical_json_bytes,
    record_identity_hash,
    to_json_ready,
)

READING_ORDER_SCHEMA_VERSION = "1.0.0"
RECORD_TYPE_READING_ORDER = "marker.kernel.reading_order.v1"

ORDER_EDGE_CONTAINS = "contains"
ORDER_EDGE_BEFORE = "before"
ORDER_EDGE_MEMBER_OF = "member_of"
ORDER_EDGE_CONTINUES = "continues"

ORDER_EDGE_KINDS = frozenset(
    {
        ORDER_EDGE_CONTAINS,
        ORDER_EDGE_BEFORE,
        ORDER_EDGE_MEMBER_OF,
        ORDER_EDGE_CONTINUES,
    }
)
#: Edge kinds that assert pairwise document order (cycle-checked).
_ORDERING_EDGE_KINDS = frozenset({ORDER_EDGE_BEFORE, ORDER_EDGE_CONTINUES})

EVIDENCE_STATE_ASSERTED = "asserted"
EVIDENCE_STATE_UNRESOLVED = "unresolved"
EVIDENCE_STATES = frozenset({EVIDENCE_STATE_ASSERTED, EVIDENCE_STATE_UNRESOLVED})

NODE_KIND_CONTENT = "content"
NODE_KIND_REGION = "region"
NODE_KINDS = frozenset({NODE_KIND_CONTENT, NODE_KIND_REGION})

#: Policies for the derived linearization view. A policy decides how
#: unordered peers are tie-broken; every tie is reported as ambiguity,
#: so the view can never masquerade as stored truth.
LINEARIZATION_POLICY_CANONICAL_ID = "canonical_id"


def order_confidence(text: str) -> DecimalValue:
    """Validate a producer confidence value for an order edge.

    Canonical decimal text in [0, 1]. Confidence is recorded evidence
    about a constraint, never an identity-promoting score.
    """
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError) as exc:
        raise KernelError(f"invalid confidence {text!r}: not a decimal") from exc
    if not Decimal(0) <= value <= Decimal(1):
        raise KernelError(f"invalid confidence {text!r}: must lie in [0, 1]")
    return DecimalValue.from_decimal(value)


@dataclass(frozen=True)
class OrderNode:
    """One addressable position in the reading-order graph.

    Content nodes may carry an anchor record ref; region nodes name
    columns/containers and stay anchor-free.
    """

    node_id: str
    kind: str = NODE_KIND_CONTENT
    anchor_ref: str | None = None

    def __post_init__(self) -> None:
        validate_record_ref(self.node_id, field_name="node_id")
        if self.kind not in NODE_KINDS:
            raise KernelError(
                f"invalid node kind {self.kind!r}; allowed: {sorted(NODE_KINDS)}"
            )
        if self.anchor_ref is not None:
            validate_record_ref(self.anchor_ref, field_name="anchor_ref")
            if self.kind != NODE_KIND_CONTENT:
                raise KernelError("only content nodes carry anchor refs")

    def canonical_value(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "anchor_ref": self.anchor_ref,
        }


@dataclass(frozen=True)
class OrderEdge:
    """One reading-order evidence edge with full lineage."""

    kind: str
    source_id: str
    target_id: str
    producer: str
    confidence: DecimalValue
    state: str = EVIDENCE_STATE_ASSERTED

    def __post_init__(self) -> None:
        if self.kind not in ORDER_EDGE_KINDS:
            raise KernelError(
                f"unknown order edge kind {self.kind!r}; allowed: {sorted(ORDER_EDGE_KINDS)}"
            )
        validate_record_ref(self.source_id, field_name="source_id")
        validate_record_ref(self.target_id, field_name="target_id")
        if self.source_id == self.target_id:
            raise KernelError(f"{self.kind} edge cannot be self-referential")
        if not isinstance(self.producer, str) or not self.producer:
            raise KernelError(f"invalid producer: {self.producer!r}")
        if not isinstance(self.confidence, DecimalValue):
            raise KernelError("confidence must be a DecimalValue built via order_confidence")
        if self.state not in EVIDENCE_STATES:
            raise KernelError(
                f"invalid evidence state {self.state!r}; allowed: {sorted(EVIDENCE_STATES)}"
            )

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.source_id, self.target_id, self.state)

    def canonical_value(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source_id,
            "target": self.target_id,
            "producer": self.producer,
            "confidence": self.confidence.canonical_value(),
            "state": self.state,
        }


def _edge_from_canonical(value: Mapping[str, Any]) -> OrderEdge:
    if not isinstance(value, Mapping):
        raise KernelError(f"canonical order edge must be a mapping, got {value!r}")
    allowed = {"kind", "source", "target", "producer", "confidence", "state"}
    unknown = set(value) - allowed
    if unknown:
        raise KernelError(f"unknown order edge fields {sorted(unknown)}")
    try:
        return OrderEdge(
            kind=value["kind"],
            source_id=value["source"],
            target_id=value["target"],
            producer=value["producer"],
            confidence=order_confidence(value["confidence"]),
            state=value["state"],
        )
    except KeyError as exc:
        raise KernelError(f"order edge is missing required field {exc.args[0]!r}") from None


def _node_from_canonical(value: Mapping[str, Any]) -> OrderNode:
    if not isinstance(value, Mapping):
        raise KernelError(f"canonical order node must be a mapping, got {value!r}")
    allowed = {"node_id", "kind", "anchor_ref"}
    unknown = set(value) - allowed
    if unknown:
        raise KernelError(f"unknown order node fields {sorted(unknown)}")
    try:
        return OrderNode(
            node_id=value["node_id"], kind=value["kind"], anchor_ref=value.get("anchor_ref")
        )
    except KeyError as exc:
        raise KernelError(f"order node is missing required field {exc.args[0]!r}") from None


@dataclass(frozen=True)
class ReadingOrderGraph:
    """An immutable, validated partial-order document graph."""

    nodes: tuple[OrderNode, ...]
    edges: tuple[OrderEdge, ...]

    def __post_init__(self) -> None:
        self._validate()

    # -- construction ----------------------------------------------------

    @classmethod
    def build(
        cls, nodes: Any = (), edges: Any = ()
    ) -> ReadingOrderGraph:
        node_map: dict[str, OrderNode] = {}
        for node in nodes:
            if not isinstance(node, OrderNode):
                raise KernelError(f"graph nodes must be OrderNode, got {type(node).__name__}")
            if node.node_id in node_map:
                raise KernelError(f"duplicate node id {node.node_id!r}")
            node_map[node.node_id] = node
        edge_map: dict[tuple[str, str, str, str], OrderEdge] = {}
        for edge in edges:
            if not isinstance(edge, OrderEdge):
                raise KernelError(f"graph edges must be OrderEdge, got {type(edge).__name__}")
            if edge.key in edge_map:
                raise KernelError(
                    f"duplicate {edge.kind} edge {edge.source_id}->{edge.target_id} "
                    f"({edge.state}); supersession requires a new graph, not a silent "
                    "last-write-wins overwrite"
                )
            edge_map[edge.key] = edge
        return cls(nodes=tuple(node_map.values()), edges=tuple(edge_map.values()))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReadingOrderGraph:
        """Rematerialize a graph from its canonical payload, failing closed."""
        if not isinstance(payload, Mapping):
            raise KernelError(f"graph payload must be a mapping, got {payload!r}")
        if payload.get("schema") != READING_ORDER_SCHEMA_VERSION:
            raise KernelError(
                f"unsupported reading-order schema {payload.get('schema')!r}; "
                f"expected {READING_ORDER_SCHEMA_VERSION}"
            )
        nodes = payload.get("nodes")
        edges = payload.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise KernelError("graph payload requires nodes[] and edges[] lists")
        return cls.build(
            [_node_from_canonical(node) for node in nodes],
            [_edge_from_canonical(edge) for edge in edges],
        )

    # -- validation ------------------------------------------------------

    def _validate(self) -> None:
        node_ids = set()
        kinds: dict[str, str] = {}
        for node in self.nodes:
            if node.node_id in node_ids:
                raise KernelError(f"duplicate node id {node.node_id!r}")
            node_ids.add(node.node_id)
            kinds[node.node_id] = node.kind

        parents: dict[str, str] = {}
        seen_edge_keys: set[tuple[str, str, str, str]] = set()
        for edge in self.edges:
            if edge.key in seen_edge_keys:
                raise KernelError(
                    f"duplicate {edge.kind} edge {edge.source_id}->{edge.target_id} "
                    f"({edge.state}); supersession requires a new graph, not a silent "
                    "last-write-wins overwrite"
                )
            seen_edge_keys.add(edge.key)
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise KernelError(
                    f"{edge.kind} edge references unknown node "
                    f"{edge.source_id}->{edge.target_id}"
                )
            if edge.kind == ORDER_EDGE_CONTAINS:
                if edge.target_id in parents:
                    raise KernelError(
                        f"node {edge.target_id!r} is contained by both "
                        f"{parents[edge.target_id]!r} and {edge.source_id!r}; overlapping "
                        "containment requires an explicit resolution, not a silent merge"
                    )
                parents[edge.target_id] = edge.source_id
            elif edge.kind == ORDER_EDGE_MEMBER_OF:
                if kinds[edge.target_id] != NODE_KIND_REGION:
                    raise KernelError(
                        f"member_of target {edge.target_id!r} must be a region node"
                    )

        # Containment must be a tree (single parent already enforced; no cycles).
        for child in parents:
            seen: set[str] = set()
            current = child
            while current in parents:
                if current in seen:
                    raise KernelError(f"containment cycle through {current!r}")
                seen.add(current)
                current = parents[current]

        # An ordering claim between a containment ancestor and descendant
        # conflates containment with order and is rejected outright.
        ancestors: dict[str, set[str]] = {}
        for node_id in node_ids:
            chain = set()
            current = node_id
            while current in parents:
                current = parents[current]
                chain.add(current)
            ancestors[node_id] = chain
        for edge in self.edges:
            if edge.kind in _ORDERING_EDGE_KINDS:
                if edge.target_id in ancestors[edge.source_id] or edge.source_id in ancestors[edge.target_id]:
                    raise KernelError(
                        f"{edge.kind} edge {edge.source_id}->{edge.target_id} orders a "
                        "containment ancestor against its descendant; ordering lives "
                        "between peers"
                    )

        # Asserted ordering edges must be acyclic; unresolved edges are
        # alternatives and do not participate in the cycle contract.
        asserted_out: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        asserted_in: dict[str, int] = {node_id: 0 for node_id in node_ids}
        asserted_pairs: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.kind in _ORDERING_EDGE_KINDS and edge.state == EVIDENCE_STATE_ASSERTED:
                asserted_out[edge.source_id].append(edge.target_id)
                asserted_in[edge.target_id] += 1
                pair = (edge.source_id, edge.target_id)
                if (edge.target_id, edge.source_id) in asserted_pairs:
                    raise OrderConflictError(
                        f"contradictory asserted order: both {edge.source_id} and "
                        f"{edge.target_id} are asserted before each other"
                    )
                asserted_pairs.add(pair)

        ready = sorted(node_id for node_id in node_ids if asserted_in[node_id] == 0)
        emitted = 0
        while ready:
            current = ready.pop(0)
            emitted += 1
            for successor in sorted(asserted_out[current]):
                asserted_in[successor] -= 1
                if asserted_in[successor] == 0:
                    ready.append(successor)
            ready.sort()
        if emitted != len(node_ids):
            cyclic = sorted(
                node_id for node_id in node_ids if asserted_in[node_id] > 0
            )
            raise OrderConflictError(
                f"asserted ordering constraints form a cycle through {cyclic}; "
                "contradictions must be represented or rejected explicitly, never "
                "resolved by iteration order"
            )

    # -- canonical serialization -----------------------------------------

    def canonical_payload(self) -> dict[str, Any]:
        """Deterministic payload: nodes by id, edges by canonical bytes."""
        nodes = [node.canonical_value() for node in sorted(self.nodes, key=lambda n: n.node_id)]
        edge_values = [edge.canonical_value() for edge in self.edges]
        edge_values.sort(key=lambda value: canonical_json_bytes(to_json_ready(value)))
        return {
            "schema": READING_ORDER_SCHEMA_VERSION,
            "nodes": nodes,
            "edges": edge_values,
        }

    def graph_id(self) -> str:
        return record_identity_hash(
            record_type=RECORD_TYPE_READING_ORDER,
            schema_version=READING_ORDER_SCHEMA_VERSION,
            payload=to_json_ready(self.canonical_payload()),
        )

    # -- queries ----------------------------------------------------------

    def node(self, node_id: str) -> OrderNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KernelError(f"unknown node {node_id!r}")

    def parents_of(self, node_id: str) -> tuple[OrderEdge, ...]:
        return tuple(e for e in self.edges if e.kind == ORDER_EDGE_CONTAINS and e.target_id == node_id)

    def children_of(self, node_id: str) -> tuple[OrderEdge, ...]:
        return tuple(e for e in self.edges if e.kind == ORDER_EDGE_CONTAINS and e.source_id == node_id)

    def regions_of(self, node_id: str) -> tuple[OrderEdge, ...]:
        return tuple(e for e in self.edges if e.kind == ORDER_EDGE_MEMBER_OF and e.source_id == node_id)


# ---------------------------------------------------------------------------
# Derived linearization (a view, never stored truth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinearizationView:
    """A policy-derived sequence plus the ambiguity it had to break.

    ``ambiguous_groups`` lists every group of nodes the policy ordered
    without asserted evidence. An empty list means the sequence was
    fully constrained by asserted graph evidence.
    """

    policy: str
    sequence: tuple[str, ...]
    ambiguous_groups: tuple[tuple[str, ...], ...]


def linearize(graph: ReadingOrderGraph, policy: str = LINEARIZATION_POLICY_CANONICAL_ID) -> LinearizationView:
    """Flatten the graph into one deterministic sequence for rendering.

    Only ``asserted`` before/continues constraints order the sequence.
    Unconstrained peers are tie-broken by the declared policy (node id
    order) and reported verbatim in ``ambiguous_groups`` — the output is
    a visible policy result, not new graph truth. Contradictions were
    already rejected at validation; this function cannot invent order
    beyond the declared tie-break.
    """
    if policy != LINEARIZATION_POLICY_CANONICAL_ID:
        raise KernelError(
            f"unknown linearization policy {policy!r}; declared: "
            f"{LINEARIZATION_POLICY_CANONICAL_ID}"
        )

    node_ids = [node.node_id for node in graph.nodes]
    out: dict[str, tuple[str, ...]] = {node_id: () for node_id in node_ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    for edge in graph.edges:
        if (
            edge.kind in _ORDERING_EDGE_KINDS
            and edge.state == EVIDENCE_STATE_ASSERTED
        ):
            out[edge.source_id] = out[edge.source_id] + (edge.target_id,)
            indegree[edge.target_id] += 1

    ready = sorted(node_id for node_id in node_ids if indegree[node_id] == 0)
    sequence: list[str] = []
    ambiguous_groups: list[tuple[str, ...]] = []
    while ready:
        if len(ready) > 1:
            # Two nodes simultaneously ready have no asserted path in
            # either direction (any path would hold one of them behind
            # the other's emission), so every multi-ready step is pure
            # policy-resolved ambiguity.
            ambiguous_groups.append(tuple(ready))
        current = ready.pop(0)
        sequence.append(current)
        for successor in sorted(out[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
        ready.sort()

    return LinearizationView(
        policy=policy,
        sequence=tuple(sequence),
        ambiguous_groups=tuple(ambiguous_groups),
    )


# ---------------------------------------------------------------------------
# Bounded local restitch (specialist split)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitResult:
    """Outcome of a bounded specialist split.

    ``neighborhood`` is exactly the set of nodes whose incident edges
    were reconsidered; every other node and edge is preserved
    byte-identically in ``graph``.
    """

    graph: ReadingOrderGraph
    neighborhood: frozenset[str]
    rewritten_edge_count: int
    preserved_edge_count: int


def split_node(
    graph: ReadingOrderGraph,
    node_id: str,
    children: Any,
    child_order: Any = None,
    producer: str = "specialist",
) -> SplitResult:
    """Replace one node with specialist children, restitching locally.

    The neighborhood is the split node, its containment parent, its
    containment children, and every node sharing a before/continues/
    member_of edge with it. Reconnection is sound, not fabricated:

    * external ``X before node`` becomes ``X before each child`` (the
      children partition the region, so the external constraint
      transfers to every part);
    * ``node before Y`` becomes ``each child before Y`` for the same
      reason;
    * internal child order comes only from the specialist's declared
      ``child_order`` — absent evidence leaves the children unordered;
    * splitting a node that itself contains children is a conservative
      abstain (explicit re-parenting required);
    * if the result would contradict existing evidence, validation
      raises :class:`OrderConflictError` and the original graph stands.
    """
    target = graph.node(node_id)
    if target.kind != NODE_KIND_CONTENT:
        raise KernelError(
            f"only content nodes can be split by a specialist, {node_id!r} is a region"
        )

    child_nodes: list[OrderNode] = []
    seen_child_ids: set[str] = set()
    for child in children:
        if not isinstance(child, OrderNode):
            raise KernelError(f"children must be OrderNode, got {type(child).__name__}")
        if child.kind != NODE_KIND_CONTENT:
            raise KernelError(
                "replacement children must be content nodes; regions are containers, "
                "not specialist segments"
            )
        if child.node_id in seen_child_ids or child.node_id in {
            node.node_id for node in graph.nodes
        }:
            raise KernelError(f"child id {child.node_id!r} collides with an existing node")
        seen_child_ids.add(child.node_id)
        child_nodes.append(child)
    if not child_nodes:
        raise KernelError("a split requires at least one replacement child")

    if child_order is not None:
        child_order = list(child_order)
        if sorted(child_order) != sorted(seen_child_ids):
            raise KernelError(
                "child_order must be a permutation of the replacement child ids"
            )

    old_children = graph.children_of(node_id)
    if old_children:
        raise KernelError(
            f"node {node_id!r} itself contains {len(old_children)} children; "
            "re-splitting a container requires explicit re-parenting (conservative "
            "abstain rather than guessing a disposition)"
        )

    parent_edges = graph.parents_of(node_id)
    region_edges = graph.regions_of(node_id)
    incident: list[OrderEdge] = []
    for edge in graph.edges:
        if edge.kind in _ORDERING_EDGE_KINDS and node_id in (edge.source_id, edge.target_id):
            incident.append(edge)

    neighborhood = {node_id} | seen_child_ids
    for edge in parent_edges + region_edges + tuple(incident):
        neighborhood.add(edge.source_id)
        neighborhood.add(edge.target_id)

    dropped = tuple(parent_edges) + tuple(region_edges) + tuple(incident)
    confidence = order_confidence("1.0")
    new_edges: list[OrderEdge] = [edge for edge in graph.edges if edge not in dropped]
    preserved = len(new_edges)

    for parent_edge in parent_edges:
        for child in child_nodes:
            new_edges.append(
                OrderEdge(
                    kind=ORDER_EDGE_CONTAINS,
                    source_id=parent_edge.source_id,
                    target_id=child.node_id,
                    producer=producer,
                    confidence=confidence,
                    state=parent_edge.state,
                )
            )
    for region_edge in region_edges:
        for child in child_nodes:
            new_edges.append(
                OrderEdge(
                    kind=ORDER_EDGE_MEMBER_OF,
                    source_id=child.node_id,
                    target_id=region_edge.target_id,
                    producer=producer,
                    confidence=confidence,
                    state=region_edge.state,
                )
            )
    for edge in incident:
        # Transfer the external constraint to every child, preserving
        # the original evidence state; the split producer owns the
        # re-derived edge lineage.
        for child in child_nodes:
            new_edges.append(
                OrderEdge(
                    kind=edge.kind,
                    source_id=edge.source_id if edge.source_id != node_id else child.node_id,
                    target_id=edge.target_id if edge.target_id != node_id else child.node_id,
                    producer=producer,
                    confidence=edge.confidence,
                    state=edge.state,
                )
            )
    if child_order is not None:
        for before, after in zip(child_order, child_order[1:]):
            new_edges.append(
                OrderEdge(
                    kind=ORDER_EDGE_BEFORE,
                    source_id=before,
                    target_id=after,
                    producer=producer,
                    confidence=confidence,
                    state=EVIDENCE_STATE_ASSERTED,
                )
            )

    new_nodes = [node for node in graph.nodes if node.node_id != node_id] + child_nodes
    rebuilt = ReadingOrderGraph.build(new_nodes, new_edges)

    return SplitResult(
        graph=rebuilt,
        neighborhood=frozenset(neighborhood),
        rewritten_edge_count=len(new_edges) - preserved,
        preserved_edge_count=preserved,
    )


# ---------------------------------------------------------------------------
# Durable record wrapper
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class ReadingOrderRecord(KernelRecord):
    """One committed reading-order graph bound to a content revision.

    Identity covers the revision and the canonical graph payload; the
    producer block is evidence-only, so re-deriving the same graph from
    the same durable facts converges to one record.
    """

    record_class: ClassVar[str] = "reading_order"
    record_type: ClassVar[str] = RECORD_TYPE_READING_ORDER
    schema_version: ClassVar[str] = READING_ORDER_SCHEMA_VERSION

    content_revision_ref: str
    graph: ReadingOrderGraph
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.content_revision_ref, field_name="content_revision_ref")
        if not isinstance(self.graph, ReadingOrderGraph):
            raise KernelError(
                f"graph must be a ReadingOrderGraph, got {type(self.graph).__name__}"
            )
        if not isinstance(self.evidence, Mapping):
            raise KernelError(f"evidence must be a mapping, got {self.evidence!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "content_revision_ref": self.content_revision_ref,
            "graph": self.graph.canonical_payload(),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> ReadingOrderRecord:
        if not isinstance(payload, Mapping):
            raise KernelError(f"reading-order payload must be a mapping, got {payload!r}")
        allowed = {"content_revision_ref", "graph"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown reading-order payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            content_revision_ref=payload["content_revision_ref"],
            graph=ReadingOrderGraph.from_payload(payload["graph"]),
        )
