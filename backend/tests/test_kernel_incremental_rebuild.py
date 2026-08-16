"""PR73 incremental rebuild vs the clean-rebuild oracle.

The oracle is double independent: (1) clean_rebuild_view replays every
committed proposal from the genesis revision and cross-checks each
step; (2) an independently FULL-derived source view (derive called for
every node) is replayed purely and must produce the same revision the
incremental path committed — a bad carry cannot hide from either.
"""

from __future__ import annotations

import random

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.kernel.commit import (
    KernelCommitService,
    PHASE_VIEW_ADVANCED,
    PHASE_VIEW_CHECKED,
)
from app.kernel.errors import InjectedFaultError
from app.kernel.models import KernelRecord
from app.kernel.dependencies import (
    COMPLETENESS_CONSERVATIVE_SCOPE,
    COMPLETENESS_EXACT_NATIVE,
    DependencyDeclarationRecord,
    DependencyInput,
)
from app.kernel.patches import (
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
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
from app.kernel.rebuild import incremental_rebuild, submit_rebase

CONF = order_confidence("1.0")
WS = "ws-rebuild"


def make_graph(node_ids):
    nodes = [OrderNode(node_id=nid, anchor_ref=f"anchor-{nid}") for nid in node_ids]
    edges = [
        OrderEdge(
            kind="before", source_id=a, target_id=b, producer="t", confidence=CONF
        )
        for a, b in zip(node_ids, node_ids[1:])
    ]
    return ReadingOrderGraph.build(nodes, edges)


NODES = ["run-a", "run-b", "run-c", "run-d"]
S1_TEXTS = {nid: f"text-{nid}" for nid in NODES}


def exact_declarations(node_ids, record_prefix="decl"):
    return [
        DependencyDeclarationRecord(
            record_id=f"{record_prefix}-{nid}",
            subject_ref=f"derived:{nid}",
            inputs=(DependencyInput(f"fact:{nid}", COMPLETENESS_EXACT_NATIVE),),
            operator="derived.renderer",
            operator_version="1.0.0",
        )
        for nid in node_ids
    ]


def conservative_declaration():
    return DependencyDeclarationRecord(
        record_id="decl-doc-summary",
        subject_ref="derived:doc-summary",
        inputs=(
            DependencyInput("anything:in-document", COMPLETENESS_CONSERVATIVE_SCOPE),
        ),
        scope_ref="document",
        operator="doc.summarizer",
        operator_version="1.0.0",
    )


def replace_proposal(base_revision, *, node_id, before_text, after_text, record_id):
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=base_revision,
            target_checks=(
                TargetCheck(node_id=node_id, before_hash=view_text_hash(before_text)),
            ),
        ),
        operations=(PatchOperation.replace_text(node_id=node_id, after_text=after_text),),
    )


async def record_total(factory) -> int:
    async with factory() as session:
        return await session.scalar(
            select(func.count()).select_from(KernelRecord).where(
                KernelRecord.workspace_id == WS
            )
        )


@pytest_asyncio.fixture
async def env(kernel_env):
    service = KernelCommitService(kernel_env)
    genesis = await initialize_view(
        kernel_env,
        service,
        workspace_id=WS,
        content_revision_ref="rev-s1",
        graph=make_graph(NODES),
        texts=dict(S1_TEXTS),
    )
    return kernel_env, service, genesis


def _patch_refs(factory):
    async def _inner():
        history = await load_view_history(factory, WS)
        return tuple(
            entry.proposal_record_id
            for entry in history
            if entry.proposal is not None
            and entry.proposal_record_id is not None
            and all(op.op_type != "rebase_source" for op in entry.proposal.operations)
        )

    return _inner


@pytest.mark.asyncio
async def test_local_edit_localized_rebuild_saves_work(env):
    factory, service, genesis = env
    # A survives the source change (its value is untouched); B does not.
    surviving = replace_proposal(
        genesis.revision_id,
        node_id="run-a",
        before_text=S1_TEXTS["run-a"],
        after_text="patched-run-a",
        record_id="p-survive",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=surviving)
    doomed = replace_proposal(
        (await read_current_view(factory, WS)).revision_id,
        node_id="run-b",
        before_text=S1_TEXTS["run-b"],
        after_text="patched-run-b",
        record_id="p-doomed",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=doomed)

    s2_texts = {**S1_TEXTS, "run-b": "text-run-b-v2"}
    derived: list[str] = []

    def derive(node_id: str) -> str:
        derived.append(node_id)
        return s2_texts[node_id]

    acceptance, report = await incremental_rebuild(
        factory,
        service,
        workspace_id=WS,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(NODES),
        changed_input_refs=["fact:run-b"],
        declarations=exact_declarations(NODES),
        derive=derive,
    )
    assert report.mode == "localized"
    assert derived == ["run-b"]  # only the invalidated node was re-derived
    assert set(report.carried_node_ids) == {"run-a", "run-c", "run-d"}
    assert report.applied_refs == ("p-survive",)
    assert [ref for ref, _ in report.dropped_refs] == ["p-doomed"]
    assert report.dropped_refs[0][1] == "BeforeHashMismatchError"

    final = await read_current_view(factory, WS)
    assert final.view.texts["run-a"] == "patched-run-a"  # repair carried
    assert final.view.texts["run-b"] == "text-run-b-v2"  # stale repair dropped
    assert final.view.content_revision_ref == "rev-s2"

    # Oracle 1: committed history replays to exactly this revision.
    assert (await clean_rebuild_view(factory, WS)).view_revision_id() == final.revision_id
    # Oracle 2: an independently full-derived source view replays to the
    # same revision (a wrong carry would diverge here).
    full_texts = {nid: s2_texts[nid] for nid in NODES}
    refs = await _patch_refs(factory)()
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=make_graph(NODES),
        source_texts=full_texts,
        replay_proposal_refs=refs,
    )
    proposals = {}
    for entry in await load_view_history(factory, WS):
        if entry.proposal is not None and entry.proposal_record_id is not None:
            proposals[entry.proposal_record_id] = entry.proposal
    independent = apply_rebase_source(op, proposals).view
    assert independent.view_revision_id() == final.revision_id


@pytest.mark.asyncio
async def test_conservative_knowledge_widens_to_full_derivation(env):
    factory, service, _genesis = env
    s2_texts = {**S1_TEXTS, "run-c": "text-run-c-v2"}
    derived: list[str] = []

    def derive(node_id: str) -> str:
        derived.append(node_id)
        return s2_texts[node_id]

    # The conservative input itself changed: the declared scope widens.
    _, report = await incremental_rebuild(
        factory,
        service,
        workspace_id=WS,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(NODES),
        changed_input_refs=["anything:in-document"],
        declarations=exact_declarations(NODES) + [conservative_declaration()],
        derive=derive,
    )
    assert report.mode == "full"
    assert sorted(derived) == sorted(NODES)
    assert report.invalidation.widened is True
    final = await read_current_view(factory, WS)
    assert (await clean_rebuild_view(factory, WS)).view_revision_id() == final.revision_id


@pytest.mark.asyncio
async def test_unknown_change_widens_to_full(env):
    factory, service, _genesis = env
    derived: list[str] = []
    s2_texts = dict(S1_TEXTS)

    def derive(node_id: str) -> str:
        derived.append(node_id)
        return s2_texts[node_id]

    _, report = await incremental_rebuild(
        factory,
        service,
        workspace_id=WS,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(NODES),
        changed_input_refs=["fact:nobody-declares"],  # no exact knowledge covers it
        declarations=exact_declarations(NODES),
        derive=derive,
    )
    # No exact knowledge explains the change: carrying anything past it
    # would pretend a narrower graph than declared, so everything derives.
    assert report.mode == "full"
    assert report.invalidation.uncovered_changes == frozenset({"fact:nobody-declares"})
    assert sorted(derived) == sorted(NODES)


@pytest.mark.asyncio
async def test_split_structure_survives_source_change_via_pure_carry(env):
    """A split on an unchanged node replays over the carried pure source
    value: derivation stays localized, the split re-applies, and the
    committed revision equals both oracles."""
    factory, service, genesis = env
    split = PatchProposalRecord(
        record_id="p-split-c",
        preconditions=PatchPreconditions(
            base_revision_id=genesis.revision_id,
            target_checks=(
                TargetCheck(
                    node_id="run-c", before_hash=view_text_hash(S1_TEXTS["run-c"])
                ),
            ),
        ),
        operations=(
            PatchOperation.split_node(
                node_id="run-c",
                children=[
                    {"node_id": "run-c-s1", "text": "text-run"},
                    {"node_id": "run-c-s2", "text": "-c"},
                ],
                child_order=["run-c-s1", "run-c-s2"],
            ),
        ),
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=split)

    s2_texts = {**S1_TEXTS, "run-a": "text-run-a-v2"}
    derived: list[str] = []

    def derive(node_id: str) -> str:
        derived.append(node_id)
        return s2_texts[node_id]

    _, report = await incremental_rebuild(
        factory,
        service,
        workspace_id=WS,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(NODES),  # run-c is whole in the source again
        changed_input_refs=["fact:run-a"],
        declarations=exact_declarations(NODES),
        derive=derive,
    )
    # The carry is from the declared SOURCE facts, not the split view:
    # only run-a is derived, the split replays over the pure value.
    assert report.mode == "localized"
    assert derived == ["run-a"]
    assert report.applied_refs == ("p-split-c",)
    final = await read_current_view(factory, WS)
    assert final.view.texts["run-a"] == "text-run-a-v2"
    assert final.view.texts["run-c-s2"] == "-c"
    assert (await clean_rebuild_view(factory, WS)).view_revision_id() == final.revision_id


@pytest.mark.asyncio
async def test_declared_structural_addition_is_localized(env):
    """A new node whose fact ref is declared changed derives exactly that
    node; everything else carries from the declared source."""
    factory, service, _genesis = env
    s2_nodes = NODES + ["run-new"]
    s2_texts = {**S1_TEXTS, "run-new": "text-run-new"}
    derived: list[str] = []

    def derive(node_id: str) -> str:
        derived.append(node_id)
        return s2_texts[node_id]

    _, report = await incremental_rebuild(
        factory,
        service,
        workspace_id=WS,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(s2_nodes),
        changed_input_refs=["fact:run-new"],
        declarations=exact_declarations(s2_nodes),
        derive=derive,
    )
    assert report.mode == "localized"
    assert derived == ["run-new"]
    assert set(report.carried_node_ids) == set(NODES)
    final = await read_current_view(factory, WS)
    assert final.view.texts["run-new"] == "text-run-new"
    assert (await clean_rebuild_view(factory, WS)).view_revision_id() == final.revision_id


@pytest.mark.asyncio
async def test_undeclared_structural_addition_falls_back_to_full(env):
    """A node appears in the source with no declared change covering it:
    the id divergence escapes the invalidated scope, so the rebuild
    derives everything instead of guessing a carry."""
    factory, service, _genesis = env
    s2_nodes = NODES + ["run-new"]
    s2_texts = {**S1_TEXTS, "run-new": "text-run-new"}
    derived: list[str] = []

    def derive(node_id: str) -> str:
        derived.append(node_id)
        return s2_texts[node_id]

    _, report = await incremental_rebuild(
        factory,
        service,
        workspace_id=WS,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(s2_nodes),
        changed_input_refs=["fact:run-a"],  # run-new's arrival is undeclared
        declarations=exact_declarations(s2_nodes),
        derive=derive,
    )
    assert report.mode == "full"
    assert sorted(derived) == sorted(s2_nodes)
    final = await read_current_view(factory, WS)
    assert final.view.texts["run-new"] == "text-run-new"
    assert (await clean_rebuild_view(factory, WS)).view_revision_id() == final.revision_id


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [PHASE_VIEW_CHECKED, PHASE_VIEW_ADVANCED])
async def test_rebase_fault_injection_never_leaves_mixed_state(env, phase):
    factory, service, _genesis = env
    before = await read_current_view(factory, WS)
    total = await record_total(factory)
    s2_texts = {**S1_TEXTS, "run-b": "text-run-b-v2"}
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=make_graph(NODES),
        source_texts=dict(s2_texts),
        replay_proposal_refs=(),
    )
    with pytest.raises(InjectedFaultError):
        await submit_rebase(
            factory,
            service,
            workspace_id=WS,
            rebase_operation=op,
            _inject_fault_at=phase,
        )
    after = await read_current_view(factory, WS)
    assert after.revision_id == before.revision_id  # old-valid current
    assert await record_total(factory) == total  # nothing partial


# ---------------------------------------------------------------------------
# randomized equivalence over the tracer model
# ---------------------------------------------------------------------------


async def _run_randomized_scenario(kernel_env, seed: int) -> None:
    rng = random.Random(seed)
    node_count = rng.randint(3, 6)
    node_ids = [f"n{i:02d}" for i in range(node_count)]
    ws = f"ws-rand-{seed}"
    service = KernelCommitService(kernel_env)
    s1_texts = {nid: f"s1-{nid}-{rng.randrange(1000)}" for nid in node_ids}
    genesis = await initialize_view(
        kernel_env,
        service,
        workspace_id=ws,
        content_revision_ref="rev-s1",
        graph=make_graph(node_ids),
        texts=dict(s1_texts),
    )

    # Random accepted patch chain, tracking each patch's target node.
    current_revision = genesis.revision_id
    current_texts = dict(s1_texts)
    patches: list[tuple[str, str]] = []  # (record_id, target node)
    for k in range(rng.randint(0, 4)):
        nid = rng.choice(node_ids)
        after = f"patch{k}-{nid}-{rng.randrange(1000)}"
        proposal = replace_proposal(
            current_revision,
            node_id=nid,
            before_text=current_texts[nid],
            after_text=after,
            record_id=f"{ws}-p{k}",
        )
        await submit_patch(kernel_env, service, workspace_id=ws, proposal=proposal)
        current_revision = (await read_current_view(kernel_env, ws)).revision_id
        current_texts[nid] = after
        patches.append((f"{ws}-p{k}", nid))

    # Random source advance: change 0-2 node texts (sometimes none).
    changed_nodes = rng.sample(node_ids, rng.randint(0, min(2, node_count)))
    s2_texts = dict(s1_texts)
    for nid in changed_nodes:
        s2_texts[nid] = f"s2-{nid}-{rng.randrange(1000)}"

    declarations = exact_declarations(node_ids, record_prefix=f"{ws}-decl")
    if rng.random() < 0.3:
        declarations.append(conservative_declaration())

    derived_calls: list[str] = []

    def derive(node_id: str) -> str:
        derived_calls.append(node_id)
        return s2_texts[node_id]

    _, report = await incremental_rebuild(
        kernel_env,
        service,
        workspace_id=ws,
        new_content_revision_ref="rev-s2",
        new_graph=make_graph(node_ids),
        changed_input_refs=[f"fact:{nid}" for nid in changed_nodes],
        declarations=declarations,
        derive=derive,
    )

    final = await read_current_view(kernel_env, ws)

    # Oracle 1: committed-history replay equals the incremental result.
    rebuilt = await clean_rebuild_view(kernel_env, ws)
    assert rebuilt.view_revision_id() == final.revision_id, (
        f"seed={seed}: history replay diverged from incremental result"
    )

    # Oracle 2: full derivation + pure replay equals the same revision.
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=make_graph(node_ids),
        source_texts=dict(s2_texts),
        replay_proposal_refs=tuple(pid for pid, _ in patches),
    )
    proposals = {}
    for entry in await load_view_history(kernel_env, ws):
        if entry.proposal is not None and entry.proposal_record_id is not None:
            proposals[entry.proposal_record_id] = entry.proposal
    independent = apply_rebase_source(op, proposals).view
    assert independent.view_revision_id() == final.revision_id, (
        f"seed={seed}: full-derivation replay diverged from incremental result"
    )

    # Expected survivor/drop split: a patch survives exactly when its
    # target node's source value is unchanged (before-hash held vs S2).
    expected_applied = [
        pid for pid, nid in patches if s2_texts[nid] == s1_texts[nid]
    ]
    assert list(report.applied_refs) == expected_applied, f"seed={seed}"

    # Localized runs must actually save derivation work.
    if report.mode == "localized":
        assert sorted(derived_calls) == sorted(changed_nodes), f"seed={seed}"


@pytest.mark.parametrize("seed", list(range(16)))
@pytest.mark.asyncio
async def test_randomized_incremental_equals_clean(kernel_env, seed):
    await _run_randomized_scenario(kernel_env, seed)
