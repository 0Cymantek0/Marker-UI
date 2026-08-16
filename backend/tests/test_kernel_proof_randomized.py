"""PR74 seeded randomized proof-graph growth (V3.2 §9.7).

Property-style, fully seeded (no test-order or hash-order dependence):

* legal DAG growth across multiple commits is always accepted;
* a randomly chosen cycle-closing relation is always rejected with no
  partial state;
* authority-bearing supports mix into the graph without changing the
  invariants;
* replay stays deterministic over the grown history.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import ProofCycleError
from app.kernel.models import KernelRecord, KernelRecordEdge
from app.kernel.proofs import PROOF_ROLE_WITNESS, ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import read_head, replay

pytestmark = pytest.mark.asyncio

WS = "ws-proof-random"
SEED = 20260817
NODES = 48
BATCHES = 6


def make_observation(record_id: str) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        observer="marker-test",
        derivation={"stage": "random-growth", "rank": int(record_id.split("-")[1])},
        summary=f"node {record_id}",
    )


async def head_count(factory: async_sessionmaker) -> tuple[int, int, int]:
    async with factory() as session:
        records = (
            await session.execute(
                select(func.count()).select_from(KernelRecord).where(
                    KernelRecord.workspace_id == WS
                )
            )
        ).scalar_one()
        edges = (
            await session.execute(
                select(func.count()).select_from(KernelRecordEdge).where(
                    KernelRecordEdge.workspace_id == WS
                )
            )
        ).scalar_one()
    return await read_head(factory, WS), records, edges


async def test_seeded_random_dag_growth_and_cycle_closure(kernel_env):
    rng = random.Random(SEED)
    service = KernelCommitService(kernel_env)

    # Phase 1: multi-commit DAG growth. Edges always point from a newer
    # node to an older node (i >= j), which cannot cycle by construction;
    # authority-bearing supports attach to derivation-free witnesses.
    all_nodes = [f"obs-{i}" for i in range(NODES)]
    batches: list[list[str]] = [
        all_nodes[i * NODES // BATCHES:(i + 1) * NODES // BATCHES]
        for i in range(BATCHES)
    ]
    committed_so_far: list[str] = []
    for batch_index, batch_nodes in enumerate(batches):
        records: list = [make_observation(n) for n in batch_nodes]
        edge_pairs: set[tuple[str, str]] = set()
        edges: list[KernelEdge] = []
        for node in batch_nodes:
            for _ in range(rng.randint(0, 3)):
                if not committed_so_far:
                    break
                older = rng.choice(committed_so_far)
                if older != node and (node, older) not in edge_pairs:
                    edge_pairs.add((node, older))
                    edges.append(
                        KernelEdge(edge_kind="derived_from",
                                   source_ref=node, target_ref=older)
                    )
        # Sprinkle authority-bearing witness supports on derivation-free
        # nodes of earlier commits.
        free = [n for n in committed_so_far
                if not any(e.source_ref == n for e in edges)]
        if free and rng.random() < 0.7:
            assertion = ClaimAssertionRecord(
                record_id=f"assertion-{len(committed_so_far)}",
                claim_key=f"random.claim.{len(committed_so_far)}",
                subject="doc:random",
                predicate="value",
                value=len(committed_so_far),
            )
            assessment = ClaimAssessmentRecord(
                record_id=f"assessment-{len(committed_so_far)}",
                assertion_ref=assertion.record_id,
                outcome="verified",
                policy_id="policy.default",
                policy_revision="rev-1",
                evidence_refs=(free[0],),
                snapshot_commit_id=batch_index,
                workflow_class="random.v1",
            )
            witness_free = not await _has_derivation(kernel_env, free[0])
            if witness_free:
                records.extend((assertion, assessment,
                                ProofSupportRecord(
                                    record_id=f"sup-{len(committed_so_far)}",
                                    holder_ref=assessment.record_id,
                                    evidence_ref=free[0],
                                    role=PROOF_ROLE_WITNESS,
                                    authority_rule="policy.default/rev-1",
                                )))
        await service.commit(
            KernelCommitBatch(workspace_id=WS, records=tuple(records),
                              edges=tuple(edges))
        )
        committed_so_far.extend(batch_nodes)

    baseline = await head_count(kernel_env)
    assert baseline[0] == BATCHES
    assert baseline[1] >= NODES

    # Replay determinism over the grown graph.
    digest_a = (await replay(kernel_env, WS)).replay_digest
    digest_b = (await replay(kernel_env, WS)).replay_digest
    assert digest_a == digest_b

    # Phase 2: randomly choose a cycle-closing edge. Compute reachability
    # over the committed reliance graph (derived_from edges only here —
    # the supports are witnesses with no lineage), pick u, v such that
    # v already reaches u, then insert u -> v.
    adjacency: dict[str, set[str]] = {}
    async with kernel_env() as session:
        rows = (
            await session.execute(
                select(KernelRecordEdge.source_record_id,
                       KernelRecordEdge.target_record_id).where(
                    KernelRecordEdge.workspace_id == WS,
                    KernelRecordEdge.edge_kind == "derived_from",
                )
            )
        ).all()
    for source, target in rows:
        adjacency.setdefault(source, set()).add(target)

    def reaches(start: str, goal: str) -> bool:
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            if node == goal and node != start:
                return True
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return False

    closures = [
        (u, v)
        for u in all_nodes
        for v in all_nodes
        if u != v and v in adjacency and reaches(v, u)
    ]
    assert closures, "the grown DAG must contain closable pairs"
    u, v = rng.choice(closures)
    with pytest.raises(ProofCycleError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                edges=(KernelEdge(edge_kind="derived_from",
                                  source_ref=u, target_ref=v),),
            )
        )
    after = await head_count(kernel_env)
    assert after == baseline  # nothing moved, nothing landed


async def _has_derivation(factory: async_sessionmaker, node: str) -> bool:
    async with factory() as session:
        row = (
            await session.execute(
                select(func.count()).select_from(KernelRecordEdge).where(
                    KernelRecordEdge.workspace_id == WS,
                    KernelRecordEdge.source_record_id == node,
                )
            )
        ).scalar_one()
    return row > 0
