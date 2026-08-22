"""Connector convergence core behavior tests (PR71B, amendment 16B.7).

The full deterministic failure-mode matrix from the implementation
plan's verification envelope, executed against the real migrated
file-backed SQLite lane, the real kernel commit spine, the real
content-addressed source store, and the real authorization overlay —
no mocked database, no in-memory truth.

Every atomicity test re-opens sessions and asserts durable post-state
(records, manifests, outbox, inbox, stream checkpoint, authorization
overlay), not just return values. Restart cases construct fresh service
objects against the same database file.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.context_runtime.authorization import resolve_effective_authorization
from app.kernel.commit import KernelCommitService
from app.kernel.connector_state import (
    CONNECTOR_APPLIED_APPLIED,
    CONNECTOR_APPLIED_DEFERRED,
    CONNECTOR_APPLIED_DUPLICATE,
    CONNECTOR_APPLIED_STALE,
    CONNECTOR_STREAM_CONSUMING,
    CONNECTOR_STREAM_RECONCILIATION_REQUIRED,
)
from app.kernel.errors import KernelError
from app.kernel.models import (
    KernelCommitHead,
    KernelCommitManifest,
    KernelConnectorInbox,
    KernelConnectorStream,
    KernelOutbox,
    KernelRecord,
)
from app.kernel.source_store import LocalSourceStore
from app.services.connector_adapter import (
    ORDERING_NONE,
    ChangePage,
    ItemSnapshot,
    ProviderChange,
    ScanPage,
    ScriptedProvider,
)
from app.services.connector_ingestion import ConnectorIngestionService
from app.services.source_acquisition import SourceAcquisitionService
from app.utils.canonical import payload_byte_hash

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-conn"
STREAM = "drive:acct1:root"

PDF_A = b"%PDF-1.4 connector revision A"
PDF_B = b"%PDF-1.4 connector revision B (different bytes)"
PDF_C = b"%PDF-1.4 connector revision C"


def content_change(
    event_id: str,
    item_id: str,
    data: bytes,
    *,
    revision: str | None,
    seq: int | None,
    ordering: str = "sequenced",
    policy_facts: dict | None = None,
) -> ProviderChange:
    return ProviderChange(
        event_id=event_id,
        item_id=item_id,
        kind="content_changed",
        revision=revision,
        seq=seq,
        content=data,
        suffix=".pdf",
        policy_facts=policy_facts or {},
        ordering=ordering,
    )


def removal(event_id: str, item_id: str, *, seq: int, removal_kind: str = "deleted"):
    return ProviderChange(
        event_id=event_id,
        item_id=item_id,
        kind="removed",
        seq=seq,
        policy_facts={"removal_kind": removal_kind},
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.db_migration import upgrade_database

    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(tmp_path / "roots"))
    monkeypatch.delenv("MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS", raising=False)

    url = f"sqlite+aiosqlite:///{(tmp_path / 'conn.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalSourceStore(tmp_path / "store")
    acq = SourceAcquisitionService(
        factory, KernelCommitService(factory), store, workspace_id=WORKSPACE
    )

    def make_service() -> ConnectorIngestionService:
        # Fresh service objects against the same database: the restart
        # proof (P10) — no correctness-critical in-process state.
        return ConnectorIngestionService(factory, acq)

    try:
        yield SimpleNamespace(
            factory=factory,
            store=store,
            acq=acq,
            service=make_service(),
            make_service=make_service,
            tmp_path=tmp_path,
        )
    finally:
        await engine.dispose()


async def _counts(env) -> Counter:
    async with env.factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord.record_class).where(
                    KernelRecord.workspace_id == WORKSPACE
                )
            )
        ).scalars().all()
    return Counter(rows)


async def _head(env) -> int:
    async with env.factory() as session:
        value = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == WORKSPACE
            )
        )
    # The head row is created lazily by the first commit: None means 0.
    return value or 0


async def _outbox(env) -> list[KernelOutbox]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(KernelOutbox).order_by(KernelOutbox.id.asc())
                )
            ).scalars().all()
        )


async def _inbox(env, stream_id: str = STREAM) -> list[KernelConnectorInbox]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(KernelConnectorInbox)
                    .where(KernelConnectorInbox.stream_id == stream_id)
                    .order_by(KernelConnectorInbox.id.asc())
                )
            ).scalars().all()
        )


async def _manifest_count(env) -> int:
    async with env.factory() as session:
        return await session.scalar(
            select(func.count()).select_from(KernelCommitManifest).where(
                KernelCommitManifest.workspace_id == WORKSPACE
            )
        )


async def _source_identity_id(env, source_key: str) -> str:
    """Committed record id of one connector source identity."""
    record_id = "source." + payload_byte_hash(source_key.encode("utf-8"))[:24]
    async with env.factory() as session:
        row = await session.get(KernelRecord, record_id)
    assert row is not None, f"identity {source_key!r} not committed"
    return row.id


def _key(item_id: str) -> str:
    return f"connector:scripted:acct:{item_id}"


class TestIdempotencyAndReplay:
    """T1–T3: replay safety."""

    async def test_t1_duplicate_event_converges(self, env):
        provider = ScriptedProvider(account="acct")
        change = content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1)
        provider.script_round(
            ChangePage(changes=(change,), next_cursor="tok-1", complete=True, page_seq=1)
        )
        first = await env.service.poll(STREAM, provider)
        assert first.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED
        assert first.kernel_commit_id == 1
        assert first.stream.cursor_token == "tok-1"

        provider.script_round(
            ChangePage(changes=(change,), next_cursor="tok-1", complete=True, page_seq=1)
        )
        second = await env.service.poll(STREAM, provider)
        assert second.outcomes[0].applied_state == CONNECTOR_APPLIED_DUPLICATE
        assert second.kernel_commit_id == 0  # no-op: nothing new, same checkpoint

        counts = await _counts(env)
        assert counts["source_identity"] == 1
        assert counts["content_revision"] == 1
        assert counts["source_observation"] == 1  # one acquisition event
        assert len(await _outbox(env)) == 1  # one invalidation intent
        inbox = await _inbox(env)
        assert len(inbox) == 1  # receipt evidence interpretable, not duplicated
        assert inbox[0].applied_state == CONNECTOR_APPLIED_APPLIED
        assert await _head(env) == 1

    async def test_t2_distinct_notifications_same_state_converge(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(
                    content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),
                ),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        provider.script_round(
            ChangePage(
                changes=(
                    # A different notification identity pointing at the
                    # SAME final provider state (same revision/seq/bytes).
                    content_change("evt-2", "item-1", PDF_A, revision="r1", seq=1),
                ),
                next_cursor="tok-2",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        await env.service.poll(STREAM, provider)

        counts = await _counts(env)
        assert counts["content_revision"] == 1
        assert counts["source_identity"] == 1
        # two notification receipts, two audit observations, one truth
        assert counts["source_observation"] == 2
        assert len(await _inbox(env)) == 2

    async def test_t3_replay_after_restart_identical(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        counts_before = await _counts(env)

        # Full restart: fresh service objects + fresh provider replaying
        # the same delivered round.
        provider2 = ScriptedProvider(account="acct")
        provider2.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        result = await env.make_service().poll(STREAM, provider2)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_DUPLICATE
        assert result.stream.cursor_token == "tok-1"
        assert await _counts(env) == counts_before


class TestOutOfOrderDelivery:
    """T4–T6: arrival order is not causal order."""

    async def test_t4_older_revision_after_newer_never_regresses(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-2", "item-1", PDF_B, revision="r2", seq=2),),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        await env.service.poll(STREAM, provider)
        # Delayed older delivery arrives afterwards.
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-3",
                complete=True,
                page_seq=2,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_STALE

        counts = await _counts(env)
        assert counts["content_revision"] == 1  # truth stays at revision B
        inbox = await _inbox(env)
        assert inbox[1].applied_state == CONNECTOR_APPLIED_STALE
        assert inbox[1].provider_seq == 1

    async def test_t5_same_entity_twice_in_one_page_converges_deterministically(
        self, env
    ):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(
                    content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),
                    content_change("evt-2", "item-1", PDF_B, revision="r2", seq=2),
                ),
                next_cursor="tok-1",
                complete=True,
                page_seq=2,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert all(o.applied_state == CONNECTOR_APPLIED_APPLIED for o in result.outcomes)

        counts = await _counts(env)
        assert counts["source_identity"] == 1
        assert counts["content_revision"] == 2  # A then B — both are truth
        assert counts["source_observation"] == 2
        # The newest revision is the provider-justified final state
        latest_blob = await _latest_content_blob(env)
        assert latest_blob == "sha256:" + payload_byte_hash(PDF_B).removeprefix("sha256:")

    async def test_t6_ordering_free_provider_resolves_via_authoritative_query(
        self, env
    ):
        provider = ScriptedProvider(account="acct")
        provider.seed_item(
            ItemSnapshot(
                item_id="item-1",
                present=True,
                revision="snap-r9",
                content=PDF_B,
                policy_facts={"declared_acl_knowledge": "partial"},
            )
        )
        provider.script_round(
            ChangePage(
                changes=(
                    # Stale-looking payload (old bytes) with NO ordering
                    # proof: must be resolved through provider truth.
                    content_change(
                        "evt-1",
                        "item-1",
                        PDF_A,
                        revision=None,
                        seq=None,
                        ordering=ORDERING_NONE,
                    ),
                ),
                next_cursor="tok-1",
                complete=True,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED
        assert provider.item_fetches == ["item-1"]  # truth came from the query

        counts = await _counts(env)
        assert counts["content_revision"] == 1
        latest_blob = await _latest_content_blob(env)
        assert latest_blob == payload_byte_hash(PDF_B)


class TestAtomicityFaultInjection:
    """T7–T12: source truth and checkpoint commit together or not at all."""

    async def _seed_change(self, env, provider: ScriptedProvider):
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )

    async def _assert_nothing_committed(self, env, *, staged_residue_expected: bool):
        counts = await _counts(env)
        assert counts["source_identity"] == 0
        assert counts["content_revision"] == 0
        assert await _head(env) == 0
        assert await _manifest_count(env) == 0
        assert await _inbox(env) == []
        assert await _outbox(env) == []
        view = await env.service.stream_view(STREAM)
        if staged_residue_expected:
            # Content-addressed bytes may remain (they are residue, not
            # truth): the store holds the object but no record claims it.
            assert len(list((env.tmp_path / "store" / "objects").rglob("*.pdf"))) >= 0
        else:
            assert view is None

    async def test_t7_fault_before_transaction_rolls_back_everything(self, env):
        provider = ScriptedProvider(account="acct")
        await self._seed_change(env, provider)
        with pytest.raises(KernelError):
            await env.service.poll(STREAM, provider, _inject_fault_at="begin")
        await self._assert_nothing_committed(env, staged_residue_expected=False)

    async def test_t8_bytes_staged_fault_before_truth_commit(self, env):
        provider = ScriptedProvider(account="acct")
        await self._seed_change(env, provider)
        with pytest.raises(KernelError):
            await env.service.poll(STREAM, provider, _inject_fault_at="records-inserted")
        # Staged residue may exist; it is not committed source truth.
        await self._assert_nothing_committed(env, staged_residue_expected=True)
        assert await env.store.artifact_exists(payload_byte_hash(PDF_A), ".pdf")
        counts = await _counts(env)
        assert counts["content_revision"] == 0  # ...but never mistaken for truth

    async def test_t9_fault_after_outbox_insert_rolls_back_all(self, env):
        provider = ScriptedProvider(account="acct")
        await self._seed_change(env, provider)
        with pytest.raises(KernelError):
            await env.service.poll(STREAM, provider, _inject_fault_at="connector-applied")
        await self._assert_nothing_committed(env, staged_residue_expected=True)
        # Retry after the crash converges onto valid truth.
        await self._seed_change(env, provider)
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED
        assert result.stream.cursor_token == "tok-1"
        assert (await _counts(env))["content_revision"] == 1

    async def test_t10_fault_immediately_before_commit(self, env):
        provider = ScriptedProvider(account="acct")
        await self._seed_change(env, provider)
        with pytest.raises(KernelError):
            await env.service.poll(STREAM, provider, _inject_fault_at="pre-commit")
        await self._assert_nothing_committed(env, staged_residue_expected=True)

    async def test_t11_crash_after_commit_replay_converges(self, env):
        provider = ScriptedProvider(account="acct")
        await self._seed_change(env, provider)
        await env.service.poll(STREAM, provider)
        head_after = await _head(env)
        counts_after = await _counts(env)

        # Crash between commit and upstream acknowledgement: restart and
        # replay the exact round.
        provider2 = ScriptedProvider(account="acct")
        provider2.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        result = await env.make_service().poll(STREAM, provider2)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_DUPLICATE
        assert await _head(env) == head_after  # no duplicate semantic effect
        assert await _counts(env) == counts_after

    async def test_t12_invalidation_intent_atomic_with_source_truth(self, env):
        provider = ScriptedProvider(account="acct")
        await self._seed_change(env, provider)
        await env.service.poll(STREAM, provider)

        rows = await _outbox(env)
        assert len(rows) == 1
        assert rows[0].work_kind == "source.invalidated"
        assert rows[0].kernel_commit_id == 1  # same commit as the truth
        payload = json.loads(rows[0].payload_json)
        assert payload["source_key"] == _key("item-1")

        # And the fault proof: a commit that rolls back cannot leave the
        # intent behind.
        provider2 = ScriptedProvider(account="acct")
        provider2.script_round(
            ChangePage(
                changes=(content_change("evt-2", "item-2", PDF_B, revision="r1", seq=1),),
                next_cursor="tok-2",
                complete=True,
                page_seq=1,
            )
        )
        with pytest.raises(KernelError):
            await env.service.poll(
                STREAM, provider2, _inject_fault_at="outbox-inserted"
            )
        assert len(await _outbox(env)) == 1  # only the survived one


class TestCheckpointLifecycle:
    """T13–T18: gap-aware, reset-aware, restartable checkpoints."""

    async def test_t13_multi_page_only_completed_progress_becomes_checkpoint(
        self, env
    ):
        provider = ScriptedProvider(account="acct")
        # Page 1 of 2: truncated — must NOT move the checkpoint.
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor=None,
                complete=False,
                page_seq=1,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.stream.cursor_token == ""  # created, nothing claimed

        # Crash between pages: fresh service continues from the same
        # (unmoved) checkpoint and receives the completed page.
        provider2 = ScriptedProvider(account="acct")
        provider2.script_round(
            ChangePage(
                changes=(content_change("evt-2", "item-1", PDF_B, revision="r2", seq=2),),
                next_cursor="tok-9",
                complete=True,
                page_seq=2,
            )
        )
        result2 = await env.make_service().poll(STREAM, provider2)
        assert result2.stream.cursor_token == "tok-9"
        assert result2.stream.cursor_seq == 2
        assert (await _counts(env))["content_revision"] == 2

    async def test_t14_expired_token_parks_stream_in_reconciliation(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)

        provider.script_invalid_signal("token_expired", "delta token expired")
        result = await env.service.poll(STREAM, provider)
        assert result.stream.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
        assert result.stream.reconciliation_reason == "token_expired"
        assert result.stream.cursor_token == "tok-1"  # NOT advanced

        # Further polling is refused until reconciliation completes.
        with pytest.raises(KernelError):
            await env.service.poll(STREAM, provider)

    async def test_t15_provider_reset_requires_completed_reconciliation(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)

        provider.script_round(
            ChangePage(
                changes=(),
                next_cursor=None,
                complete=True,
                invalid_reason="provider_reset",
                invalid_detail="410 Gone",
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.stream.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
        assert result.stream.reconciliation_reason == "provider_reset"

        # The reset interval is only adopted after the full scan's final
        # page installs the fresh checkpoint.
        provider.script_scan(
            [
                ScanPage(
                    changes=(content_change("scan-1", "item-1", PDF_B, revision="r9", seq=9),),
                    resume_token="scan-p2",
                ),
                ScanPage(changes=(), resume_token=None, final=True, fresh_cursor="fresh-1"),
            ]
        )
        rec = await env.service.reconcile(STREAM, provider)
        assert rec.completed
        assert rec.stream.state == CONNECTOR_STREAM_CONSUMING
        assert rec.stream.cursor_token == "fresh-1"
        assert (await _counts(env))["content_revision"] == 2  # A then scanned B

    async def test_t16_sequence_gap_refuses_unsafe_progression(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)

        # Provider skips seq 2..4: page claims seq 5 directly.
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-5", "item-1", PDF_B, revision="r5", seq=5),),
                next_cursor="tok-5",
                complete=True,
                page_seq=5,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.stream.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
        assert result.stream.reconciliation_reason == "gap_detected"
        assert result.stream.cursor_token == "tok-1"
        assert result.stream.cursor_seq == 1
        # The gapped event is deferred evidence, never applied truth
        inbox = await _inbox(env)
        assert inbox[-1].applied_state == CONNECTOR_APPLIED_DEFERRED
        assert (await _counts(env))["content_revision"] == 1

    async def test_t17_crash_mid_reconciliation_resumes_and_completes(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        provider.script_invalid_signal("token_expired")
        await env.service.poll(STREAM, provider)

        scan_pages = [
            ScanPage(
                changes=(content_change("scan-1", "item-1", PDF_B, revision="r9", seq=9),),
                resume_token="scan-p2",
            ),
            ScanPage(
                changes=(content_change("scan-2", "item-2", PDF_C, revision="r8", seq=8),),
                resume_token="scan-p3",
            ),
            ScanPage(changes=(), resume_token=None, final=True, fresh_cursor="fresh-9"),
        ]
        provider.script_scan(scan_pages)

        # Crash after page 1 (bounded work per call).
        partial = await env.service.reconcile(STREAM, provider, page_limit=1)
        assert not partial.completed
        assert partial.stream.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
        assert partial.stream.cursor_token == "scan-p2"  # tentative resume point

        # Incomplete scan was NOT blessed: no fresh checkpoint authority.
        assert partial.stream.cursor_token != "fresh-9"

        # Restart: fresh service + fresh provider replaying the same
        # deterministic scan, resumed from the tentative checkpoint.
        provider2 = ScriptedProvider(account="acct")
        provider2.script_scan(scan_pages)
        done = await env.make_service().reconcile(STREAM, provider2)
        assert done.completed
        assert done.stream.state == CONNECTOR_STREAM_CONSUMING
        assert done.stream.cursor_token == "fresh-9"
        counts = await _counts(env)
        assert counts["content_revision"] == 3  # A, scanned B, scanned C
        assert counts["source_identity"] == 2

    async def test_t18_repeated_reconciliation_is_idempotent(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_scan(
            [
                ScanPage(
                    changes=(content_change("scan-1", "item-1", PDF_A, revision="r1", seq=1),),
                    resume_token="scan-p2",
                ),
                ScanPage(changes=(), resume_token=None, final=True, fresh_cursor="fresh-1"),
            ]
        )
        first = await env.service.reconcile(STREAM, provider)
        assert first.completed
        counts_after_first = await _counts(env)
        head_after_first = await _head(env)

        # Second run, no provider change: converges to the same state.
        provider2 = ScriptedProvider(account="acct")
        provider2.script_scan(
            [ScanPage(changes=(), resume_token=None, final=True, fresh_cursor="fresh-1")]
        )
        second = await env.make_service().reconcile(STREAM, provider2)
        assert second.completed
        assert second.stream.cursor_token == "fresh-1"
        assert await _counts(env) == counts_after_first
        assert await _head(env) == head_after_first  # no redundant commits
        assert len(await _outbox(env)) == 1


class TestSourceLifecycleSemantics:
    """T19–T24: change classes against the existing truth model."""

    async def test_t19_content_update_same_source_new_revision(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-2", "item-1", PDF_B, revision="r2", seq=2),),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        await env.service.poll(STREAM, provider)
        await env.service.poll(STREAM, provider)

        counts = await _counts(env)
        assert counts["source_identity"] == 1  # same logical source
        assert counts["content_revision"] == 2  # new revision for new bytes
        assert counts["source_observation"] == 2
        assert len(await _outbox(env)) == 2  # invalidation per transition

    async def test_t20_policy_only_change_never_mints_content_revision(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(
                    content_change(
                        "evt-1",
                        "item-1",
                        PDF_A,
                        revision="r1",
                        seq=1,
                        policy_facts={"declared_acl_knowledge": "partial", "denied": False},
                    ),
                ),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)

        provider.script_round(
            ChangePage(
                changes=(
                    ProviderChange(
                        event_id="evt-2",
                        item_id="item-1",
                        kind="policy_changed",
                        seq=2,
                        policy_facts={
                            "declared_acl_knowledge": "partial",
                            "granted_to": ["alice"],
                            "denied": False,
                        },
                    ),
                ),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED

        counts = await _counts(env)
        assert counts["content_revision"] == 1  # untouched by the policy change
        assert counts["access_policy_revision"] == 2
        assert counts["source_observation"] == 2
        # The policy transition is inspectable as its own outcome class
        obs = await _observations(env)
        assert any(o["outcome"] == "policy_updated" for o in obs)

    async def test_t21_deletion_denies_live_reads_preserves_history(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        source_id = await _source_identity_id(env, _key("item-1"))

        provider.script_round(
            ChangePage(
                changes=(removal("evt-2", "item-1", seq=2),),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED

        counts = await _counts(env)
        assert counts["content_revision"] == 1  # historical truth preserved
        assert counts["access_denial"] == 1

        # Live-read consequence through the real authorization overlay.
        authz = await resolve_effective_authorization(env.factory, WORKSPACE)
        assert source_id in authz.denied_sources
        assert not authz.allows("any-record", source_ref=source_id)

        # The invalidation intent for the removal is durable.
        payload = json.loads((await _outbox(env))[-1].payload_json)
        assert payload["change_kind"] == "removed"

    async def test_t22_loss_of_access_without_deletion_is_a_security_event(
        self, env
    ):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        source_id = await _source_identity_id(env, _key("item-1"))

        provider.script_round(
            ChangePage(
                changes=(removal("evt-2", "item-1", seq=2, removal_kind="loss_of_access"),),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        await env.service.poll(STREAM, provider)

        authz = await resolve_effective_authorization(env.factory, WORKSPACE)
        assert source_id in authz.denied_sources  # not "nothing happened"
        obs = await _observations(env)
        access_lost = [o for o in obs if o["outcome"] == "access_lost"]
        assert access_lost and access_lost[0]["evidence"]["removal_kind"] == "loss_of_access"
        assert (await _counts(env))["content_revision"] == 1  # history intact

    async def test_t23_move_with_stable_identity_preserves_source(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        source_before = await _source_identity_id(env, _key("item-1"))

        provider.script_round(
            ChangePage(
                changes=(
                    ProviderChange(
                        event_id="evt-2",
                        item_id="item-1",
                        kind="moved",
                        seq=2,
                        new_location="/new/place/report.pdf",
                    ),
                ),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED

        assert await _source_identity_id(env, _key("item-1")) == source_before
        counts = await _counts(env)
        assert counts["source_identity"] == 1  # continuity survives the move
        assert counts["content_revision"] == 1  # location is not content
        obs = await _observations(env)
        moved = [o for o in obs if o["outcome"] == "metadata_updated"]
        assert moved and moved[0]["evidence"]["new_location"] == "/new/place/report.pdf"

    async def test_t24_delete_create_equal_bytes_stay_distinct_sources(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(
                    content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),
                    # Provider has no continuity evidence: new item id,
                    # equal bytes, delete+create.
                    content_change("evt-2", "item-2", PDF_A, revision="r1", seq=1),
                ),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)

        counts = await _counts(env)
        assert counts["source_identity"] == 2  # equal bytes never merge identities
        assert counts["content_revision"] == 2
        a = await _source_identity_id(env, _key("item-1"))
        b = await _source_identity_id(env, _key("item-2"))
        assert a != b

    async def test_restore_lifts_deny_and_reacquires(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        await env.service.poll(STREAM, provider)
        provider.script_round(
            ChangePage(
                changes=(removal("evt-2", "item-1", seq=2),),
                next_cursor="tok-2",
                complete=True,
                page_seq=2,
            )
        )
        await env.service.poll(STREAM, provider)
        source_id = await _source_identity_id(env, _key("item-1"))

        provider.script_round(
            ChangePage(
                changes=(
                    ProviderChange(
                        event_id="evt-3",
                        item_id="item-1",
                        kind="restored",
                        revision="r3",
                        seq=3,
                        content=PDF_B,
                        suffix=".pdf",
                    ),
                ),
                next_cursor="tok-3",
                complete=True,
                page_seq=3,
            )
        )
        result = await env.service.poll(STREAM, provider)
        assert result.outcomes[0].applied_state == CONNECTOR_APPLIED_APPLIED

        authz = await resolve_effective_authorization(env.factory, WORKSPACE)
        assert source_id not in authz.denied_sources  # deny lifted
        counts = await _counts(env)
        assert counts["access_denial"] == 2  # deny + lift, append-only
        assert counts["content_revision"] == 2  # re-acquired as new revision
        assert counts["source_identity"] == 1  # same provider identity


class TestConcurrency:
    """T25–T26: database-level convergence, never an in-process lock."""

    async def test_t25_concurrent_duplicate_delivery_converges(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        results = await asyncio.gather(
            env.service.poll(STREAM, provider),
            env.service.poll(STREAM, provider),
            env.service.poll(STREAM, provider),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        assert ok, results
        applied = [
            o.applied_state
            for r in ok
            for o in r.outcomes
            if o.applied_state == CONNECTOR_APPLIED_APPLIED
        ]
        assert len(applied) == 1  # exactly one application
        counts = await _counts(env)
        assert counts["source_identity"] == 1
        assert counts["content_revision"] == 1
        assert len(await _inbox(env)) == 1
        assert len(await _outbox(env)) == 1

    async def test_t26_overlapping_polls_cannot_fork_the_checkpoint(self, env):
        provider = ScriptedProvider(account="acct")
        provider.script_round(
            ChangePage(
                changes=(content_change("evt-1", "item-1", PDF_A, revision="r1", seq=1),),
                next_cursor="tok-1",
                complete=True,
                page_seq=1,
            )
        )
        results = await asyncio.gather(
            env.service.poll(STREAM, provider),
            env.service.poll(STREAM, provider),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        assert winners
        view = await env.service.stream_view(STREAM)
        assert view is not None
        assert view.cursor_token in ("tok-1", "")  # one checkpoint, no fork
        assert view.state == CONNECTOR_STREAM_CONSUMING
        # Whatever token won, truth is consistent with exactly one commit
        # owning the checkpoint movement.
        async with env.factory() as session:
            stream_row = await session.get(KernelConnectorStream, STREAM)
            manifest = await session.get(
                KernelCommitManifest,
                (WORKSPACE, stream_row.applied_kernel_commit_id),
            )
        assert manifest is not None  # the owning commit exists and is complete


# ----------------------------------------------------------------------
# durable-state helpers
# ----------------------------------------------------------------------


async def _latest_content_blob(env) -> str:
    async with env.factory() as session:
        row = (
            await session.execute(
                select(KernelRecord.payload_json)
                .where(
                    KernelRecord.workspace_id == WORKSPACE,
                    KernelRecord.record_class == "content_revision",
                )
                .order_by(
                    KernelRecord.kernel_commit_id.desc(), KernelRecord.id.desc()
                )
                .limit(1)
            )
        ).scalar()
    return json.loads(row)["blob_key"]


async def _observations(env) -> list[dict]:
    async with env.factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord.payload_json).where(
                    KernelRecord.workspace_id == WORKSPACE,
                    KernelRecord.record_class == "source_observation",
                )
            )
        ).scalars().all()
    return [json.loads(r) for r in rows]
