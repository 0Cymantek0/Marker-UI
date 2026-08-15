"""Transactional outbox contract tests (V3.2 PR64, plan workstream D).

Atomic successor intent: outbox rows appear exactly with their
authorizing commit, survive restart, and expose an honest at-least-once
claim/ack/release surface with deterministic dedupe identity.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitBatch, KernelCommitService, PHASE_PRE_COMMIT
from app.kernel.errors import InvalidOutboxIntentError
from app.kernel.models import KernelOutbox
from app.kernel.outbox import (
    OUTBOX_STATE_DONE,
    OUTBOX_STATE_IN_FLIGHT,
    OUTBOX_STATE_PENDING,
    OutboxIntent,
    ack,
    claim,
    compute_dedupe_key,
    list_outbox,
    release,
    reset_in_flight,
)
from app.kernel.records import ClaimAssertionRecord, ObservationRecord

pytestmark = pytest.mark.asyncio


def _assertion(claim_key: str = "outbox-1") -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=claim_key,
        subject="doc:report.pdf",
        predicate="contains_table",
        value=True,
    )


async def _fetch_outbox(factory: async_sessionmaker) -> list[KernelOutbox]:
    from sqlalchemy import select

    async with factory() as session:
        rows = (await session.execute(select(KernelOutbox))).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# validation and identity
# ---------------------------------------------------------------------------


def test_intent_validation_rejects_hostile_kinds_and_payloads() -> None:
    from app.kernel.outbox import validate_intent

    with pytest.raises(InvalidOutboxIntentError):
        validate_intent(OutboxIntent(work_kind="Bad Kind!", payload={}))
    with pytest.raises(InvalidOutboxIntentError):
        validate_intent(OutboxIntent(work_kind="ok", payload={"bad": float("nan")}))


def test_dedupe_key_is_deterministic_and_content_sensitive() -> None:
    base = dict(
        workspace_id="ws",
        kernel_commit_id=7,
        work_kind="materialize",
        payload_json='{"a":1}',
    )
    first = compute_dedupe_key(**base)
    assert first == compute_dedupe_key(**base)
    for changed in (
        {**base, "kernel_commit_id": 8},
        {**base, "work_kind": "index"},
        {**base, "payload_json": '{"a":2}'},
    ):
        assert first != compute_dedupe_key(**changed)


# ---------------------------------------------------------------------------
# atomic intent with commit
# ---------------------------------------------------------------------------


async def test_outbox_intent_is_atomic_with_commit(payload_env) -> None:
    factory, _store, service = payload_env
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(OutboxIntent(work_kind="materialize", payload={"target": "twin"}),),
        )
    )
    rows = await _fetch_outbox(factory)
    assert len(rows) == 1
    assert rows[0].workspace_id == "ws"
    assert rows[0].kernel_commit_id == receipt.kernel_commit_id
    assert rows[0].state == OUTBOX_STATE_PENDING
    assert rows[0].attempts == 0
    assert tuple(receipt.outbox_ids) == (rows[0].id,)


async def test_rolled_back_commit_leaves_no_outbox_work(payload_env) -> None:
    factory, _store, service = payload_env
    with pytest.raises(Exception):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(_assertion(),),
                outbox=(OutboxIntent(work_kind="materialize", payload={}),),
            ),
            _inject_fault_at=PHASE_PRE_COMMIT,
        )
    assert await _fetch_outbox(factory) == []


async def test_identical_intents_in_one_batch_dedupe_to_one_row(payload_env) -> None:
    factory, _store, service = payload_env
    intent = OutboxIntent(work_kind="index", payload={"index": "claims"})
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(intent, OutboxIntent(work_kind="index", payload={"index": "claims"})),
        )
    )
    rows = await _fetch_outbox(factory)
    assert len(rows) == 1
    assert receipt.outbox_ids == (rows[0].id,)


async def test_distinct_intents_stay_distinct(payload_env) -> None:
    factory, _store, service = payload_env
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(
                OutboxIntent(work_kind="materialize", payload={"a": 1}),
                OutboxIntent(work_kind="materialize", payload={"a": 2}),
                OutboxIntent(work_kind="index", payload={"a": 1}),
            ),
        )
    )
    rows = await _fetch_outbox(factory)
    assert len(rows) == 3
    assert len(receipt.outbox_ids) == 3


# ---------------------------------------------------------------------------
# claim / ack / release — at-least-once surface
# ---------------------------------------------------------------------------


async def test_claim_ack_lifecycle(payload_env) -> None:
    factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(OutboxIntent(work_kind="materialize", payload={}),),
        )
    )
    (row,) = await _fetch_outbox(factory)

    claimed = await claim(factory, row.id)
    assert claimed is not None and claimed.state == OUTBOX_STATE_IN_FLIGHT
    # Exactly-once claim within this stage: second claim loses.
    assert await claim(factory, row.id) is None

    assert await ack(factory, row.id) is True
    done = (await list_outbox(factory))[0]
    assert done.state == OUTBOX_STATE_DONE and done.completed_at is not None
    # Acking non-in-flight work is rejected.
    assert await ack(factory, row.id) is False


async def test_release_returns_work_to_pending_with_attempt_count(payload_env) -> None:
    factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(OutboxIntent(work_kind="materialize", payload={}),),
        )
    )
    (row,) = await _fetch_outbox(factory)
    await claim(factory, row.id)
    assert await release(factory, row.id) is True
    pending = (await list_outbox(factory))[0]
    assert pending.state == OUTBOX_STATE_PENDING and pending.attempts == 1
    # Releasing non-claimed work is rejected.
    assert await release(factory, row.id) is False


async def test_reset_in_flight_recovers_crash_stuck_work(payload_env) -> None:
    factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(
                OutboxIntent(work_kind="materialize", payload={}),
                OutboxIntent(work_kind="index", payload={}),
            ),
        )
    )
    rows = await _fetch_outbox(factory)
    await claim(factory, rows[0].id)
    await claim(factory, rows[1].id)
    await ack(factory, rows[1].id)

    reset = await reset_in_flight(factory)
    assert reset == 1  # only the stuck item, done work stays done
    states = {r.id: r.state for r in await _fetch_outbox(factory)}
    assert set(states.values()) == {OUTBOX_STATE_PENDING, OUTBOX_STATE_DONE}


async def test_pending_work_survives_process_restart(payload_env, tmp_path) -> None:
    factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(_assertion(),),
            outbox=(OutboxIntent(work_kind="materialize", payload={"x": 1}),),
        )
    )
    # A "new process": a fresh engine + session factory over the same file.
    db_file = tmp_path / "kernel.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        rows = await _fetch_outbox(factory2)
        assert len(rows) == 1 and rows[0].state == OUTBOX_STATE_PENDING
        claimed = await claim(factory2, rows[0].id)
        assert claimed is not None and claimed.kernel_commit_id == 1
    finally:
        await engine2.dispose()
