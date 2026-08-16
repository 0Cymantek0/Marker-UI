"""Incremental rebuild with a clean-rebuild oracle (V3.2 PR73 workstream D).

When the authoritative source advances (a new content revision), the
derived view must be rebuilt. Two paths exist:

* **clean** — re-derive every node from the new source facts, then
  replay the accepted patch chain (the standing oracle,
  :func:`app.kernel.patching.clean_rebuild_view`, replays committed
  history and hard-fails on divergence);
* **incremental** — reuse the source facts the current lineage last
  declared for nodes whose exact dependencies did not change, re-derive
  only the invalidated scope, and submit through the SAME verified
  rebase path. Carrying is always from the last *declared source*
  (genesis texts or the last rebase's source facts) — never from the
  patched view — so the next rebase declares real source values and
  replay re-applies repairs uniformly instead of meeting its own
  effects.

Both paths converge on one authority: the rebase proposal commits pure
new-source facts plus the accepted patch set, the commit transaction
replays it to verify the resulting revision, and the randomized
equivalence tests prove the declared outputs identical. Incrementality
is a derivation-layer optimization only — it never changes what is
declared or how it is verified.

Honest widening: when invalidation widens (conservative/unknown
knowledge) or the structural delta escapes the invalidated scope, the
incremental path falls back to full re-derivation rather than guessing.
Change pruning (Bazel-style resurrection) was evaluated and deliberately
declined for this slice — see the evidence bundle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitReceipt, KernelCommitService
from app.kernel.dependencies import (
    DependencyDeclarationRecord,
    InvalidationResult,
    compute_invalidation,
)
from app.kernel.errors import KernelError
from app.kernel.models import KernelRecord
from app.kernel.patches import (
    DEFAULT_VIEW_ID,
    OP_TYPE_REBASE_SOURCE,
    PatchOperation,
    PatchOutcomeRecord,
    PatchPreconditions,
    PatchProposalRecord,
    ViewAdvancement,
    ViewDocumentRecord,
    apply_rebase_source,
)
from app.kernel.patching import (
    ViewRevision,
    _scoped_record_id,
    load_view_history,
    read_current_view,
)
from app.kernel.reading_order import NODE_KIND_CONTENT, ReadingOrderGraph
from app.kernel.records import (
    EDGE_KIND_DEPENDS_ON,
    EDGE_KIND_DERIVED_FROM,
    EDGE_KIND_EVIDENCE_FOR,
    KernelEdge,
)

__all__ = [
    "RebaseAcceptance",
    "RebuildReport",
    "incremental_rebuild",
    "submit_rebase",
]

#: Tracer convention linking dependency-declaration subjects to view
#: nodes: subject ``derived:<node_id>`` declares the derived value of
#: ``<node_id>``. Subjects outside this mapping (document-level
#: summaries, recall hints) widen the rebuild to full derivation.
DERIVED_SUBJECT_PREFIX = "derived:"


@dataclass(frozen=True)
class RebaseAcceptance:
    """Result of one accepted source rebase."""

    receipt: KernelCommitReceipt
    proposal_id: str
    previous: ViewRevision
    result: ViewRevision
    applied_refs: tuple[str, ...]
    dropped_refs: tuple[tuple[str, str], ...]


async def submit_rebase(
    session_factory: async_sessionmaker,
    service: KernelCommitService,
    *,
    workspace_id: str,
    rebase_operation: PatchOperation,
    producer: Mapping[str, Any] | None = None,
    view_id: str = DEFAULT_VIEW_ID,
    _inject_fault_at: str | None = None,
) -> RebaseAcceptance:
    """Submit a source rebase: new source facts + preconditioned replay.

    The operation names the pure new-source graph/texts and the accepted
    patch proposals to replay. Survivors apply because their
    before-value claims still hold; the rest drop with a typed reason
    (recorded in the outcome) and must be re-proposed by a human
    decision — a source change never silently re-targets a patch.
    """
    if rebase_operation.op_type != OP_TYPE_REBASE_SOURCE:
        raise KernelError(f"expected a rebase_source operation, got {rebase_operation.op_type!r}")
    current = await read_current_view(session_factory, workspace_id, view_id=view_id)
    if current is None:
        raise KernelError(
            f"workspace={workspace_id!r}: a rebase requires an initialized view"
        )

    replay: dict[str, PatchProposalRecord] = {}
    async with session_factory() as session:
        for ref in rebase_operation.params["replay_proposal_refs"]:
            row = (
                await session.execute(
                    select(KernelRecord.payload_json).where(
                        KernelRecord.id == ref,
                        KernelRecord.workspace_id == workspace_id,
                        KernelRecord.record_class == "patch_proposal",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise KernelError(
                    f"rebase references proposal {ref!r} which is not committed "
                    f"in workspace {workspace_id!r}"
                )
            replay[ref] = PatchProposalRecord.from_payload(json.loads(row), record_id=ref)
    replayed = apply_rebase_source(rebase_operation, replay)

    proposal = PatchProposalRecord(
        record_id=_scoped_record_id(workspace_id, f"proposal-rebase-{replayed.view.content_revision_ref}"),
        preconditions=PatchPreconditions(
            base_revision_id=current.revision_id,
            required_source_revision_refs=(current.view.content_revision_ref,),
        ),
        operations=(rebase_operation,),
        producer=dict(producer or {}),
    )
    outcome = PatchOutcomeRecord(
        record_id=_scoped_record_id(workspace_id, f"outcome-rebase-{replayed.view.content_revision_ref}"),
        proposal_identity=proposal.proposal_id(),
        outcome="accepted",
        observed={
            "view_id": view_id,
            "base_revision_id": current.revision_id,
            "source_revision_from": current.view.content_revision_ref,
            "source_revision_to": replayed.view.content_revision_ref,
            "applied_proposal_refs": list(replayed.applied_refs),
            "dropped_proposal_refs": [
                {"ref": ref, "reason": reason} for ref, reason in replayed.dropped_refs
            ],
        },
        resulting_revision_id=replayed.view.view_revision_id(),
    )
    result_view = ViewDocumentRecord(
        record_id=_scoped_record_id(workspace_id, f"view-rebase-{replayed.view.content_revision_ref}"),
        content_revision_ref=replayed.view.content_revision_ref,
        graph=replayed.view.graph,
        texts=dict(replayed.view.texts),
    )
    if result_view.view_revision_id() != replayed.view.view_revision_id():
        raise KernelError("rebase result identity mismatch on reconstruction")
    edges = (
        KernelEdge(
            edge_kind=EDGE_KIND_DEPENDS_ON,
            source_ref=proposal.record_id,
            target_ref=current.record_id,
        ),
        KernelEdge(
            edge_kind=EDGE_KIND_DERIVED_FROM,
            source_ref=result_view.record_id,
            target_ref=current.record_id,
        ),
        KernelEdge(
            edge_kind=EDGE_KIND_EVIDENCE_FOR,
            source_ref=outcome.record_id,
            target_ref=result_view.record_id,
        ),
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(proposal, outcome, result_view),
            edges=edges,
            producer={"operation": "view.rebase", **(producer or {})},
            view_advancement=ViewAdvancement(
                new_revision_id=result_view.view_revision_id(),
                view_id=view_id,
                base_revision_id=current.revision_id,
                proposal_record_id=proposal.record_id,
            ),
        ),
        _inject_fault_at=_inject_fault_at,
    )
    return RebaseAcceptance(
        receipt=receipt,
        proposal_id=proposal.proposal_id(),
        previous=current,
        result=ViewRevision(
            view=result_view,
            revision_id=result_view.view_revision_id(),
            record_id=result_view.record_id,
            kernel_commit_id=receipt.kernel_commit_id,
        ),
        applied_refs=replayed.applied_refs,
        dropped_refs=replayed.dropped_refs,
    )


@dataclass(frozen=True)
class RebuildReport:
    """What the incremental path actually did, and why."""

    mode: str  # "localized" | "full"
    invalidation: InvalidationResult
    derived_node_ids: tuple[str, ...]
    carried_node_ids: tuple[str, ...]
    applied_refs: tuple[str, ...]
    dropped_refs: tuple[tuple[str, str], ...]


async def _accepted_view_patch_refs(
    session_factory: async_sessionmaker, workspace_id: str
) -> tuple[str, ...]:
    """Record ids of accepted view patches (excluding rebases), in order."""
    history = await load_view_history(session_factory, workspace_id)
    refs: list[str] = []
    for entry in history:
        if entry.proposal is None or entry.proposal_record_id is None:
            continue
        if any(op.op_type == OP_TYPE_REBASE_SOURCE for op in entry.proposal.operations):
            continue
        refs.append(entry.proposal_record_id)
    return tuple(refs)


async def _declared_source_texts(
    session_factory: async_sessionmaker, workspace_id: str
) -> dict[str, str]:
    """The pure source facts the current lineage last declared.

    Genesis view texts, or the source_texts of the last accepted
    rebase — value patches are NOT part of source truth. Carrying from
    here (never from the patched view) is what keeps an incremental
    rebuild honest: the next rebase declares real source values, and
    replay re-applies repairs uniformly instead of meeting its own
    effects.
    """
    history = await load_view_history(session_factory, workspace_id)
    if not history:
        raise KernelError(f"workspace={workspace_id!r}: no view lineage to carry from")
    for entry in reversed(history):
        if entry.proposal is None:
            continue
        for op in entry.proposal.operations:
            if op.op_type == OP_TYPE_REBASE_SOURCE:
                return dict(op.params["source_texts"])
    # No rebase in the lineage: the genesis view's texts are the
    # declared source facts.
    return dict(history[0].view.texts)


def _localized_texts(
    last_source_texts: Mapping[str, str],
    new_graph: ReadingOrderGraph,
    derive: Callable[[str], str],
    changed_nodes: frozenset[str],
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]] | None:
    """Carry unchanged source-declared values; derive only changed nodes.

    Returns ``None`` when the structural delta escapes the changed
    scope (nodes added/removed outside it) — the caller must then fall
    back to full derivation. Structural changes inside the changed
    scope are fine: those nodes are re-derived from the new source.
    """
    new_ids = {
        node.node_id for node in new_graph.nodes if node.kind == NODE_KIND_CONTENT
    }
    carried_ids_source = set(last_source_texts)
    if (new_ids ^ carried_ids_source) - changed_nodes:
        return None
    texts: dict[str, str] = {}
    derived: list[str] = []
    carried: list[str] = []
    for node_id in sorted(new_ids):
        if node_id in changed_nodes or node_id not in carried_ids_source:
            texts[node_id] = derive(node_id)
            derived.append(node_id)
        else:
            texts[node_id] = last_source_texts[node_id]
            carried.append(node_id)
    return texts, tuple(derived), tuple(carried)


async def incremental_rebuild(
    session_factory: async_sessionmaker,
    service: KernelCommitService,
    *,
    workspace_id: str,
    new_content_revision_ref: str,
    new_graph: ReadingOrderGraph,
    changed_input_refs: Sequence[str],
    declarations: Sequence[DependencyDeclarationRecord],
    derive: Callable[[str], str],
    producer: Mapping[str, Any] | None = None,
    view_id: str = DEFAULT_VIEW_ID,
) -> tuple[RebaseAcceptance, RebuildReport]:
    """Rebuild the view against a new source revision, incrementally.

    ``derive(node_id)`` produces the new-source derived value for one
    node (the unit of recomputable work). The invalidation result over
    the caller-observed changed inputs and the committed dependency
    declarations decides the scope: exact knowledge localizes
    derivation to the invalidated nodes; widening or an unlocalizable
    structural delta falls back to deriving every node.
    """
    current = await read_current_view(session_factory, workspace_id, view_id=view_id)
    if current is None:
        raise KernelError(f"workspace={workspace_id!r}: no view to rebuild")

    invalidation = compute_invalidation(changed_input_refs, declarations)
    changed_nodes = frozenset(
        subject[len(DERIVED_SUBJECT_PREFIX):]
        for subject in invalidation.invalidated
        if subject.startswith(DERIVED_SUBJECT_PREFIX)
    )
    unlocalizable = any(
        not subject.startswith(DERIVED_SUBJECT_PREFIX)
        for subject in invalidation.invalidated
    )

    if invalidation.widened or invalidation.uncovered_changes or unlocalizable:
        # Conservative/unknown knowledge or document-level subjects: the
        # safe scope is the whole document. An uncovered change is a
        # change no exact knowledge explains — carrying any value past it
        # would pretend a narrower dependency graph than declared, so
        # everything is re-derived. Derive everything.
        mode = "full"
        texts = {
            node.node_id: derive(node.node_id)
            for node in new_graph.nodes
            if node.kind == NODE_KIND_CONTENT
        }
        derived_ids = tuple(sorted(texts))
        carried_ids: tuple[str, ...] = ()
    else:
        last_source = await _declared_source_texts(session_factory, workspace_id)
        localized = _localized_texts(last_source, new_graph, derive, changed_nodes)
        if localized is None:
            mode = "full"
            texts = {
                node.node_id: derive(node.node_id)
                for node in new_graph.nodes
                if node.kind == NODE_KIND_CONTENT
            }
            derived_ids = tuple(sorted(texts))
            carried_ids = ()
        else:
            texts, derived_ids, carried_ids = localized
            mode = "localized"

    _validate_texts_cover(new_graph, texts)
    replay_refs = await _accepted_view_patch_refs(session_factory, workspace_id)
    operation = PatchOperation.rebase_source(
        new_content_revision_ref=new_content_revision_ref,
        source_graph=new_graph,
        source_texts=texts,
        replay_proposal_refs=replay_refs,
    )
    acceptance = await submit_rebase(
        session_factory,
        service,
        workspace_id=workspace_id,
        rebase_operation=operation,
        producer=producer,
        view_id=view_id,
    )
    report = RebuildReport(
        mode=mode,
        invalidation=invalidation,
        derived_node_ids=derived_ids,
        carried_node_ids=carried_ids,
        applied_refs=acceptance.applied_refs,
        dropped_refs=acceptance.dropped_refs,
    )
    return acceptance, report


def _validate_texts_cover(graph: ReadingOrderGraph, texts: Mapping[str, str]) -> None:
    content = {
        node.node_id for node in graph.nodes if node.kind == "content"
    }
    if set(texts) != content:
        raise KernelError(
            "derived texts must cover exactly the content nodes of the new graph"
        )
