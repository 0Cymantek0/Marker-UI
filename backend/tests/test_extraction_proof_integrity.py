"""PR80A proof-integrity integration tests (matrix F).

Extraction must inherit — never bypass — the kernel's proof rules:
authority-bearing assessments require an exactly matching support
graph, witnesses cannot launder derived material, and no
extraction/reconciliation output can feed back as the evidence for the
source claim it came from (cycle/self-support rejection).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.extraction.service import (
    WITNESS_AUTHORITY_RULE,
    result_record_id,
)
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import KernelError
from app.kernel.models import KernelRecord
from app.kernel.proofs import ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    KernelEdge,
)
from app.kernel.replay import read_head

from tests.test_extraction_service import (
    GOOD_ROWS,
    _invoice_header_doc,
    _items_doc,
    _publish,
    _run,
    _service,
)

pytestmark = pytest.mark.asyncio

WS = "ws-extract"


async def _published_happy_env(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    return _service(payload_env)


async def _committed_records(factory, record_class: str) -> list[KernelRecord]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == record_class,
                )
            )
        ).scalars().all()
    return rows


async def test_accepted_fields_commit_as_kernel_claims_with_matching_proof(
    payload_env,
):
    factory = payload_env[0]
    service = await _published_happy_env(payload_env)
    result = await _run(service)

    assertions = await _committed_records(factory, "claim_assertion")
    assessments = await _committed_records(factory, "claim_assessment")
    supports = await _committed_records(factory, "proof_support")

    # One assertion + authority-bearing assessment per accepted scalar
    # field and per accepted item field, each with exact support graph.
    total_payloads = [
        json.loads(row.payload_json)
        for row in assertions
        if json.loads(row.payload_json)["predicate"] == "total_due"
    ]
    assert total_payloads and total_payloads[0]["value"] == "154.97"

    source_exact = [
        row
        for row in assessments
        if json.loads(row.payload_json)["outcome"] == "source_exact"
    ]
    assert source_exact, "accepted extraction fields must be authority-bearing"
    for row in source_exact:
        payload = json.loads(row.payload_json)
        declared = set(payload["evidence_refs"])
        holders = [
            json.loads(s.payload_json)
            for s in supports
            if json.loads(s.payload_json)["holder_ref"] == row.id
        ]
        assert declared == {h["evidence_ref"] for h in holders}
        assert all(h["authority_rule"] == WITNESS_AUTHORITY_RULE for h in holders)
        # Snapshot honesty: the declared cut contains every witness.
        assert payload["snapshot_commit_id"] <= await read_head(factory, WS)

    # Evidence references are committed, visible workspace records.
    for support in supports:
        payload = json.loads(support.payload_json)
        async with factory() as session:
            evidence = await session.get(KernelRecord, payload["evidence_ref"])
        assert evidence is not None


async def test_extraction_result_cannot_support_its_own_source_claim(payload_env):
    """The laundering/cycle guard: the result view record derives from
    the invoice assertion, so presenting it as a WITNESS for a new
    authority-bearing assessment of that same source must be rejected
    by the existing kernel proof rules."""
    factory, _store, commit_service = payload_env
    service = await _published_happy_env(payload_env)
    result = await _run(service)

    result_id = result_record_id(result.identity)
    assertion_rows = await _committed_records(factory, "claim_assertion")
    invoice_assertion = next(
        row
        for row in assertion_rows
        if json.loads(row.payload_json)["predicate"] == "invoice_number"
    )

    head = await read_head(factory, WS)
    laundering_assertion = ClaimAssertionRecord(
        record_id="launder.assertion-1",
        claim_key="demo.invoice@invoice_number#launder",
        subject="launder",
        predicate="invoice_number",
        value="INV-2026-042",
    )
    laundering_assessment = ClaimAssessmentRecord(
        record_id="launder.assessment-1",
        assertion_ref="launder.assertion-1",
        outcome="source_exact",
        policy_id="marker.extraction.reconcile",
        policy_revision="v1",
        evidence_refs=(result_id,),
        snapshot_commit_id=head,
    )
    derived_edge = KernelEdge(
        edge_kind="derived_from",
        source_ref=result_id,
        target_ref=invoice_assertion.id,
    )
    support = ProofSupportRecord(
        record_id="launder.support-1",
        holder_ref="launder.assessment-1",
        evidence_ref=result_id,
        role="witness",
        authority_rule=WITNESS_AUTHORITY_RULE,
    )
    with pytest.raises(KernelError):
        await commit_service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(laundering_assertion, laundering_assessment, support),
                edges=(derived_edge,),
            )
        )
    # Nothing from the rejected batch may be committed.
    async with factory() as session:
        ghost = await session.get(KernelRecord, "launder.assessment-1")
    assert ghost is None


async def test_result_record_cannot_be_its_own_evidence(payload_env):
    """Direct self-support: an assessment naming ITS OWN assertion as
    evidence is structurally invalid and must fail closed."""
    _factory, _store, commit_service = payload_env
    head = 1
    self_assertion = ClaimAssertionRecord(
        record_id="self.assertion-1",
        claim_key="demo.invoice@self",
        subject="self",
        predicate="total_due",
        value="1.00",
    )
    self_assessment = ClaimAssessmentRecord(
        record_id="self.assessment-1",
        assertion_ref="self.assertion-1",
        outcome="source_exact",
        policy_id="marker.extraction.reconcile",
        policy_revision="v1",
        evidence_refs=("self.assertion-1",),
        snapshot_commit_id=head,
    )
    self_support = ProofSupportRecord(
        record_id="self.support-1",
        holder_ref="self.assessment-1",
        evidence_ref="self.assertion-1",
        role="witness",
        authority_rule=WITNESS_AUTHORITY_RULE,
    )
    with pytest.raises(KernelError):
        await commit_service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(self_assertion, self_assessment, self_support),
            )
        )


async def test_authority_bearing_without_support_is_rejected(payload_env):
    _factory, _store, commit_service = payload_env
    bare_assertion = ClaimAssertionRecord(
        record_id="bare.assertion-1",
        claim_key="demo.invoice@bare",
        subject="bare",
        predicate="total_due",
        value="2.00",
    )
    bare_assessment = ClaimAssessmentRecord(
        record_id="bare.assessment-1",
        assertion_ref="bare.assertion-1",
        outcome="source_exact",
        policy_id="marker.extraction.reconcile",
        policy_revision="v1",
        evidence_refs=(),
        snapshot_commit_id=1,
    )
    with pytest.raises(KernelError):
        await commit_service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(bare_assertion, bare_assessment),
            )
        )


async def test_deterministic_rerun_is_idempotent_not_duplicated(payload_env):
    factory = payload_env[0]
    service = await _published_happy_env(payload_env)
    first = await _run(service)
    second = await _run(service)

    assert second.identity == first.identity
    # Idempotent replay committed exactly one set of extraction records.
    assertions = await _committed_records(factory, "claim_assertion")
    identity_seen: set[str] = set()
    for row in assertions:
        identity_seen.add(row.identity_hash)
    assert len(assertions) == len(identity_seen)
    results = await _committed_records(factory, "native_object")
    extraction_results = [
        row
        for row in results
        if row.id.startswith("extraction.result.")
    ]
    assert len(extraction_results) == 1
