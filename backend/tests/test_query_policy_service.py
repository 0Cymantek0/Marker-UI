"""QueryPolicyService durable policy commits (PR78).

Every assignment / deny / lift lands through the kernel commit spine:
atomic, append-only, and visible to the resolver at commit time. The
deny→lift→deny cycle must chain onto history instead of colliding with
the first (semantically identical) denial.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.kernel.models import KernelRecord
from app.services.query_policy import QueryPolicyService

pytestmark = pytest.mark.asyncio


async def _policy(factory, service, workspace: str = "ws-a") -> QueryPolicyService:
    return QueryPolicyService(factory, service, workspace_id=workspace)


async def _records(
    factory, workspace: str, record_class: str
) -> list[tuple[str, int, dict]]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        KernelRecord.id,
                        KernelRecord.kernel_commit_id,
                        KernelRecord.payload_json,
                    )
                    .where(
                        KernelRecord.workspace_id == workspace,
                        KernelRecord.record_class == record_class,
                    )
                    .order_by(KernelRecord.kernel_commit_id.asc())
                )
            )
            .all()
        )
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


async def test_domain_assignment_commits_durable_record(payload_env: tuple) -> None:
    factory, store, service = payload_env
    policy = await _policy(factory, service)
    record_id = await policy.assign_source_domain(
        "src-1", "dom-alpha", basis={"operator": "test"}
    )
    rows = await _records(factory, "ws-a", "security_domain")
    assert len(rows) == 1
    committed_id, _commit, payload = rows[0]
    assert committed_id == record_id
    assert payload["source_ref"] == "src-1"
    assert payload["domain_key"] == "dom-alpha"
    # Audit basis is persisted but excluded from semantic identity.
    assert payload["assignment_basis"] == {"operator": "test"}


async def test_reassignment_is_a_second_record_not_a_mutation(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    policy = await _policy(factory, service)
    await policy.assign_source_domain("src-1", "dom-alpha")
    await policy.assign_source_domain("src-1", "dom-beta")
    rows = await _records(factory, "ws-a", "security_domain")
    assert [payload["domain_key"] for _, _, payload in rows] == [
        "dom-alpha",
        "dom-beta",
    ]


async def test_deny_lift_deny_chains_three_distinct_events(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    policy = await _policy(factory, service)
    first = await policy.deny_record("view-1", basis={"reason": "revoked"})
    lift = await policy.allow_record("view-1", basis={"reason": "restored"})
    second = await policy.deny_record("view-1")
    rows = await _records(factory, "ws-a", "access_denial")
    assert len(rows) == 3
    by_event = {record_id: payload for record_id, _, payload in rows}
    assert by_event[first]["denied"] is True
    assert by_event[first]["supersedes"] is None
    assert by_event[lift]["denied"] is False
    assert by_event[lift]["supersedes"] == first
    assert by_event[second]["denied"] is True
    assert by_event[second]["supersedes"] == lift
    # Three distinct semantic identities despite deny(True) appearing twice.
    assert len({first, lift, second}) == 3


async def test_deny_targets_are_separate_chains(payload_env: tuple) -> None:
    factory, store, service = payload_env
    policy = await _policy(factory, service)
    await policy.deny_domain("dom-alpha")
    await policy.deny_source("src-1")
    await policy.deny_record("view-1")
    rows = await _records(factory, "ws-a", "access_denial")
    kinds = sorted(payload["target_kind"] for _, _, payload in rows)
    assert kinds == ["domain", "record", "source"]
    assert all(payload["supersedes"] is None for _, _, payload in rows)


async def test_policy_commits_are_workspace_scoped(payload_env: tuple) -> None:
    factory, store, service = payload_env
    policy = await _policy(factory, service, workspace="ws-a")
    await policy.deny_record("view-1")
    other = await _records(factory, "ws-other", "access_denial")
    assert other == []


async def test_invalid_target_kind_rejected_before_commit(payload_env: tuple) -> None:
    factory, store, service = payload_env
    policy = await _policy(factory, service)
    with pytest.raises(ValueError, match="target_kind"):
        await policy.set_denial("workspace", "ws-a", denied=True)
    rows = await _records(factory, "ws-a", "access_denial")
    assert rows == []
