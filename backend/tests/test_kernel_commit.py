"""Kernel commit path tests (V3.2 PR63A, plan workstreams B and C).

Covers the commit authority contract: deterministic parent-linked chain,
atomic multi-record batches with edges, boundary validation, PR61
canonical identity integration, and record-vs-payload identity
separation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    BatchTooLargeError,
    CrossWorkspaceReferenceError,
    DuplicateRecordIdError,
    DuplicateRecordIdentityError,
    EmptyBatchError,
    InvalidRecordPayloadError,
    InvalidWorkspaceIdError,
    KernelError,
    UnknownRecordReferenceError,
)
from app.kernel.models import KernelCommitManifest, KernelRecord, KernelRecordEdge
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    DecisionRecord,
    EDGE_KIND_ASSESSES,
    EDGE_KIND_DERIVED_FROM,
    EDGE_KIND_EVIDENCE_FOR,
    KernelEdge,
    NativeFactRecord,
    NativeObjectRecord,
    ObservationRecord,
)
from app.utils.canonical import CanonicalBox, DecimalValue

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_native_object(source_uri: str = "file:///docs/report.pdf", **overrides):
    fields = {
        "source_uri": source_uri,
        "locator": "pdf:obj:12",
        "media_type": "application/pdf",
        "extractor_name": "marker",
        "extractor_version": "1.0.0",
        "properties": {},
    }
    fields.update(overrides)
    return NativeObjectRecord(**fields)


def make_assertion(claim_key: str = "claim-1", **overrides):
    fields = {
        "claim_key": claim_key,
        "subject": "doc:report.pdf",
        "predicate": "contains_table",
        "value": True,
        "qualifiers": {},
    }
    fields.update(overrides)
    return ClaimAssertionRecord(**fields)


def make_observation(observer: str, derivation: dict, **overrides):
    fields = {
        "observer": observer,
        "derivation": derivation,
        "summary": "",
        "context": {},
    }
    fields.update(overrides)
    return ObservationRecord(**fields)


async def fetch_rows(factory: async_sessionmaker, model, **filters):
    async with factory() as session:
        rows = (await session.execute(select(model).filter_by(**filters))).scalars().all()
    return rows


# ---------------------------------------------------------------------------
# chain formation and atomic batches
# ---------------------------------------------------------------------------


async def test_first_commit_forms_chain_root(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    record = make_assertion()
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,), producer={"op": "test"})
    )

    assert receipt.kernel_commit_id == 1
    assert receipt.parent_kernel_commit_id == 0  # initial empty state
    assert receipt.record_ids == (record.record_id,)
    assert receipt.record_count == 1 and receipt.edge_count == 0

    manifests = await fetch_rows(kernel_env, KernelCommitManifest, workspace_id="ws-a")
    assert len(manifests) == 1
    assert manifests[0].kernel_commit_id == 1
    assert manifests[0].parent_kernel_commit_id == 0
    assert manifests[0].record_count == 1
    assert manifests[0].producer_json == '{"op":"test"}'

    from app.kernel.replay import read_head

    assert await read_head(kernel_env, "ws-a") == 1


async def test_second_commit_names_first_as_parent(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    first = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(make_assertion("c1"),))
    )
    second = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(make_assertion("c2"),))
    )
    assert first.kernel_commit_id == 1
    assert second.kernel_commit_id == 2
    assert second.parent_kernel_commit_id == first.kernel_commit_id


async def test_multi_record_batch_with_edges_atomic(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)

    native_obj = make_native_object()
    fact = NativeFactRecord(
        native_object_ref=native_obj.record_id,
        property_name="page.count",
        raw_representation="17",
        typed_interpretation=17,
        extractor_name="marker",
        extractor_version="1.0.0",
    )
    observation = make_observation(
        "marker-table-detector", {"stage": "layout", "pass": 2}
    )
    assertion = make_assertion()
    assessment = ClaimAssessmentRecord(
        assertion_ref=assertion.record_id,
        outcome="supported",
        policy_id="policy.default",
        policy_revision="rev-3",
        evidence_refs=(observation.record_id,),
        declared_context={"as_of": "commit-local"},
    )
    decision = DecisionRecord(
        decision_key="publish-page-3",
        outcome="accepted",
        rationale="evidence supported",
        input_refs=(assessment.record_id,),
    )

    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(native_obj, fact, observation, assertion, assessment, decision),
            edges=(
                KernelEdge(
                    edge_kind=EDGE_KIND_DERIVED_FROM,
                    source_ref=fact.record_id,
                    target_ref=native_obj.record_id,
                ),
                KernelEdge(
                    edge_kind=EDGE_KIND_EVIDENCE_FOR,
                    source_ref=observation.record_id,
                    target_ref=assertion.record_id,
                ),
                KernelEdge(
                    edge_kind=EDGE_KIND_ASSESSES,
                    source_ref=assessment.record_id,
                    target_ref=assertion.record_id,
                ),
            ),
        )
    )

    assert receipt.record_count == 6
    assert receipt.edge_count == 3

    records = await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a")
    edges = await fetch_rows(kernel_env, KernelRecordEdge, workspace_id="ws-a")
    manifests = await fetch_rows(kernel_env, KernelCommitManifest, workspace_id="ws-a")
    assert len(records) == 6 and len(edges) == 3 and len(manifests) == 1
    assert all(row.kernel_commit_id == 1 for row in records)
    assert all(row.kernel_commit_id == 1 for row in edges)

    # All six PR63 record classes are representable and versioned.
    assert {row.record_class for row in records} == {
        "native_object",
        "native_fact",
        "claim_assertion",
        "claim_assessment",
        "observation",
        "decision",
    }
    manifest = manifests[0]
    import json as _json

    assert _json.loads(manifest.record_class_counts_json) == {
        "claim_assessment": 1,
        "claim_assertion": 1,
        "decision": 1,
        "native_fact": 1,
        "native_object": 1,
        "observation": 1,
    }


async def test_same_commit_and_earlier_commit_dependency_edges(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    first_record = make_assertion("anchor")
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(first_record,))
    )

    new_record = make_observation("late-observer", {"derivation": "second pass"})
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(new_record,),
            edges=(
                # earlier-commit reference
                KernelEdge(
                    edge_kind=EDGE_KIND_EVIDENCE_FOR,
                    source_ref=new_record.record_id,
                    target_ref=first_record.record_id,
                ),
            ),
        )
    )
    assert receipt.kernel_commit_id == 2
    edges = await fetch_rows(kernel_env, KernelRecordEdge, workspace_id="ws-a")
    assert len(edges) == 1
    assert edges[0].target_record_id == first_record.record_id
    assert edges[0].kernel_commit_id == 2


async def test_edges_only_commit_allowed(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    record = make_assertion()
    other = make_observation("obs", {"k": 1})
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record, other))
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            edges=(
                KernelEdge(
                    edge_kind=EDGE_KIND_EVIDENCE_FOR,
                    source_ref=other.record_id,
                    target_ref=record.record_id,
                ),
            ),
        )
    )
    assert receipt.kernel_commit_id == 2
    assert receipt.record_count == 0 and receipt.edge_count == 1


# ---------------------------------------------------------------------------
# boundary validation
# ---------------------------------------------------------------------------


async def test_empty_batch_rejected(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(EmptyBatchError):
        await service.commit(KernelCommitBatch(workspace_id="ws-a"))


@pytest.mark.parametrize(
    "workspace_id", ["", "WS-A", "ws a", "ws/a", "ws\x00a", "x" * 129, "café"]
)
async def test_invalid_workspace_ids_rejected(
    kernel_env: async_sessionmaker, workspace_id: str
) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(InvalidWorkspaceIdError):
        await service.commit(
            KernelCommitBatch(workspace_id=workspace_id, records=(make_assertion(),))
        )


async def test_batch_size_bound_enforced(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env, max_batch_records=2)
    records = tuple(make_assertion(f"c{i}") for i in range(3))
    with pytest.raises(BatchTooLargeError):
        await service.commit(KernelCommitBatch(workspace_id="ws-a", records=records))
    # nothing was committed
    assert await fetch_rows(kernel_env, KernelCommitManifest, workspace_id="ws-a") == []


async def test_duplicate_record_id_in_batch_rejected(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    record = make_assertion()
    duplicate = make_assertion("other")
    duplicate.record_id = record.record_id  # same id, different semantics
    with pytest.raises(DuplicateRecordIdError):
        await service.commit(
            KernelCommitBatch(workspace_id="ws-a", records=(record, duplicate))
        )


async def test_semantically_identical_records_in_batch_rejected(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(DuplicateRecordIdentityError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(make_assertion("same"), make_assertion("same")),
            )
        )


async def test_duplicate_identity_across_commits_rejected(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(make_assertion("dup"),))
    )
    with pytest.raises(DuplicateRecordIdentityError):
        await service.commit(
            KernelCommitBatch(workspace_id="ws-a", records=(make_assertion("dup"),))
        )
    # same semantics in another workspace are a separate identity space
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-b", records=(make_assertion("dup"),))
    )
    assert receipt.kernel_commit_id == 1


async def test_edge_unknown_reference_rejected(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    record = make_assertion()
    with pytest.raises(UnknownRecordReferenceError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(record,),
                edges=(
                    KernelEdge(
                        edge_kind=EDGE_KIND_EVIDENCE_FOR,
                        source_ref=record.record_id,
                        target_ref="does-not-exist",
                    ),
                ),
            )
        )
    assert await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a") == []


async def test_edge_cross_workspace_reference_rejected(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    foreign = make_assertion()
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(foreign,))
    )
    local = make_observation("obs", {"k": 1})
    with pytest.raises(CrossWorkspaceReferenceError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-b",
                records=(local,),
                edges=(
                    KernelEdge(
                        edge_kind=EDGE_KIND_EVIDENCE_FOR,
                        source_ref=local.record_id,
                        target_ref=foreign.record_id,
                    ),
                ),
            )
        )


async def test_edge_self_loop_rejected_at_construction() -> None:
    record = make_assertion()
    with pytest.raises(KernelError):
        KernelEdge(
            edge_kind=EDGE_KIND_DERIVED_FROM,
            source_ref=record.record_id,
            target_ref=record.record_id,
        )


async def test_duplicate_edge_in_batch_rejected(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    a = make_assertion("a")
    b = make_observation("obs", {"k": 1})
    edge = KernelEdge(
        edge_kind=EDGE_KIND_EVIDENCE_FOR, source_ref=b.record_id, target_ref=a.record_id
    )
    with pytest.raises(KernelError, match="duplicate edge"):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(a, b),
                edges=(edge, KernelEdge(edge_kind=edge.edge_kind,
                                        source_ref=edge.source_ref,
                                        target_ref=edge.target_ref)),
            )
        )


async def test_unknown_edge_kind_rejected() -> None:
    record = make_assertion()
    with pytest.raises(KernelError, match="edge_kind"):
        KernelEdge(
            edge_kind="proof_cycles_v2",  # PR74 semantics must not sneak in
            source_ref=record.record_id,
            target_ref="other-record",
        )


async def test_invalid_producer_metadata_rejected(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(InvalidRecordPayloadError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(make_assertion(),),
                producer={"bad": 0.5},  # float never enters canonical metadata
            )
        )


# ---------------------------------------------------------------------------
# canonical identity integration (PR61 contract is the identity boundary)
# ---------------------------------------------------------------------------


async def test_mapping_order_does_not_change_identity(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    # same semantics, different dict construction order
    first = make_observation(
        "obs", {"alpha": 1, "beta": 2, "gamma": {"x": 1, "y": 2}}
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(first,))
    )
    second = make_observation(
        "obs", {"gamma": {"y": 2, "x": 1}, "beta": 2, "alpha": 1}
    )
    with pytest.raises(DuplicateRecordIdentityError):
        # identical semantic record under a different key order must be
        # recognized as the same identity, not committed twice
        await service.commit(
            KernelCommitBatch(workspace_id="ws-a", records=(second,))
        )


async def test_identity_domain_schema_version_separation(
    kernel_env: async_sessionmaker,
) -> None:
    class ObservationV2(ObservationRecord):
        schema_version = "2.0.0"  # same fields, different identity domain

    service = KernelCommitService(kernel_env)
    v1 = make_observation("obs", {"k": 1})
    v2 = ObservationV2(observer="obs", derivation={"k": 1})
    first = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(v1,))
    )
    second = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(v2,))
    )
    assert first.manifest_identity_hash != second.manifest_identity_hash
    records = await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a")
    assert {row.schema_version for row in records} == {"1.0.0", "2.0.0"}
    assert len({row.identity_hash for row in records}) == 2


async def test_raw_unicode_preserved_exactly(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    # NFC form and decomposed form must stay distinct raw values
    nfc = "caf\u00e9"
    decomposed = "cafe\u0301"
    records = (
        make_observation("obs", {"text": nfc}),
        make_observation("obs", {"text": decomposed}),
    )
    await service.commit(KernelCommitBatch(workspace_id="ws-a", records=records))
    rows = await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a")
    payloads = [row.payload_json for row in rows]
    assert any(nfc in payload for payload in payloads)
    assert any(decomposed in payload for payload in payloads)
    assert len({row.identity_hash for row in rows}) == 2  # not folded together


@pytest.mark.parametrize(
    "bad_value",
    [
        0.9,  # float
        {"a", "b"},  # set
        datetime(2026, 1, 1, tzinfo=timezone.utc),  # datetime
        b"raw-bytes",  # bytes
    ],
)
async def test_rejected_canonical_values_rejected_at_kernel_boundary(
    kernel_env: async_sessionmaker, bad_value
) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(InvalidRecordPayloadError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(make_observation("obs", {"value": bad_value}),),
            )
        )
    assert await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a") == []


async def test_decimal_and_geometry_canonical_values_accepted(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    obj = make_native_object()
    fact = NativeFactRecord(
        native_object_ref=obj.record_id,
        property_name="crop.box",
        raw_representation="(1.111, 2.222, 3.333, 4.444)",
        typed_interpretation=DecimalValue.from_decimal(Decimal("12.30")),
        extractor_name="marker",
        extractor_version="1.0.0",
        anchor=CanonicalBox.from_bbox([1.111, 2.222, 3.333, 4.444]),
    )
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(obj, fact))
    )
    assert receipt.record_count == 2
    row = next(
        row
        for row in await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a")
        if row.record_class == "native_fact"
    )
    # fixed-point integers and the canonical decimal string, never floats;
    # raw_representation keeps the source text unnormalized by design
    assert '"x0":1111,"x1":3333,"y0":2222,"y1":4444' in row.payload_json
    assert '"typed_interpretation":"12.30"' in row.payload_json


async def test_two_observations_same_bytes_distinct_evidence(
    kernel_env: async_sessionmaker,
) -> None:
    """Payload dedup must never collapse evidence identity (4C.5)."""
    service = KernelCommitService(kernel_env)
    shared_bytes = b"identical witness payload"
    first = make_observation(
        "operator-a", {"derivation": "ocr-pass-1"}, payload_bytes=shared_bytes
    )
    second = make_observation(
        "operator-b", {"derivation": "ocr-pass-2"}, payload_bytes=shared_bytes
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(first, second))
    )
    rows = await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a")
    assert len(rows) == 2
    # same payload bytes...
    assert len({row.payload_byte_hash for row in rows}) == 1
    assert all(row.payload_length == len(shared_bytes) for row in rows)
    # ...but two distinct evidence identities
    assert len({row.identity_hash for row in rows}) == 2


async def test_declared_payload_hash_and_bytes_rejected_on_bad_format(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(InvalidRecordPayloadError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(
                    make_observation(
                        "obs", {"k": 1}, declared_payload_hash="md5:abc"
                    ),
                ),
            )
        )
    with pytest.raises(InvalidRecordPayloadError):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-a",
                records=(make_observation("obs", {"k": 1}, payload_bytes="str"),),
            )
        )


async def test_record_identity_distinct_from_payload_byte_hash(
    kernel_env: async_sessionmaker,
) -> None:
    """Identity hash and payload hash are stored as separate columns."""
    service = KernelCommitService(kernel_env)
    record = make_observation("obs", {"k": 1}, payload_bytes=b"payload")
    await service.commit(KernelCommitBatch(workspace_id="ws-a", records=(record,)))
    row = (await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a"))[0]
    assert row.identity_hash.startswith("sha256:")
    assert row.payload_byte_hash.startswith("sha256:")
    assert row.identity_hash != row.payload_byte_hash
    # a metadata-only record carries no payload hash at all
    meta_only = make_observation("obs2", {"k": 2})
    await service.commit(KernelCommitBatch(workspace_id="ws-a", records=(meta_only,)))
    row2 = [
        row
        for row in await fetch_rows(kernel_env, KernelRecord, workspace_id="ws-a")
        if row.record_class == "observation"
    ][-1]
    assert row2.payload_byte_hash is None and row2.payload_length is None
