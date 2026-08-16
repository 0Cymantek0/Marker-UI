"""Fenced work ownership and exactly-once accepted publication contract
tests (V3.2 PR66).

The behavioral contract under test:

* authority is the durable fencing token — a superseded owner can
  never turn a late result into accepted state (T4);
* lease expiry permits takeover but never revives an old token;
* exactly one accepted publication exists per work identity, enforced
  by the database scope;
* retrying the same acceptance converges; a materially different one
  is a classified conflict (T6/T7);
* acknowledgement cannot precede accepted truth;
* workspace isolation holds; illegal transitions fail with typed
  errors instead of writing state.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel import fencing
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import (
    InvalidOwnerIdError,
    InvalidWorkLeaseError,
    InvalidWorkResultError,
    KernelError,
    PublicationConflictError,
    StaleFenceError,
    UnknownWorkError,
    UnknownWorkLeaseError,
)
from app.kernel.models import KernelPublication
from app.kernel.outbox import (
    OUTBOX_STATE_DONE,
    OUTBOX_STATE_IN_FLIGHT,
    OutboxIntent,
    ack as outbox_ack,
    claim as outbox_claim,
    list_outbox,
)
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

SHORT_LEASE = 0.05  # seconds; expires almost immediately


async def _new_work(payload_env, *, workspace_id: str = "ws", tag: str = "w") -> int:
    """Commit one outbox intent and return its durable work id."""
    _factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ClaimAssertionRecord(
                    claim_key=f"fence-{tag}",
                    subject="doc:x.pdf",
                    predicate="p",
                    value=1,
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"tag": tag}),),
        )
    )
    rows = await list_outbox(payload_env[0])
    return rows[-1].id  # audit order (id asc): the newest intent


async def _publication_count(factory) -> int:
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(KernelPublication))
        ).scalar_one()


# ---------------------------------------------------------------------------
# boundary validation
# ---------------------------------------------------------------------------


def test_owner_id_grammar() -> None:
    assert fencing.validate_owner_id("worker-a.1:b") == "worker-a.1:b"
    for bad in ("", "Worker", "-x", "worker a", "w" * 65, 5):
        with pytest.raises(InvalidOwnerIdError):
            fencing.validate_owner_id(bad)


def test_result_validation_rejects_non_canonical_values() -> None:
    with pytest.raises(InvalidWorkResultError):
        fencing.validate_result({"bad": float("nan")})
    assert fencing.validate_result({"ok": [1, 2]}) == '{"ok":[1,2]}'


def test_result_hash_is_deterministic_and_content_sensitive() -> None:
    first = fencing.compute_result_hash('{"a":1}')
    assert first == fencing.compute_result_hash('{"a":1}')
    assert first != fencing.compute_result_hash('{"a":2}')


def test_publication_id_is_deterministic_and_scope_sensitive() -> None:
    base = dict(workspace_id="ws", work_id=1, result_hash="h1")
    first = fencing.compute_publication_id(**base)
    assert first == fencing.compute_publication_id(**base)
    for changed in (
        {**base, "workspace_id": "other"},
        {**base, "work_id": 2},
        {**base, "result_hash": "h2"},
    ):
        assert first != fencing.compute_publication_id(**changed)


async def test_acquire_rejects_invalid_lease_and_unknown_work(payload_env) -> None:
    factory, _store, _service = payload_env
    with pytest.raises(InvalidWorkLeaseError):
        await fencing.acquire(factory, work_id=1, owner_id="w", lease_seconds=0)
    with pytest.raises(UnknownWorkError):
        await fencing.acquire(factory, work_id=999, owner_id="w")


# ---------------------------------------------------------------------------
# T1: single owner, normal acceptance, durable after reopen
# ---------------------------------------------------------------------------


async def test_single_owner_normal_acceptance_survives_reopen(
    payload_env, tmp_path
) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")

    outcome = await fencing.accept(
        factory, work_id=work_id, fencing_token=1, result={"pages": 3}
    )
    assert not outcome.already_accepted
    assert outcome.publication.fencing_token == 1
    assert outcome.publication.result == {"pages": 3}

    # A "new process" over the same file reconstructs the same truth.
    db_file = tmp_path / "kernel.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        reopened = await fencing.get_publication(factory2, work_id=work_id)
        assert reopened == outcome.publication
        lease = await fencing.get_lease(factory2, work_id=work_id)
        assert lease.state == fencing.LEASE_STATE_ACCEPTED
    finally:
        await engine2.dispose()


# ---------------------------------------------------------------------------
# ownership transitions
# ---------------------------------------------------------------------------


async def test_second_owner_cannot_acquire_valid_lease(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    first = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    assert first is not None and first.fencing_token == 1
    assert await fencing.acquire(factory, work_id=work_id, owner_id="worker-b") is None


async def test_same_owner_reacquire_renews_without_advancing_token(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    first = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=60.0
    )
    renewed = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=60.0
    )
    assert renewed is not None
    assert renewed.fencing_token == first.fencing_token  # duplicate delivery is safe
    assert renewed.lease_expires_at >= first.lease_expires_at


async def test_release_vacates_and_immediately_fences_owner(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    # Wrong token or wrong owner cannot vacate.
    assert not await fencing.release(
        factory, work_id=work_id, owner_id="worker-a", fencing_token=7
    )
    assert await fencing.release(
        factory, work_id=work_id, owner_id="worker-a", fencing_token=1
    )
    lease = await fencing.get_lease(factory, work_id=work_id)
    assert lease.state == fencing.LEASE_STATE_RELEASED
    assert lease.fencing_token == 2
    # The releasing owner is stale the instant it vacates.
    with pytest.raises(StaleFenceError):
        await fencing.accept(
            factory, work_id=work_id, fencing_token=1, result={"x": 1}
        )
    # ...and a successor acquires forward from the vacated state.
    successor = await fencing.acquire(factory, work_id=work_id, owner_id="worker-b")
    assert successor is not None and successor.fencing_token == 3


async def test_cannot_acquire_accepted_or_done_work(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    await fencing.accept(factory, work_id=work_id, fencing_token=1, result={"r": 1})
    assert await fencing.acquire(factory, work_id=work_id, owner_id="worker-b") is None

    # Done outbox work is not claimable either.
    work_id2 = await _new_work(payload_env, tag="w2")
    await outbox_claim(factory, work_id2)
    await outbox_ack(factory, work_id2)
    assert await fencing.acquire(factory, work_id=work_id2, owner_id="worker-b") is None


# ---------------------------------------------------------------------------
# T3/T4/T10: failover fences the previous owner
# ---------------------------------------------------------------------------


async def test_takeover_after_expiry_advances_the_durable_fence(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    await asyncio.sleep(SHORT_LEASE + 0.02)
    successor = await fencing.acquire(factory, work_id=work_id, owner_id="worker-b")
    assert successor is not None
    assert successor.fencing_token == 2  # strictly newer authority (T3)
    lease = await fencing.get_lease(factory, work_id=work_id)
    assert lease.owner_id == "worker-b" and lease.state == fencing.LEASE_STATE_LEASED


async def test_stale_worker_finishing_late_cannot_publish(payload_env) -> None:
    """The core PR66 acceptance signal (T4/T10): worker A is superseded
    (here by lease-lapse takeover while A is still 'running', not by an
    orderly release), finishes its computation, and is fenced."""
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    stale = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    await asyncio.sleep(SHORT_LEASE + 0.02)
    current = await fencing.acquire(factory, work_id=work_id, owner_id="worker-b")

    with pytest.raises(StaleFenceError) as exc_info:
        await fencing.accept(
            factory, work_id=work_id, fencing_token=stale.fencing_token, result={"a": 1}
        )
    assert exc_info.value.submitted_token == 1
    assert exc_info.value.current_token == 2
    assert await _publication_count(factory) == 0

    outcome = await fencing.accept(
        factory, work_id=work_id, fencing_token=current.fencing_token, result={"b": 1}
    )
    assert not outcome.already_accepted
    assert await _publication_count(factory) == 1


async def test_expired_but_unsuperseded_owner_still_current(payload_env) -> None:
    """Wall-clock expiry alone is not authority: nobody moved the fence,
    so the still-current token may accept."""
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    lease = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    await asyncio.sleep(SHORT_LEASE + 0.02)
    outcome = await fencing.accept(
        factory, work_id=work_id, fencing_token=lease.fencing_token, result={"late": 1}
    )
    assert not outcome.already_accepted


# ---------------------------------------------------------------------------
# T6/T7: idempotent retry and conflicting duplicate
# ---------------------------------------------------------------------------


async def test_same_result_retry_converges_to_existing_publication(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    first = await fencing.accept(
        factory, work_id=work_id, fencing_token=1, result={"r": "same"}
    )
    second = await fencing.accept(
        factory, work_id=work_id, fencing_token=1, result={"r": "same"}
    )
    assert second.already_accepted
    assert second.publication == first.publication
    assert await _publication_count(factory) == 1


async def test_conflicting_duplicate_is_rejected_without_changing_state(
    payload_env,
) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    accepted = await fencing.accept(
        factory, work_id=work_id, fencing_token=1, result={"winner": True}
    )
    with pytest.raises(PublicationConflictError) as exc_info:
        await fencing.accept(
            factory, work_id=work_id, fencing_token=1, result={"winner": False}
        )
    assert exc_info.value.existing_result_hash == accepted.publication.result_hash
    assert await _publication_count(factory) == 1
    unchanged = await fencing.get_publication(factory, work_id=work_id)
    assert unchanged.result == {"winner": True}


async def test_accept_without_ever_acquiring_is_illegal(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    with pytest.raises(UnknownWorkLeaseError):
        await fencing.accept(factory, work_id=work_id, fencing_token=1, result={})


# ---------------------------------------------------------------------------
# fenced acknowledgement
# ---------------------------------------------------------------------------


async def test_completion_requires_accepted_truth_and_current_fence(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await outbox_claim(factory, work_id)
    lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")

    # Ack cannot become durable before the accepted result it represents.
    assert not await fencing.complete_work(
        factory, work_id=work_id, fencing_token=lease.fencing_token
    )
    rows = {r.id: r.state for r in await list_outbox(factory)}
    assert rows[work_id] == OUTBOX_STATE_IN_FLIGHT

    await fencing.accept(
        factory, work_id=work_id, fencing_token=lease.fencing_token, result={"r": 1}
    )
    assert await fencing.complete_work(
        factory, work_id=work_id, fencing_token=lease.fencing_token
    )
    rows = {r.id: r.state for r in await list_outbox(factory)}
    assert rows[work_id] == OUTBOX_STATE_DONE
    # Idempotent-shaped: a second completion is a no-op, matching the
    # PR64 ack contract (False for non-in-flight).
    assert not await fencing.complete_work(
        factory, work_id=work_id, fencing_token=lease.fencing_token
    )


async def test_stale_worker_cannot_acknowledge_either(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await outbox_claim(factory, work_id)
    await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    await asyncio.sleep(SHORT_LEASE + 0.02)
    current = await fencing.acquire(factory, work_id=work_id, owner_id="worker-b")
    await fencing.accept(
        factory, work_id=work_id, fencing_token=current.fencing_token, result={"b": 1}
    )
    # The superseded owner must not be able to ack through the new path.
    assert not await fencing.complete_work(factory, work_id=work_id, fencing_token=1)
    rows = {r.id: r.state for r in await list_outbox(factory)}
    assert rows[work_id] == OUTBOX_STATE_IN_FLIGHT
    assert await fencing.complete_work(
        factory, work_id=work_id, fencing_token=current.fencing_token
    )


# ---------------------------------------------------------------------------
# T13: workspace isolation
# ---------------------------------------------------------------------------


async def test_identical_work_in_two_workspaces_stays_isolated(payload_env) -> None:
    factory, _store, service = payload_env
    for i, ws in enumerate(("ws-one", "ws-two")):
        await service.commit(
            KernelCommitBatch(
                workspace_id=ws,
                records=(
                    ClaimAssertionRecord(
                        claim_key=f"iso-{ws}",
                        subject="doc:x.pdf",
                        predicate="p",
                        value=1,
                    ),
                ),
                outbox=(
                    OutboxIntent(work_kind="materialize", payload={"same": "payload"}),
                ),
            )
        )
    rows = {r.workspace_id: r.id for r in await list_outbox(factory)}
    assert set(rows) == {"ws-one", "ws-two"}

    for ws, work_id in rows.items():
        lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-x")
        outcome = await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"workspace": ws},
        )
        assert not outcome.already_accepted
        assert outcome.publication.workspace_id == ws

    assert await _publication_count(factory) == 2
    one = await fencing.get_publication(factory, work_id=rows["ws-one"])
    assert one.result == {"workspace": "ws-one"}
    # A workspace filter that disagrees with the publication's own
    # workspace resolves to nothing (isolation guard).
    assert (
        await fencing.get_publication(factory, work_id=rows["ws-one"], workspace_id="ws-two")
        is None
    )


# ---------------------------------------------------------------------------
# illegal fault-phase input
# ---------------------------------------------------------------------------


async def test_unknown_fault_phase_is_rejected(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env)
    await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    with pytest.raises(KernelError):
        await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=1,
            result={},
            _inject_fault_at="not-a-phase",
        )
