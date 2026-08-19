"""PR80A lane integration tests (matrix V) - real authorities, throwaway DBs.

Proves the benchmark lane exercises the production publication/query/
extraction spine, stays deterministic, and cannot leak evidence across
documents or mint authority outside its own workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.eval.pr80b.corpus import load_corpus
from app.eval.pr80b.pr80a_lane import SYSTEM_ID, run_pr80a_lane
from app.eval.pr80b.scoring import score_document

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "eval_data" / "pr80b"

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS_ROOT)


async def test_happy_path_through_real_authorities(corpus, tmp_path):
    out = await run_pr80a_lane(corpus.doc("inv-001-straightforward"), tmp_path)
    assert out.error is None
    assert out.system_id == SYSTEM_ID
    assert out.run_status == "accepted"
    assert out.fields["invoice_number"].value == "INV-2026-001"
    assert out.fields["total_due"].value == "155.00"
    assert all(field.has_evidence for field in out.fields.values())
    assert out.invariant_findings == {"total_due": "satisfied"}
    assert [r.sku for r in out.rows] == ["SKU-1001", "SKU-1002", "SKU-1003"]


async def test_every_emitted_value_carries_a_citation(corpus, tmp_path):
    out = await run_pr80a_lane(corpus.doc("inv-001-straightforward"), tmp_path)
    cited = set(out.raw["cited_record_ids"])
    assert cited == {"inv-001-straightforward-p1"}
    for row in out.rows:
        for field in row.fields.values():
            if field.status == "emitted":
                assert field.has_evidence is True


async def test_missing_required_field_escalates_to_review(corpus, tmp_path):
    out = await run_pr80a_lane(corpus.doc("inv-003-missing-required-date"), tmp_path)
    assert out.run_status == "review_required"
    date = out.fields["invoice_date"]
    assert date.status == "absent"
    assert date.self_flagged is True


async def test_witness_conflict_is_flagged_not_guessed(corpus, tmp_path):
    out = await run_pr80a_lane(corpus.doc("inv-021-two-witness-conflict"), tmp_path)
    assert out.run_status == "review_required"
    total = out.fields["total_due"]
    assert total.status == "absent"
    assert total.self_flagged is True
    assert out.fields["invoice_number"].value == "INV-2026-021"


async def test_broken_row_loss_surfaces_as_invariant_violation(corpus, tmp_path):
    out = await run_pr80a_lane(corpus.doc("inv-013-broken-row-short"), tmp_path)
    assert [r.sku for r in out.rows] == ["SKU-7001", "SKU-7003"]
    assert out.invariant_findings == {"total_due": "violated"}


async def test_two_witness_agreement_collapses_to_one_truth(corpus, tmp_path):
    out = await run_pr80a_lane(corpus.doc("inv-020-two-witness-agree"), tmp_path)
    assert [r.sku for r in out.rows] == ["SKU-3100", "SKU-3101"]
    assert out.run_status == "accepted"
    cited = set(out.raw["cited_record_ids"])
    assert cited == {
        "inv-020-two-witness-agree-p1",
        "inv-020-two-witness-agree-p2",
    }


async def test_lane_is_deterministic_across_reruns(corpus, tmp_path):
    first = await run_pr80a_lane(corpus.doc("inv-012-duplicate-sku-conflicting"), tmp_path / "a")
    second = await run_pr80a_lane(corpus.doc("inv-012-duplicate-sku-conflicting"), tmp_path / "b")
    assert first.fields == second.fields
    assert first.rows == second.rows
    assert first.invariant_findings == second.invariant_findings
    assert first.run_status == second.run_status
    assert (
        first.raw["result_identity"] == second.raw["result_identity"]
    )


async def test_documents_are_isolated_per_workspace(corpus, tmp_path):
    """No evidence crosses documents: each lane run cites only its own records."""
    doc_a = await run_pr80a_lane(corpus.doc("inv-001-straightforward"), tmp_path)
    doc_b = await run_pr80a_lane(corpus.doc("inv-016-total-mismatch"), tmp_path)
    assert set(doc_a.raw["cited_record_ids"]) == {"inv-001-straightforward-p1"}
    assert set(doc_b.raw["cited_record_ids"]) == {"inv-016-total-mismatch-p1"}
    assert doc_a.raw["publication_set_id"] != doc_b.raw["publication_set_id"]


async def test_lane_output_scores_against_gold(corpus, tmp_path):
    """End-to-end sanity: lane output feeds the scorer and matches truth."""
    doc = corpus.doc("inv-001-straightforward")
    out = await run_pr80a_lane(doc, tmp_path)
    score = score_document(doc.gold, out)
    assert score.doc_exact is True
    assert score.invariant["outcome"] == "reported_match"
    assert score.evidence["with_evidence"] == score.evidence["emitted"]


async def test_stale_context_protection_is_not_bypassed(corpus, tmp_path):
    """The lane pins expected_publication_set_id; extraction refuses stale truth.

    Republished truth after the lane's publication must produce a stale
    result when run against the OLD expectation - proven here by driving
    the production service directly with the lane's own database.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.db_migration import upgrade_database
    from app.extraction.contract import ExtractionRequest, INVOICE_SCHEMA
    from app.extraction.service import ExtractionService
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.generations import GenerationService
    from app.kernel.payloads import LocalPayloadStore
    from app.kernel.publications import PublicationService
    from app.kernel.snapshots import resolve_snapshot
    from app.eval.pr80b.pr80a_lane import _part_record

    workspace = "ws-pr80b-stale-probe"
    url = f"sqlite+aiosqlite:///{(tmp_path / 'kernel.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = LocalPayloadStore(tmp_path / "payloads")
        commit_service = KernelCommitService(factory, payload_store=store)
        doc = corpus.doc("inv-001-straightforward")
        await commit_service.commit(
            KernelCommitBatch(
                workspace_id=workspace,
                records=(_part_record("stale-probe", 1, doc.part_texts[0]),),
            )
        )
        generation = await GenerationService(factory).build_and_activate(
            await resolve_snapshot(factory, workspace)
        )
        publication = await PublicationService(factory).publish(
            materialized_generation_id=generation.generation_id
        )
        service = ExtractionService(factory, commit_service, workspace_id=workspace)
        request = ExtractionRequest(
            schema_id=INVOICE_SCHEMA.schema_id,
            schema_version=INVOICE_SCHEMA.version,
            workspace_id=workspace,
            expected_publication_set_id=publication.publication_set_id,
        )
        first = await service.run(request)
        assert first.run_status == "accepted"

        # Republish different truth, then rerun against the old expectation.
        await commit_service.commit(
            KernelCommitBatch(
                workspace_id=workspace,
                records=(
                    _part_record(
                        "stale-probe-2",
                        1,
                        "Invoice Number: INV-CHANGED\nInvoice Date: 2026-09-09\n"
                        "Currency: USD\nTotal Due: 1.00\n"
                        "LINEITEM | SKU-X | Thing | 1 | 1.00 | 1.00",
                    ),
                ),
            )
        )
        generation2 = await GenerationService(factory).build_and_activate(
            await resolve_snapshot(factory, workspace)
        )
        await PublicationService(factory).publish(
            materialized_generation_id=generation2.generation_id
        )
        stale = await service.run(request)
        assert stale.run_status == "stale_context"
    finally:
        await engine.dispose()
