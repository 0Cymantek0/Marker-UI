"""Patch application service & view lineage (V3.2 PR73).

Thin orchestration over the single commit authority: building one
all-or-conflict batch per patch (proposal + outcome + resulting view
revision + lineage edges + conditional head movement), reading the
current view revision from the durable head, enumerating revision
lineage, and replaying committed history as the independent clean
rebuild.

Everything durable still goes through :class:`KernelCommitService` —
this module never writes outside a kernel commit, and every
precondition is re-verified inside the commit transaction regardless of
the advisory pre-checks here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitReceipt, KernelCommitService
from app.kernel.errors import (
    InvalidViewAdvancementError,
    KernelError,
    StaleBaseRevisionError,
)
from app.kernel.models import KernelRecord, KernelViewHead
from app.kernel.patches import (
    DEFAULT_VIEW_ID,
    OP_TYPE_REBASE_SOURCE,
    PatchOutcomeRecord,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    ViewAdvancement,
    ViewDocumentRecord,
    apply_operation,
    apply_rebase_source,
    evaluate_preconditions,
    view_text_hash,
)
from app.kernel.reading_order import ReadingOrderGraph
from app.utils.canonical import payload_byte_hash
from app.kernel.records import (
    EDGE_KIND_DEPENDS_ON,
    EDGE_KIND_DERIVED_FROM,
    EDGE_KIND_EVIDENCE_FOR,
    KernelEdge,
)

__all__ = [
    "PatchAcceptance",
    "ViewHistoryEntry",
    "ViewRevision",
    "clean_rebuild_view",
    "initialize_view",
    "load_view_history",
    "read_current_view",
    "rebase_proposal",
    "submit_patch",
]


@dataclass(frozen=True)
class ViewRevision:
    """The current (or a historical) view revision as durable state."""

    view: ViewDocumentRecord
    revision_id: str
    record_id: str
    kernel_commit_id: int


@dataclass(frozen=True)
class PatchAcceptance:
    """Result of one accepted patch submission."""

    receipt: KernelCommitReceipt
    proposal_id: str
    previous: ViewRevision
    result: ViewRevision


def _scoped_record_id(workspace_id: str, name: str) -> str:
    """Deterministic record id unique within one database.

    ``kernel_records.id`` is a global primary key, so ids minted by this
    service carry the workspace; long workspace ids are shortened under
    a digest prefix so the result stays within the 128-char grammar.
    """
    if len(workspace_id) > 64:
        digest = payload_byte_hash(workspace_id.encode("utf-8")).split(":")[1][:8]
        workspace_id = workspace_id[:55] + "-" + digest
    scoped = f"{name}-{workspace_id}"
    return scoped[:128]


async def read_current_view(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    view_id: str = DEFAULT_VIEW_ID,
) -> ViewRevision | None:
    """Resolve the current view revision from the durable head.

    The head is only ever written inside commit transactions, so this
    read is the committed-cut truth — never a timestamp guess.
    """
    async with session_factory() as session:
        head = (
            await session.execute(
                select(KernelViewHead).where(
                    KernelViewHead.workspace_id == workspace_id,
                    KernelViewHead.view_id == view_id,
                )
            )
        ).scalar_one_or_none()
        if head is None:
            return None
        row = (
            await session.execute(
                select(
                    KernelRecord.id, KernelRecord.payload_json, KernelRecord.kernel_commit_id
                ).where(
                    KernelRecord.workspace_id == workspace_id,
                    KernelRecord.identity_hash == head.current_revision_id,
                    KernelRecord.record_class == "view_document",
                )
            )
        ).one()
    view = ViewDocumentRecord.from_payload(json.loads(row.payload_json), record_id=row.id)
    return ViewRevision(
        view=view,
        revision_id=head.current_revision_id,
        record_id=row.id,
        kernel_commit_id=head.kernel_commit_id,
    )


async def initialize_view(
    session_factory: async_sessionmaker,
    service: KernelCommitService,
    *,
    workspace_id: str,
    content_revision_ref: str,
    graph: ReadingOrderGraph,
    texts: Mapping[str, str],
    content_revision_record_ref: str | None = None,
    producer: Mapping[str, Any] | None = None,
    view_id: str = DEFAULT_VIEW_ID,
) -> ViewRevision:
    """Commit the genesis view revision for a workspace.

    The view is derived truth: its content revision binding and its
    node texts come from authoritative source facts the caller already
    extracted (PR72 native tracers), never from this module.
    """
    view = ViewDocumentRecord(
        record_id=_scoped_record_id(workspace_id, f"view-{view_id}-genesis"),
        content_revision_ref=content_revision_ref,
        graph=graph,
        texts=dict(texts),
        evidence=dict(producer or {}),
    )
    edges: tuple[KernelEdge, ...] = ()
    if content_revision_record_ref is not None:
        edges = (
            KernelEdge(
                edge_kind=EDGE_KIND_DERIVED_FROM,
                source_ref=view.record_id,
                target_ref=content_revision_record_ref,
            ),
        )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(view,),
            edges=edges,
            producer={"operation": "view.genesis", **(producer or {})},
            view_advancement=ViewAdvancement(
                new_revision_id=view.view_revision_id(),
                view_id=view_id,
            ),
        )
    )
    return ViewRevision(
        view=view,
        revision_id=view.view_revision_id(),
        record_id=view.record_id,
        kernel_commit_id=receipt.kernel_commit_id,
    )


def _build_next_view(
    workspace_id: str, current: ViewRevision, proposal: PatchProposalRecord
) -> ViewDocumentRecord:
    graph, texts = current.view.graph, dict(current.view.texts)
    for op in proposal.operations:
        graph, texts = apply_operation(graph, texts, op)
    return ViewDocumentRecord(
        record_id=_scoped_record_id(workspace_id, f"view-{proposal.record_id}-result"),
        content_revision_ref=current.view.content_revision_ref,
        graph=graph,
        texts=texts,
    )


async def submit_patch(
    session_factory: async_sessionmaker,
    service: KernelCommitService,
    *,
    workspace_id: str,
    proposal: PatchProposalRecord,
    producer: Mapping[str, Any] | None = None,
    view_id: str = DEFAULT_VIEW_ID,
) -> PatchAcceptance:
    """Submit one conditional patch; accepted or typed-conflict, atomically.

    Advisory pre-checks run against the currently readable revision so
    callers get the typed conflict without a commit round-trip; the
    commit transaction re-evaluates every precondition against locked
    current state, so a race between the two can never let a stale
    patch through.
    """
    current = await read_current_view(session_factory, workspace_id, view_id=view_id)
    if current is None:
        raise StaleBaseRevisionError(
            expected_base_revision_id=proposal.preconditions.base_revision_id,
            observed_base_revision_id=None,
        )
    # Advisory only — the authoritative evaluation is in-transaction.
    if (
        proposal.preconditions.base_revision_id is not None
        and proposal.preconditions.base_revision_id != current.revision_id
    ):
        raise StaleBaseRevisionError(
            expected_base_revision_id=proposal.preconditions.base_revision_id,
            observed_base_revision_id=current.revision_id,
        )
    evaluate_preconditions(current.view, proposal.preconditions)
    next_view = _build_next_view(workspace_id, current, proposal)
    outcome = PatchOutcomeRecord(
        record_id=_scoped_record_id(workspace_id, f"outcome-{proposal.record_id}"),
        proposal_identity=proposal.proposal_id(),
        outcome="accepted",
        observed={
            "view_id": view_id,
            "base_revision_id": current.revision_id,
            "source_revision": current.view.content_revision_ref,
        },
        resulting_revision_id=next_view.view_revision_id(),
    )
    edges = (
        KernelEdge(
            edge_kind=EDGE_KIND_DEPENDS_ON,
            source_ref=proposal.record_id,
            target_ref=current.record_id,
        ),
        KernelEdge(
            edge_kind=EDGE_KIND_DERIVED_FROM,
            source_ref=next_view.record_id,
            target_ref=current.record_id,
        ),
        KernelEdge(
            edge_kind=EDGE_KIND_EVIDENCE_FOR,
            source_ref=outcome.record_id,
            target_ref=next_view.record_id,
        ),
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(proposal, outcome, next_view),
            edges=edges,
            producer={"operation": "view.patch", **(producer or {})},
            view_advancement=ViewAdvancement(
                new_revision_id=next_view.view_revision_id(),
                view_id=view_id,
                base_revision_id=current.revision_id,
                proposal_record_id=proposal.record_id,
            ),
        )
    )
    return PatchAcceptance(
        receipt=receipt,
        proposal_id=proposal.proposal_id(),
        previous=current,
        result=ViewRevision(
            view=next_view,
            revision_id=next_view.view_revision_id(),
            record_id=next_view.record_id,
            kernel_commit_id=receipt.kernel_commit_id,
        ),
    )


def rebase_proposal(
    proposal: PatchProposalRecord,
    current: ViewRevision,
    *,
    record_id: str,
    allow_value_clobber: bool = False,
) -> PatchProposalRecord | None:
    """Explicitly re-target a conflicted proposal at the current revision.

    The tested rebase rule, deliberately conservative: the operations are
    preserved verbatim, the base moves to the current revision, and every
    before-value check is re-derived from the current view. Rebase is
    impossible (returns ``None``) when a target node no longer exists,
    when a target's value changed and the caller has not explicitly
    accepted overwriting it (``allow_value_clobber``), or when the
    current view is bound to a source revision the proposal does not
    accept. Nothing about arrival order silently decides document truth:
    the loser only lands after a revalidation under these declared rules.
    """
    checks: list[TargetCheck] = []
    for check in proposal.preconditions.target_checks:
        try:
            text = current.view.text_of(check.node_id)
        except KernelError:
            return None  # target gone (e.g. consumed by a split)
        current_hash = view_text_hash(text)
        if current_hash != check.before_hash and not allow_value_clobber:
            return None  # intent no longer holds; clobbering requires an explicit choice
        checks.append(TargetCheck(node_id=check.node_id, before_hash=current_hash))
    source_refs = proposal.preconditions.required_source_revision_refs
    if source_refs and current.view.content_revision_ref not in tuple(source_refs):
        return None  # authored under a source this view no longer binds
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=current.revision_id,
            target_checks=tuple(checks),
            required_source_revision_refs=tuple(source_refs),
        ),
        operations=proposal.operations,
        producer=dict(proposal.producer),
    )


@dataclass(frozen=True)
class ViewHistoryEntry:
    """One commit in the view lineage: its revision and its proposal."""

    kernel_commit_id: int
    view: ViewDocumentRecord
    view_record_id: str
    proposal: PatchProposalRecord | None
    proposal_record_id: str | None
    outcome: PatchOutcomeRecord | None


async def load_view_history(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    view_id: str = DEFAULT_VIEW_ID,
    upto_commit: int | None = None,
) -> list[ViewHistoryEntry]:
    """Enumerate the committed view lineage in causal order.

    One entry per commit that carries a view_document record; the
    proposal/outcome of that same commit are attached. ``upto_commit``
    bounds the enumeration for oracle replay.
    """
    del view_id  # lineage is workspace-scoped; heads are view-scoped
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    KernelRecord.id,
                    KernelRecord.kernel_commit_id,
                    KernelRecord.record_class,
                    KernelRecord.record_type,
                    KernelRecord.payload_json,
                )
                .where(
                    KernelRecord.workspace_id == workspace_id,
                    KernelRecord.record_class.in_(
                        ("view_document", "patch_proposal", "patch_outcome")
                    ),
                )
                .order_by(KernelRecord.kernel_commit_id.asc(), KernelRecord.id.asc())
            )
        ).all()
    if upto_commit is not None:
        rows = [row for row in rows if row.kernel_commit_id <= upto_commit]

    by_commit: dict[int, dict[str, Any]] = {}
    for row in rows:
        slot = by_commit.setdefault(row.kernel_commit_id, {})
        if row.record_class == "view_document":
            slot["view"] = ViewDocumentRecord.from_payload(
                json.loads(row.payload_json), record_id=row.id
            )
            slot["view_record_id"] = row.id
        elif row.record_class == "patch_proposal":
            slot["proposal"] = PatchProposalRecord.from_payload(
                json.loads(row.payload_json), record_id=row.id
            )
            slot["proposal_record_id"] = row.id
        else:
            slot["outcome"] = PatchOutcomeRecord.from_payload(
                json.loads(row.payload_json), record_id=row.id
            )

    history: list[ViewHistoryEntry] = []
    for commit_id in sorted(by_commit):
        slot = by_commit[commit_id]
        if "view" not in slot:
            # A proposal/outcome without its view record in the same
            # commit is not part of the advancement lineage this slice
            # writes; skip rather than fabricate a revision entry.
            continue
        history.append(
            ViewHistoryEntry(
                kernel_commit_id=commit_id,
                view=slot["view"],
                view_record_id=slot["view_record_id"],
                proposal=slot.get("proposal"),
                proposal_record_id=slot.get("proposal_record_id"),
                outcome=slot.get("outcome"),
            )
        )
    return history


async def clean_rebuild_view(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    view_id: str = DEFAULT_VIEW_ID,
    upto_commit: int | None = None,
) -> ViewDocumentRecord:
    """Independently rebuild the declared view from committed truth.

    The oracle path: start at the committed genesis revision and replay
    every committed proposal in causal order — view patches by applying
    their operations, rebase proposals by replaying their declared
    source facts and proposal set. Every step's recomputed revision
    must equal the revision that commit actually recorded, so any
    divergence between incremental maintenance and clean history
    surfaces here as a hard error.
    """
    history = await load_view_history(
        session_factory, workspace_id, view_id=view_id, upto_commit=upto_commit
    )
    if not history:
        raise KernelError(f"workspace={workspace_id!r}: no view lineage to rebuild")

    current = history[0].view
    proposals = {
        entry.proposal_record_id: entry.proposal
        for entry in history
        if entry.proposal is not None and entry.proposal_record_id is not None
    }
    for entry in history[1:]:
        proposal = entry.proposal
        if proposal is None:
            # A view record without a proposal in its commit cannot be
            # replayed from inputs; refuse to trust it.
            raise InvalidViewAdvancementError(
                f"commit {entry.kernel_commit_id} advanced the view without a "
                "replayable proposal; clean rebuild refuses to fabricate the step"
            )
        rebase_ops = [
            op for op in proposal.operations if op.op_type == OP_TYPE_REBASE_SOURCE
        ]
        if rebase_ops:
            replayed = apply_rebase_source(rebase_ops[0], proposals).view
            if replayed.view_revision_id() != entry.view.view_revision_id():
                raise InvalidViewAdvancementError(
                    f"commit {entry.kernel_commit_id}: replayed rebase revision "
                    f"{replayed.view_revision_id()} disagrees with the committed "
                    f"revision {entry.view.view_revision_id()}"
                )
            current = replayed
            continue
        evaluate_preconditions(current, proposal.preconditions)
        graph, texts = current.graph, dict(current.texts)
        for op in proposal.operations:
            graph, texts = apply_operation(graph, texts, op)
        current = ViewDocumentRecord(
            record_id=f"rebuild-{entry.kernel_commit_id}",
            content_revision_ref=current.content_revision_ref,
            graph=graph,
            texts=texts,
        )
        if current.view_revision_id() != entry.view.view_revision_id():
            raise InvalidViewAdvancementError(
                f"commit {entry.kernel_commit_id}: replayed revision "
                f"{current.view_revision_id()} disagrees with the committed "
                f"revision {entry.view.view_revision_id()}; incremental state "
                "diverged from clean history"
            )
    return current
