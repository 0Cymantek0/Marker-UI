"""PR72 durability: anchors and reading order survive commit/replay.

Anchors and graph records flow through the existing kernel commit
authority, are covered by verify_history, and rematerialize from the
replayed payload with identical identity — no parallel persistence,
no ephemeral Python objects.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.kernel.anchors import RECORD_TYPE_SOURCE_ANCHOR, SourceAnchorRecord
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import DuplicateRecordIdentityError
from app.kernel.models import KernelRecord
from app.kernel.reading_order import (
    ORDER_EDGE_BEFORE,
    RECORD_TYPE_READING_ORDER,
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    ReadingOrderRecord,
    order_confidence,
)
from app.kernel.records import (
    SOURCE_CONSISTENCY_VERSION_PINNED,
    ContentRevisionRecord,
    EDGE_KIND_DERIVED_FROM,
    KernelEdge,
)
from app.kernel.replay import replay, verify_history
from tests.pr72_fixtures import build_two_column_pdf
from tests.test_pr72_native_tracer import make_pdf_anchors

pytestmark = pytest.mark.asyncio

CONF = order_confidence("1.0")


def _blob(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


async def commit_tracer_batch(kernel_env) -> tuple:
    pdf = build_two_column_pdf()
    revision = ContentRevisionRecord(
        record_id="rev-pdf-1",
        source_ref="src-1",
        blob_key=_blob(pdf),
        byte_length=len(pdf),
        media_type="application/pdf",
        consistency_class=SOURCE_CONSISTENCY_VERSION_PINNED,
        suffix=".pdf",
    )
    anchors = make_pdf_anchors(pdf, "rev-pdf-1")
    graph = ReadingOrderGraph.build(
        [OrderNode(node_id="run-72-720", anchor_ref=anchors[0].record_id),
         OrderNode(node_id="run-72-700", anchor_ref=anchors[1].record_id)],
        [OrderEdge(kind=ORDER_EDGE_BEFORE, source_id="run-72-720", target_id="run-72-700",
                   producer="layout", confidence=CONF)],
    )
    order_record = ReadingOrderRecord(
        record_id="ro-1", content_revision_ref="rev-pdf-1", graph=graph
    )
    edges = (
        KernelEdge(edge_kind=EDGE_KIND_DERIVED_FROM, source_ref="ro-1", target_ref="rev-pdf-1"),
    )
    service = KernelCommitService(kernel_env)
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws-pr72",
            records=(revision, *anchors, order_record),
            edges=edges,
            producer={"op": "pr72-tracer"},
        )
    )
    return receipt, anchors, order_record


class TestDurability:
    async def test_commit_persists_anchors_and_graph(self, kernel_env):
        receipt, anchors, order_record = await commit_tracer_batch(kernel_env)
        assert receipt.record_count == 2 + len(anchors)  # revision + graph + anchors

        async with kernel_env() as session:
            rows = (await session.execute(select(KernelRecord))).scalars().all()
        by_type: dict[str, list] = {}
        for row in rows:
            by_type.setdefault(row.record_type, []).append(row)
        assert len(by_type[RECORD_TYPE_SOURCE_ANCHOR]) == len(anchors)
        assert len(by_type[RECORD_TYPE_READING_ORDER]) == 1
        stored_anchor_ids = {row.identity_hash for row in by_type[RECORD_TYPE_SOURCE_ANCHOR]}
        assert stored_anchor_ids == {a.anchor_id() for a in anchors}

    async def test_verify_history_and_replay_rematerialize(self, kernel_env):
        _, anchors, order_record = await commit_tracer_batch(kernel_env)

        verification = await verify_history(kernel_env, "ws-pr72")
        assert not verification.problems

        result = await replay(kernel_env, "ws-pr72")
        all_views = [record for commit in result.commits for record in commit.records]
        anchor_views = [v for v in all_views if v.record_type == RECORD_TYPE_SOURCE_ANCHOR]
        order_views = [v for v in all_views if v.record_type == RECORD_TYPE_READING_ORDER]
        assert len(anchor_views) == len(anchors) and len(order_views) == 1

        rematerialized = {
            SourceAnchorRecord.from_payload(view.payload, record_id=view.record_id).anchor_id()
            for view in anchor_views
        }
        assert rematerialized == {a.anchor_id() for a in anchors}

        replayed_order = ReadingOrderRecord.from_payload(
            order_views[0].payload, record_id=order_views[0].record_id
        )
        assert replayed_order.graph.graph_id() == order_record.graph.graph_id()

    async def test_duplicate_anchor_identity_converges_to_rejection(self, kernel_env):
        _, anchors, _ = await commit_tracer_batch(kernel_env)
        original = anchors[0]
        # Same selector facts under a new event id: semantically the
        # same anchor — the kernel refuses a second copy.
        duplicate = SourceAnchorRecord(
            record_id="anchor-evt-duplicate",
            content_revision_ref=original.content_revision_ref,
            locator=original.locator,
            selectors=original.selectors,
        )
        assert duplicate.anchor_id() == original.anchor_id()
        service = KernelCommitService(kernel_env)
        with pytest.raises(DuplicateRecordIdentityError):
            await service.commit(
                KernelCommitBatch(
                    workspace_id="ws-pr72", records=(duplicate,), producer={"op": "dup"}
                )
            )

    async def test_recommit_after_simulated_restart_keeps_ids(self, kernel_env):
        # A "restart" cannot see prior objects; it re-extracts from the
        # artifact and must reach the same identities.
        _, anchors, _ = await commit_tracer_batch(kernel_env)
        pdf = build_two_column_pdf()
        fresh = make_pdf_anchors(pdf, "rev-pdf-1")
        assert [a.anchor_id() for a in fresh] == [a.anchor_id() for a in anchors]
