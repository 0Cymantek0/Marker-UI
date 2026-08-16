"""PR74 claims/proofs operational benchmark.

Measures the cost PR74 adds to the commit path and the claim-precondition
seam, so the simple visible-cut graph check is justified by numbers
rather than guesswork (V3.2 §10):

* pure cycle detection and grounding over synthetic proof graphs
  (100 / 1,000 / 10,000 relations; long chain = DFS worst case; wide
  fan = BFS worst case);
* the full in-transaction ``check_batch_proof_integrity`` against a
  file-backed SQLite database pre-loaded with committed history
  (existing-history + same-batch validation);
* ``evaluate_claim_requirements`` over committed assessment populations
  (the PR73 claim-precondition seam).

Run from the repository root:

    python backend/scripts/bench_pr74_claims.py

Writes ``docs/reference/measurements/pr74-claims-proofs.json``.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.kernel.models import (  # noqa: E402
    KernelCommitHead,
    KernelRecord,
    KernelRecordEdge,
)
from app.kernel.proofs import (  # noqa: E402
    ClaimRequirement,
    ProofBatchRecord,
    check_batch_proof_integrity,
    detect_proof_cycle,
    evaluate_claim_requirements,
    proof_closure_path_to_authority_consumer,
)
from app.kernel.records import KernelEdge  # noqa: E402

MEASUREMENTS_PATH = (
    BACKEND.parent / "docs" / "reference" / "measurements" / "pr74-claims-proofs.json"
)

SCALE_POINTS = (100, 1_000, 10_000)


def time_callable(fn, *, repeat: int = 5) -> float:
    """Best-of-N wall seconds (min filters scheduler noise)."""
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - started)
    return best


# ---------------------------------------------------------------------------
# pure graph probes
# ---------------------------------------------------------------------------


def chain_relations(count: int):
    """Worst-case DFS shape: one long reliance chain."""
    supports = [("assessment-1", "obs-0")]
    derived = [(f"obs-{i}", f"obs-{i + 1}") for i in range(count - 1)]
    return supports, derived


def wide_relations(count: int):
    """Worst-case BFS shape: one consumer, many independent witnesses."""
    supports = [("assessment-1", f"obs-{i}") for i in range(count)]
    return supports, []


def mixed_dag_relations(count: int):
    """Half fan / half chain — the shape real proofs resemble."""
    half = count // 2
    supports = [("assessment-1", f"obs-{i}") for i in range(half)] + [
        ("assessment-2", f"obs-{half}")
    ]
    derived = [(f"obs-{half + i}", f"obs-{half + i + 1}") for i in range(count - half - 1)]
    derived.append((f"obs-{half}", f"obs-{half + 1}"))
    return supports, derived


def bench_pure_probes() -> dict:
    results = {"cycle_detection_seconds": {}, "grounding_seconds": {}}
    for count in SCALE_POINTS:
        for name, builder in (
            ("chain", chain_relations),
            ("wide", wide_relations),
            ("mixed_dag", mixed_dag_relations),
        ):
            supports, derived = builder(count)
            elapsed = time_callable(
                lambda s=supports, d=derived: detect_proof_cycle(s, d)
            )
            results["cycle_detection_seconds"][f"{name}_{count}"] = round(elapsed, 6)
            classes = {h: "claim_assessment" for h, _ in supports}
            classes.update({e: "observation" for _, e in supports})
            grounding = time_callable(
                lambda s=supports, d=derived, c=classes:
                proof_closure_path_to_authority_consumer("assessment-1", s, d, c)
            )
            results["grounding_seconds"][f"{name}_{count}"] = round(grounding, 6)
    # Cycle-at-the-far-end: the rejected worst case for the DFS.
    for count in SCALE_POINTS:
        supports, derived = chain_relations(count)
        closing = supports + [(f"obs-{count - 1}", "assessment-1")]
        elapsed = time_callable(
            lambda c=closing, d=derived: detect_proof_cycle(c, d)
        )
        results["cycle_detection_seconds"][f"cycle_far_{count}"] = round(elapsed, 6)
    return results


# ---------------------------------------------------------------------------
# in-transaction check against committed history (file-backed SQLite)
# ---------------------------------------------------------------------------


def batch_view(record_id: str, record_class: str, payload: dict) -> ProofBatchRecord:
    return ProofBatchRecord(
        record_id=record_id,
        record_class=record_class,
        payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def assessment_payload(assessment_id: str, outcome: str = "verified") -> dict:
    return {
        "assertion_ref": f"assertion-{assessment_id}",
        "outcome": outcome,
        "policy": {"policy_id": "policy.default", "revision": "rev-3"},
        "evidence_refs": [f"obs-{assessment_id}"],
        "snapshot_commit_id": 1,
        "workflow_class": "bench.v1",
        "declared_context": {},
    }


def support_payload(evidence_id: str) -> dict:
    # role=derived: the seeded chain gives every obs node derivation
    # lineage, so the batch support must honestly declare derivation.
    return {
        "holder_ref": "assessment-new",
        "evidence_ref": evidence_id,
        "role": "derived",
        "authority_rule": "policy.default/rev-3:derived-v1",
    }


async def _seed_history(factory, edge_count: int) -> None:
    """Insert committed observations + derived_from edges directly."""
    async with factory() as session:
        session.add(KernelCommitHead(workspace_id="bench", head_kernel_commit_id=1))
        records = [
            KernelRecord(
                id=f"obs-{i}",
                workspace_id="bench",
                kernel_commit_id=1,
                record_class="observation",
                record_type="marker.kernel.observation.v1",
                schema_version="1.0.0",
                identity_hash=f"sha256:{i:064d}",
                payload_json="{}",
            )
            for i in range(edge_count + 1)
        ]
        records.append(
            KernelRecord(
                id="assessment-new",
                workspace_id="bench",
                kernel_commit_id=1,
                record_class="claim_assessment",
                record_type="marker.kernel.claim_assessment.v1",
                schema_version="1.0.0",
                identity_hash="sha256:" + "a" * 64,
                payload_json=json.dumps(assessment_payload("new")),
            )
        )
        session.add_all(records)
        session.add_all(
            KernelRecordEdge(
                id=f"edge-{i}",
                workspace_id="bench",
                kernel_commit_id=1,
                edge_kind="derived_from",
                source_record_id=f"obs-{i}",
                target_record_id=f"obs-{i + 1}",
            )
            for i in range(edge_count)
        )
        await session.commit()


def bench_commit_check() -> dict:
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for edge_count in SCALE_POINTS:
            db_path = Path(tmp) / f"bench_{edge_count}.db"
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{db_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            asyncio.run(_prepare_engine(engine))
            factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            asyncio.run(_seed_history(factory, edge_count))
            batch_records = {
                "assertion-new": batch_view(
                    "assertion-new",
                    "claim_assertion",
                    {
                        "claim_key": "bench.claim",
                        "subject": "doc:bench",
                        "predicate": "value",
                        "value": 1,
                        "qualifiers": {},
                    },
                ),
                "assessment-new": batch_view(
                    "assessment-new",
                    "claim_assessment",
                    # evidence obs-0 == the support's evidence (agreement)
                    {**assessment_payload("new"), "evidence_refs": ["obs-0"]},
                ),
                "sup-new": batch_view(
                    "sup-new", "proof_support", support_payload("obs-0")
                ),
            }
            edges = [
                KernelEdge(
                    edge_kind="derived_from",
                    source_ref="obs-edge-new",
                    target_ref="obs-0",
                )
            ]

            async def run_check():
                async with factory() as session:
                    async with session.begin():
                        await check_batch_proof_integrity(
                            session,
                            workspace_id="bench",
                            batch_records=batch_records,
                            edges=edges,
                            current_head=1,
                        )

            elapsed = time_callable(lambda: asyncio.run(run_check()))
            results[f"edges_{edge_count}"] = {
                "seconds": round(elapsed, 6),
                "committed_derived_edges": edge_count,
                "batch_records": len(batch_records),
            }
            asyncio.run(engine.dispose())
    return results


async def _prepare_engine(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def bench_preconditions() -> dict:
    """evaluate_claim_requirements over committed assessment counts."""
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for assessment_count in (10, 100, 1_000):
            db_path = Path(tmp) / f"pre_{assessment_count}.db"
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{db_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            asyncio.run(_prepare_engine(engine))
            factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            asyncio.run(_seed_assessments(factory, assessment_count))

            requirements = (
                ClaimRequirement(
                    assertion_ref="assertion-1",
                    policy_id="policy.default",
                    policy_revision="rev-3",
                ),
            )

            async def run_eval():
                async with factory() as session:
                    async with session.begin():
                        await evaluate_claim_requirements(
                            session,
                            "bench",
                            requirements,
                            current_head=1,
                        )

            # The seeded assessments are 'uncertain' — the requirement
            # rejects; measuring the rejection path (worst case: full
            # scan + revalidation attempt).
            def attempt():
                try:
                    asyncio.run(run_eval())
                except Exception:
                    pass

            elapsed = time_callable(attempt)
            results[f"assessments_{assessment_count}"] = round(elapsed, 6)
            asyncio.run(engine.dispose())
    return results


async def _seed_assessments(factory, assessment_count: int) -> None:
    async with factory() as session:
        session.add(KernelCommitHead(workspace_id="bench", head_kernel_commit_id=1))
        session.add_all(
            KernelRecord(
                id=f"assessment-{i}",
                workspace_id="bench",
                kernel_commit_id=1,
                record_class="claim_assessment",
                record_type="marker.kernel.claim_assessment.v1",
                schema_version="1.0.0",
                identity_hash=f"sha256:{i:064x}",
                payload_json=json.dumps(
                    assessment_payload(str(i), outcome="uncertain")
                ),
            )
            for i in range(assessment_count)
        )
        session.add_all(
            KernelRecord(
                id=f"sup-{i}",
                workspace_id="bench",
                kernel_commit_id=1,
                record_class="proof_support",
                record_type="marker.kernel.proof_support.v1",
                schema_version="1.0.0",
                identity_hash=f"sha256:{(i + 500000):064x}",
                payload_json=json.dumps(
                    {
                        "holder_ref": f"assessment-{i}",
                        "evidence_ref": f"obs-{i}",
                        "role": "witness",
                        "authority_rule": "policy.default/rev-3:witness-v1",
                    }
                ),
            )
            for i in range(assessment_count)
        )
        session.add_all(
            KernelRecord(
                id=f"obs-{i}",
                workspace_id="bench",
                kernel_commit_id=1,
                record_class="observation",
                record_type="marker.kernel.observation.v1",
                schema_version="1.0.0",
                identity_hash=f"sha256:{(i + 900000):064x}",
                payload_json="{}",
            )
            for i in range(assessment_count)
        )
        await session.commit()


def main() -> None:
    results = {
        "benchmark": "pr74-claims-proofs",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unreported",
        "method": "best-of-5 wall seconds via time.perf_counter",
        "scale_points": list(SCALE_POINTS),
        "pure": bench_pure_probes(),
        "commit_check": bench_commit_check(),
        "precondition_check": bench_preconditions(),
    }
    MEASUREMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEASUREMENTS_PATH.write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {MEASUREMENTS_PATH}")
    for section, values in results["pure"]["cycle_detection_seconds"].items():
        print(f"cycle {section}: {values}s")
    for key, value in results["commit_check"].items():
        print(f"commit-check {key}: {value['seconds']}s")
    for key, value in results["precondition_check"].items():
        print(f"precondition {key}: {value}s")


if __name__ == "__main__":
    main()
