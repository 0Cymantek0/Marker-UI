"""Adversarial incremental-rebuild and revision-propagation eval (PR82A).

Extends PR73's randomized clean-vs-incremental oracle with longer mixed
change sequences (4-10 ops) that interleave:

* accepted text patches;
* conflicting patches (stale before-hash must be rejected, not merged);
* structural node insertion / deletion between revisions;
* source revision advance with patch replay (rebase);
* exact vs conservative dependency declarations (unknown scope must
  widen, never narrow);
* anchor mapping records computed and committed alongside each rebase,
  so deterministic and stale mappings participate in the same history
  the oracles replay.

Every scenario asserts BOTH independent oracles from PR73: the
committed-history replay and a full-derivation pure replay must equal
the incremental result. Answers preregistered Q3.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.kernel.anchor_mapping import SourceAnchorMappingRecord, map_anchor
from app.kernel.anchors import SourceAnchorRecord, TextQuoteSelector
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.dependencies import (
    COMPLETENESS_CONSERVATIVE_SCOPE,
    COMPLETENESS_EXACT_NATIVE,
    DependencyDeclarationRecord,
    DependencyInput,
)
from app.kernel.errors import KernelError
from app.kernel.patches import (
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    ViewDocumentRecord,
    apply_operation,
    apply_rebase_source,
    view_text_hash,
)
from app.kernel.patching import (
    clean_rebuild_view,
    initialize_view,
    load_view_history,
    read_current_view,
    submit_patch,
)
from app.kernel.reading_order import OrderEdge, OrderNode, ReadingOrderGraph, order_confidence
from app.kernel.rebuild import incremental_rebuild

CONF = order_confidence("1.0")

#: Frozen seed set for the release runner; every seed is reproducible.
DEFAULT_SEEDS: tuple[int, ...] = tuple(range(24))


def make_graph(node_ids: Sequence[str]) -> ReadingOrderGraph:
    nodes = [OrderNode(node_id=nid, anchor_ref=f"anchor-{nid}") for nid in node_ids]
    edges = [
        OrderEdge(kind="before", source_id=a, target_id=b, producer="pr82", confidence=CONF)
        for a, b in zip(node_ids, node_ids[1:])
    ]
    return ReadingOrderGraph.build(nodes, edges)


# ---------------------------------------------------------------------------
# Deterministic mixed-sequence generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchOp:
    node_id: str
    after_text: str


@dataclass(frozen=True)
class ConflictingPatchOp:
    node_id: str
    claimed_before: str
    after_text: str


@dataclass(frozen=True)
class InsertNodeOp:
    node_id: str


@dataclass(frozen=True)
class DeleteNodeOp:
    node_id: str


@dataclass(frozen=True)
class RebaseOp:
    changed_nodes: tuple[str, ...]
    conservative: bool


MixedOp = PatchOp | ConflictingPatchOp | InsertNodeOp | DeleteNodeOp | RebaseOp


def generate_mixed_sequence(seed: int) -> tuple[MixedOp, ...]:
    """Deterministic longer mixed change sequence (pure stdlib random)."""
    rng = random.Random(seed)
    node_count = rng.randint(3, 6)
    node_ids = [f"n{i:02d}" for i in range(node_count)]
    live = list(node_ids)
    ops: list[MixedOp] = []
    next_node = node_count

    for step in range(rng.randint(4, 10)):
        choice = rng.random()
        if choice < 0.30 and live:
            ops.append(PatchOp(rng.choice(live), f"patch-s{step}-{rng.randrange(1000)}"))
        elif choice < 0.40 and live:
            victim = rng.choice(live)
            # Claim a stale before-text: the patch must be rejected.
            ops.append(ConflictingPatchOp(victim, f"stale-{step}", f"conflict-{step}"))
        elif choice < 0.55:
            node_id = f"x{next_node:02d}"
            next_node += 1
            live.append(node_id)
            ops.append(InsertNodeOp(node_id))
        elif choice < 0.65 and len(live) > 2:
            victim = rng.choice(live)
            live.remove(victim)
            ops.append(DeleteNodeOp(victim))
        else:
            changed = rng.sample(live, rng.randint(0, min(2, len(live))))
            ops.append(RebaseOp(tuple(changed), rng.random() < 0.3))
    return tuple(ops)


# ---------------------------------------------------------------------------
# Scenario execution against a real kernel
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    seed: int
    op_count: int
    modes: tuple[str, ...]
    rebases: int
    conflicts_rejected: int
    conflicts_skipped: int = 0
    mapping_dispositions: dict[str, int] = field(default_factory=dict)
    equal_clean_replay: bool = False
    equal_independent_replay: bool = False
    violations: tuple[str, ...] = ()


@dataclass
class IncrementalResult:
    scenarios: tuple[ScenarioResult, ...]
    violations: tuple[str, ...] = ()

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    def summary(self) -> dict[str, Any]:
        mapping_totals: dict[str, int] = {}
        for result in self.scenarios:
            for disposition, count in result.mapping_dispositions.items():
                mapping_totals[disposition] = mapping_totals.get(disposition, 0) + count
        return {
            "scenarios": len(self.scenarios),
            "seeds": [result.seed for result in self.scenarios],
            "modes": sorted({mode for result in self.scenarios for mode in result.modes}),
            "rebases": sum(result.rebases for result in self.scenarios),
            "conflicts_rejected": sum(result.conflicts_rejected for result in self.scenarios),
            "conflicts_skipped": sum(result.conflicts_skipped for result in self.scenarios),
            "mapping_dispositions": dict(sorted(mapping_totals.items())),
            "violations": list(self.violations),
        }


def _declarations(node_ids: Sequence[str], ws: str, conservative: bool):
    declarations = [
        DependencyDeclarationRecord(
            record_id=f"{ws}-decl-{nid}",
            subject_ref=f"derived:{nid}",
            inputs=(DependencyInput(f"fact:{nid}", COMPLETENESS_EXACT_NATIVE),),
            operator="derived.renderer",
            operator_version="1.0.0",
        )
        for nid in node_ids
    ]
    if conservative:
        declarations.append(
            DependencyDeclarationRecord(
                record_id=f"{ws}-decl-doc",
                subject_ref="derived:doc-summary",
                inputs=(DependencyInput("anything:in-document", COMPLETENESS_CONSERVATIVE_SCOPE),),
                scope_ref="document",
                operator="doc.summarizer",
                operator_version="1.0.0",
            )
        )
    return declarations


def _anchor(node_id: str, revision_ref: str, text: str, ws: str) -> SourceAnchorRecord:
    return SourceAnchorRecord(
        record_id=f"{ws}-anchor-{node_id}-{revision_ref}",
        content_revision_ref=revision_ref,
        locator="pdf:page:1",
        selectors={"quote": TextQuoteSelector(quote=text)},
    )


async def run_mixed_scenario(kernel_env, seed: int) -> ScenarioResult:
    """Execute one mixed sequence against a fresh workspace."""
    ops = generate_mixed_sequence(seed)
    # Node count must match the generator's own first draw; text noise
    # uses an independent stream so the two never desynchronize.
    node_count = random.Random(seed).randint(3, 6)
    rng = random.Random(f"{seed}-texts")
    initial_nodes = [f"n{i:02d}" for i in range(node_count)]
    ws = f"ws-pr82-inc-{seed}"
    service = KernelCommitService(kernel_env)

    # View texts track every accepted patch (precondition basis);
    # declared source texts track only the declared source of the last
    # rebase (oracle basis). They diverge exactly when patches exist.
    view_texts = {nid: f"s1-{nid}-{rng.randrange(1000)}" for nid in initial_nodes}
    declared_texts = dict(view_texts)
    live_nodes = list(initial_nodes)
    # The committed view is only ever advanced by a rebase (structural
    # inserts/deletes materialize at the NEXT rebase). The oracle must
    # replay exactly the committed state, so snapshot it per rebase;
    # patches may only target nodes the committed view actually has.
    committed_nodes = tuple(live_nodes)
    committed_declared = dict(declared_texts)
    view_node_ids = set(committed_nodes)
    revision_counter = 1
    genesis = await initialize_view(
        kernel_env,
        service,
        workspace_id=ws,
        content_revision_ref=f"rev-{ws}-1",
        graph=make_graph(live_nodes),
        texts=dict(view_texts),
    )
    current_revision = genesis.revision_id
    patch_refs: list[str] = []
    patch_after: dict[str, tuple[str, str]] = {}
    trailing_patch_refs: list[str] = []
    conflicts_rejected = 0
    conflicts_skipped = 0
    rebases = 0
    modes_seen: set[str] = set()
    mapping_dispositions: dict[str, int] = {}
    violations: list[str] = []

    for index, op in enumerate(ops):
        if isinstance(op, PatchOp):
            if op.node_id not in view_node_ids:
                # Inserted locally but not yet materialized by a rebase:
                # honestly unexecutable this step, not a kernel fault.
                continue
            proposal = PatchProposalRecord(
                record_id=f"{ws}-p{index}",
                preconditions=PatchPreconditions(
                    base_revision_id=current_revision,
                    target_checks=(
                        TargetCheck(
                            node_id=op.node_id,
                            before_hash=view_text_hash(view_texts[op.node_id]),
                        ),
                    ),
                ),
                operations=(
                    PatchOperation.replace_text(node_id=op.node_id, after_text=op.after_text),
                ),
            )
            await submit_patch(kernel_env, service, workspace_id=ws, proposal=proposal)
            current_revision = (await read_current_view(kernel_env, ws)).revision_id
            view_texts[op.node_id] = op.after_text
            patch_refs.append(f"{ws}-p{index}")
            patch_after[f"{ws}-p{index}"] = (op.node_id, op.after_text)
            trailing_patch_refs.append(f"{ws}-p{index}")
        elif isinstance(op, ConflictingPatchOp):
            if op.node_id not in view_node_ids:
                conflicts_skipped += 1
                continue
            proposal = PatchProposalRecord(
                record_id=f"{ws}-c{index}",
                preconditions=PatchPreconditions(
                    base_revision_id=current_revision,
                    target_checks=(
                        TargetCheck(
                            node_id=op.node_id,
                            before_hash=view_text_hash(op.claimed_before),
                        ),
                    ),
                ),
                operations=(
                    PatchOperation.replace_text(node_id=op.node_id, after_text=op.after_text),
                ),
            )
            try:
                await submit_patch(kernel_env, service, workspace_id=ws, proposal=proposal)
            except KernelError:
                conflicts_rejected += 1
            else:
                violations.append(
                    f"seed={seed}: conflicting patch {index} was accepted against "
                    "a stale before-hash"
                )
        elif isinstance(op, InsertNodeOp):
            view_texts[op.node_id] = f"inserted-{op.node_id}"
            declared_texts[op.node_id] = f"declared-{op.node_id}"
            live_nodes.append(op.node_id)
        elif isinstance(op, DeleteNodeOp):
            live_nodes.remove(op.node_id)
            view_texts.pop(op.node_id, None)
            declared_texts.pop(op.node_id, None)
            # The deletion lands structurally at the next rebase; a
            # trailing patch on the doomed node has no replay target in
            # the final graph, so retire it from the pure-replay list
            # (the rebase drops it with a typed reason instead).
            trailing_patch_refs = [
                ref
                for ref in trailing_patch_refs
                if patch_after[ref][0] != op.node_id
            ]
        elif isinstance(op, RebaseOp):
            rebases += 1
            trailing_patch_refs = []
            revision_counter += 1
            old_revision_ref = f"rev-{ws}-{revision_counter - 1}"
            new_revision_ref = f"rev-{ws}-{revision_counter}"
            previous_texts = dict(declared_texts)
            new_texts = dict(declared_texts)
            for nid in op.changed_nodes:
                new_texts[nid] = f"s{revision_counter}-{nid}-{rng.randrange(1000)}"

            derived_calls: list[str] = []

            def derive(node_id: str) -> str:
                derived_calls.append(node_id)
                return new_texts[node_id]

            _, report = await incremental_rebuild(
                kernel_env,
                service,
                workspace_id=ws,
                new_content_revision_ref=new_revision_ref,
                new_graph=make_graph(live_nodes),
                changed_input_refs=[f"fact:{nid}" for nid in op.changed_nodes],
                declarations=_declarations(live_nodes, ws, op.conservative),
                derive=derive,
            )
            modes_seen.add(report.mode)
            current_revision = (await read_current_view(kernel_env, ws)).revision_id
            declared_texts = dict(new_texts)
            committed_nodes = tuple(live_nodes)
            committed_declared = dict(new_texts)
            view_node_ids = set(committed_nodes)
            # The rebased view is the new declared source with every
            # SURVIVING patch replayed on top (victims dropped with a
            # typed reason), so track exactly that.
            view_texts = dict(new_texts)
            for ref in report.applied_refs:
                applied_node, applied_after = patch_after[ref]
                view_texts[applied_node] = applied_after

            # Localized runs must derive exactly the changed nodes —
            # unknown scope widens (mode "full"), never narrows.
            if report.mode == "localized" and op.changed_nodes:
                if sorted(derived_calls) != sorted(set(op.changed_nodes)):
                    violations.append(
                        f"seed={seed}: localized rebuild derived "
                        f"{sorted(set(derived_calls))} but only "
                        f"{sorted(set(op.changed_nodes))} changed"
                    )

            # Mapping composition: carried nodes keep their quote bytes
            # (deterministic mapping); edited nodes lose the byte match
            # (stale). Dispositions are committed into the same history
            # the oracles replay.
            mapping_records = []
            new_anchors = [
                _anchor(nid, new_revision_ref, new_texts[nid], ws) for nid in live_nodes
            ]
            for nid in live_nodes:
                if nid not in previous_texts:
                    continue
                old_anchor = _anchor(nid, old_revision_ref, previous_texts[nid], ws)
                outcome = map_anchor(
                    old_anchor,
                    tuple(new_anchors),
                    source_revision_ref=old_revision_ref,
                    target_revision_ref=new_revision_ref,
                )
                mapping_dispositions[outcome.disposition] = (
                    mapping_dispositions.get(outcome.disposition, 0) + 1
                )
                mapping_records.append(
                    SourceAnchorMappingRecord.from_outcome(
                        outcome,
                        source_revision_ref=old_revision_ref,
                        target_revision_ref=new_revision_ref,
                        source_anchor_id=old_anchor.anchor_id(),
                    )
                )
            if mapping_records:
                await service.commit(
                    KernelCommitBatch(
                        workspace_id=ws,
                        records=tuple(mapping_records),
                        producer={"op": "pr82-incremental-mapping"},
                    )
                )

    final = await read_current_view(kernel_env, ws)

    # Oracle 1: committed-history replay equals the incremental result.
    rebuilt = await clean_rebuild_view(kernel_env, ws)
    equal_clean = rebuilt.view_revision_id() == final.revision_id
    if not equal_clean:
        violations.append(f"seed={seed}: history replay diverged from incremental result")

    # Oracle 2: full-derivation pure replay equals the same revision.
    # The rebase replays every committed patch ref (survivors apply,
    # victims drop); patches submitted AFTER the last rebase are then
    # applied on top operation-by-operation, mirroring the committed
    # history without reusing it.
    last_rebase_revision = f"rev-{ws}-{revision_counter}"
    rebase_op = PatchOperation.rebase_source(
        new_content_revision_ref=last_rebase_revision,
        source_graph=make_graph(committed_nodes),
        source_texts=dict(committed_declared),
        replay_proposal_refs=tuple(patch_refs),
    )
    proposals = {}
    for entry in await load_view_history(kernel_env, ws):
        if entry.proposal is not None and entry.proposal_record_id is not None:
            proposals[entry.proposal_record_id] = entry.proposal
    replayed = apply_rebase_source(rebase_op, proposals).view
    graph, replay_texts = replayed.graph, dict(replayed.texts)
    for ref in trailing_patch_refs:
        for op in proposals[ref].operations:
            graph, replay_texts = apply_operation(graph, replay_texts, op)
    independent = ViewDocumentRecord(
        record_id=f"{ws}-oracle2",
        content_revision_ref=replayed.content_revision_ref,
        graph=graph,
        texts=replay_texts,
    )
    equal_independent = independent.view_revision_id() == final.revision_id
    if not equal_independent:
        violations.append(f"seed={seed}: full-derivation replay diverged")

    return ScenarioResult(
        seed=seed,
        op_count=len(ops),
        modes=tuple(sorted(modes_seen)),
        rebases=rebases,
        conflicts_rejected=conflicts_rejected,
        conflicts_skipped=conflicts_skipped,
        mapping_dispositions=mapping_dispositions,
        equal_clean_replay=equal_clean,
        equal_independent_replay=equal_independent,
        violations=tuple(violations),
    )


async def evaluate_incremental(
    kernel_env, seeds: Sequence[int] = DEFAULT_SEEDS
) -> IncrementalResult:
    """Run all mixed scenarios; any oracle divergence is a violation."""
    results: list[ScenarioResult] = []
    violations: list[str] = []
    for seed in seeds:
        result = await run_mixed_scenario(kernel_env, seed)
        results.append(result)
        violations.extend(result.violations)
    return IncrementalResult(scenarios=tuple(results), violations=tuple(violations))
