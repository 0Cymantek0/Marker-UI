"""Fencing acceptance crash-boundary tests (V3.2 PR66).

Every fault phase pins one side of the binary crash truth around the
acceptance linearization point (the commit of the transaction that
inserted ``kernel_publications`` and flipped the lease to accepted):

* before/at insert (``fence_validated``, ``publication_inserted``) →
  rollback: no accepted publication exists and the work stays
  recoverable (T8);
* after commit (``post_commit``) → the caller observes failure while
  the database holds exactly one durable accepted publication; the
  retry converges to it (T9);
* a takeover that happens while the old worker is mid-flight leaves
  the old worker fenced at every crash boundary (T10).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel import fencing
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import InjectedFaultError, StaleFenceError
from app.kernel.outbox import OUTBOX_STATE_IN_FLIGHT, OutboxIntent, claim, list_outbox
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

SHORT_LEASE = 0.05


async def _new_work(payload_env, *, tag: str) -> int:
    _factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(
                ClaimAssertionRecord(
                    claim_key=f"fault-{tag}",
                    subject="doc:x.pdf",
                    predicate="p",
                    value=1,
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"tag": tag}),),
        )
    )
    rows = await list_outbox(payload_env[0])
    return rows[-1].id


async def _count_publications(factory) -> int:
    from sqlalchemy import func, select

    from app.kernel.models import KernelPublication

    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(KernelPublication))
        ).scalar_one()


async def _outbox_state(factory, work_id: int) -> str:
    rows = {r.id: r.state for r in await list_outbox(factory)}
    return rows[work_id]


# ---------------------------------------------------------------------------
# T8: crash before the acceptance linearization point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", sorted(fencing.FAULT_PHASES - {fencing.PHASE_POST_COMMIT}))
async def test_crash_before_linearization_leaves_no_publication(
    payload_env, phase
) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="pre")
    await claim(factory, work_id)
    lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")

    with pytest.raises(InjectedFaultError):
        await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"r": 1},
            _inject_fault_at=phase,
        )

    # Binary truth side A: nothing was accepted...
    assert await _count_publications(factory) == 0
    resumed = await fencing.get_lease(factory, work_id=work_id)
    assert resumed.state == fencing.LEASE_STATE_LEASED
    assert resumed.fencing_token == lease.fencing_token
    # ...and the work remains recoverable: a clean retry accepts.
    outcome = await fencing.accept(
        factory, work_id=work_id, fencing_token=lease.fencing_token, result={"r": 1}
    )
    assert not outcome.already_accepted
    assert await _count_publications(factory) == 1


# ---------------------------------------------------------------------------
# T9: crash after the linearization point, before the caller observes
# ---------------------------------------------------------------------------


async def test_crash_after_linearization_keeps_exactly_one_publication(
    payload_env,
) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="post")
    await claim(factory, work_id)
    lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")

    with pytest.raises(InjectedFaultError) as exc_info:
        await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"r": "committed"},
            _inject_fault_at=fencing.PHASE_POST_COMMIT,
        )
    assert exc_info.value.phase == fencing.PHASE_POST_COMMIT

    # Binary truth side B: the database holds the accepted publication
    # even though the caller saw a failure.
    assert await _count_publications(factory) == 1
    stored = await fencing.get_publication(factory, work_id=work_id)
    assert stored.result == {"r": "committed"}

    # The ambiguous-outcome retry converges instead of duplicating.
    retry = await fencing.accept(
        factory,
        work_id=work_id,
        fencing_token=lease.fencing_token,
        result={"r": "committed"},
    )
    assert retry.already_accepted
    assert retry.publication == stored
    assert await _count_publications(factory) == 1


async def test_post_commit_fault_truth_reconstructs_after_restart(
    payload_env, tmp_path
) -> None:
    """Restart continuity for the T9 scenario: a fresh process finds the
    accepted publication, the accepted lease, and can complete the
    outbox delivery the crashed caller never acknowledged."""
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="restart")
    await claim(factory, work_id)
    lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    with pytest.raises(InjectedFaultError):
        await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"r": 1},
            _inject_fault_at=fencing.PHASE_POST_COMMIT,
        )

    db_file = tmp_path / "kernel.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        reopened = await fencing.get_publication(factory2, work_id=work_id)
        assert reopened is not None and reopened.result == {"r": 1}
        resumed_lease = await fencing.get_lease(factory2, work_id=work_id)
        assert resumed_lease.state == fencing.LEASE_STATE_ACCEPTED
        # Acknowledgement was never durable; the row is still in flight
        # and the recovered worker completes behind accepted truth.
        assert await _outbox_state(factory2, work_id) == OUTBOX_STATE_IN_FLIGHT
        assert await fencing.complete_work(
            factory2, work_id=work_id, fencing_token=lease.fencing_token
        )
        assert await _outbox_state(factory2, work_id) == "done"
    finally:
        await engine2.dispose()


# ---------------------------------------------------------------------------
# T10: takeover while the old worker is mid-flight
# ---------------------------------------------------------------------------


async def test_takeover_during_old_worker_flight_fences_every_crash_boundary(
    payload_env,
) -> None:
    """Worker A is interrupted at each crash boundary; worker B takes
    over from durable state (never via an orderly release from A). A
    stays fenced no matter which side of its own boundary it resumes
    on."""
    factory, _store, _service = payload_env

    # A crashes before acceptance; B takes over and accepts first.
    work_id = await _new_work(payload_env, tag="t10a")
    await claim(factory, work_id)
    stale = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    with pytest.raises(InjectedFaultError):
        await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=stale.fencing_token,
            result={"a": 1},
            _inject_fault_at=fencing.PHASE_PUBLICATION_INSERTED,
        )
    await asyncio.sleep(SHORT_LEASE + 0.02)
    current = await fencing.acquire(factory, work_id=work_id, owner_id="worker-b")
    assert current.fencing_token == stale.fencing_token + 1
    await fencing.accept(
        factory, work_id=work_id, fencing_token=current.fencing_token, result={"b": 1}
    )
    # A resumes after its rollback: fenced, accepted truth unchanged.
    with pytest.raises(StaleFenceError):
        await fencing.accept(
            factory, work_id=work_id, fencing_token=stale.fencing_token, result={"a": 1}
        )
    assert (await fencing.get_publication(factory, work_id=work_id)).result == {"b": 1}

    # A crashes AFTER its acceptance committed... but only after B had
    # already superseded it — impossible to accept under a stale token:
    # prove the fence rejects A even at its strongest moment.
    work_id2 = await _new_work(payload_env, tag="t10b")
    await claim(factory, work_id2)
    stale2 = await fencing.acquire(
        factory, work_id=work_id2, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    await asyncio.sleep(SHORT_LEASE + 0.02)
    current2 = await fencing.acquire(factory, work_id=work_id2, owner_id="worker-b")
    assert current2.fencing_token == stale2.fencing_token + 1
    with pytest.raises(StaleFenceError):
        await fencing.accept(
            factory,
            work_id=work_id2,
            fencing_token=stale2.fencing_token,
            result={"a": 1},
            _inject_fault_at=fencing.PHASE_POST_COMMIT,
        )
    assert await _count_publications(factory) == 1  # only the first work accepted
