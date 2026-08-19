"""PR79B agent query adapter coverage."""

from __future__ import annotations

import json

import pytest

from app.agent_query import (
    QUERY_RESULT_SCHEMA_VERSION,
    configure_query_runtime,
    reset_query_runtime,
    run_agent_query,
)
from app.context_runtime import QUERY_SCHEMA_VERSION
from app.errors import UsageError
from app.kernel.commit import KernelCommitBatch
from tests.test_context_runtime_authorization import _epoch
from tests.test_context_runtime_service import _publish

pytestmark = pytest.mark.asyncio


@pytest.fixture
def query_runtime(payload_env, monkeypatch):
    factory, _store, commit_service = payload_env
    monkeypatch.setenv("MARKER_QUERY_CURSOR_KEY", "pr79b-adapter-test-key")
    configure_query_runtime(factory)
    yield factory, commit_service
    reset_query_runtime()


def _raw_request(workspace: str = "ws-agent") -> dict:
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": [{"op": "lexical_search", "text": "needle", "limit": 25}],
    }


async def _drive_chain(
    first: dict, workspace: str, page_size: int
) -> tuple[list[str], list[dict], list[int]]:
    statuses = [first["status"]]
    evidence = list(first["result"]["packet"]["evidence"])
    pages = [first["result"]["cumulative_budget"]["pages"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = await run_agent_query(
            continuation=cursor, workspace_id=workspace, page_size=page_size
        )
        statuses.append(page["status"])
        packet = (page["result"] or {}).get("packet")
        if packet:
            evidence.extend(packet["evidence"])
        budget = (page["result"] or {}).get("cumulative_budget")
        if budget:
            pages.append(budget["pages"])
        cursor = page["next_cursor"]
        if page["status"] == "complete":
            break
    return statuses, evidence, pages


async def test_fresh_partial_query_returns_typed_envelope(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    envelope = await run_agent_query(query=_raw_request(), page_size=2)

    assert envelope["schema_version"] == QUERY_RESULT_SCHEMA_VERSION
    assert envelope["status"] == "partial"
    assert envelope["next_cursor"]
    packet = envelope["result"]["packet"]
    assert packet["schema_version"] == "marker.evidence_packet.v1"
    assert packet["publication_status"] == "published"
    assert packet["evidence"]
    assert envelope["result"]["cumulative_budget"]["pages"] == 1
    # The envelope must be plain JSON-serializable data.
    json.dumps(envelope)


async def test_continuation_chain_reaches_terminal_without_gaps(query_runtime):
    factory, commit_service = query_runtime
    publication = await _publish(factory, commit_service, "ws-agent")

    first = await run_agent_query(query=_raw_request(), page_size=2)
    statuses, evidence, pages = await _drive_chain(first, "ws-agent", 2)

    assert statuses[0] == "partial"
    assert statuses[-1] == "complete"
    keys = [(unit["record_id"], unit["node_id"]) for unit in evidence]
    assert len(keys) == 6
    assert len(set(keys)) == 6
    assert pages == [1, 2, 3, 4]
    publication_ids = {unit["publication_set_id"] for unit in evidence}
    assert publication_ids == {publication.publication_set_id}


async def test_terminal_outcome_carries_no_cursor(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    envelope = await run_agent_query(query=_raw_request(), page_size=10)

    assert envelope["status"] == "complete"
    assert envelope["next_cursor"] is None


async def test_unpublished_workspace_returns_complete_unpublished_packet(
    query_runtime,
):
    envelope = await run_agent_query(query=_raw_request("ws-empty"), page_size=10)

    assert envelope["status"] == "complete"
    assert envelope["next_cursor"] is None
    assert envelope["result"]["packet"]["publication_status"] == "unpublished"


async def test_chain_stays_on_original_publication_after_head_switch(query_runtime):
    factory, commit_service = query_runtime
    first_publication = await _publish(factory, commit_service, "ws-agent")

    first = await run_agent_query(query=_raw_request(), page_size=2)
    await _publish(factory, commit_service, "ws-agent", "-new", advance=False)

    statuses, evidence, _pages = await _drive_chain(first, "ws-agent", 2)

    assert statuses[-1] == "complete"
    publication_ids = {unit["publication_set_id"] for unit in evidence}
    assert publication_ids == {first_publication.publication_set_id}


async def test_contract_failures_are_usage_errors(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    unsupported = _raw_request()
    unsupported["operations"] = [
        {"op": "vector_search", "text": "needle", "limit": 5}
    ]
    with pytest.raises(UsageError):
        await run_agent_query(query=unsupported)

    bad_version = _raw_request()
    bad_version["schema_version"] = "marker.query.v0"
    with pytest.raises(UsageError):
        await run_agent_query(query=bad_version)

    with pytest.raises(UsageError):
        await run_agent_query()
    with pytest.raises(UsageError):
        await run_agent_query(query=_raw_request(), continuation="token")
    with pytest.raises(UsageError):
        await run_agent_query(continuation="token")
    mismatch = _raw_request()
    with pytest.raises(UsageError):
        await run_agent_query(query=mismatch, workspace_id="ws-other")


async def test_tampered_and_replayed_cursors_fail_closed(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    first = await run_agent_query(query=_raw_request(), page_size=2)
    token = first["next_cursor"]

    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    outcome = await run_agent_query(
        continuation=tampered, workspace_id="ws-agent"
    )
    assert outcome["status"] == "invalidated"
    assert outcome["error_code"] == "cursor_invalid"
    assert outcome["result"] is None
    assert outcome["next_cursor"] is None

    wrong_workspace = await run_agent_query(
        continuation=token, workspace_id="ws-other"
    )
    assert wrong_workspace["status"] == "invalidated"
    assert wrong_workspace["error_code"] == "cursor_invalid"

    second = await run_agent_query(
        continuation=token, workspace_id="ws-agent", page_size=2
    )
    assert second["status"] == "partial"
    replay = await run_agent_query(
        continuation=token, workspace_id="ws-agent", page_size=2
    )
    assert replay["status"] == "invalidated"
    assert replay["error_code"] == "cursor_invalid"
    assert replay["result"] is None


async def test_authenticated_principal_binding_survives_transport(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    first = await run_agent_query(
        query=_raw_request(), page_size=2, principal_id="principal-a"
    )
    assert first["status"] == "partial"
    token = first["next_cursor"]

    hijack = await run_agent_query(
        continuation=token, workspace_id="ws-agent", principal_id="principal-b"
    )
    assert hijack["status"] == "invalidated"
    assert hijack["error_code"] == "cursor_invalid"
    assert hijack["result"] is None
    assert hijack["next_cursor"] is None

    unauthenticated = await run_agent_query(
        continuation=token, workspace_id="ws-agent"
    )
    assert unauthenticated["status"] == "invalidated"
    assert unauthenticated["error_code"] == "cursor_invalid"

    owner = await run_agent_query(
        continuation=token,
        workspace_id="ws-agent",
        page_size=2,
        principal_id="principal-a",
    )
    assert owner["status"] == "partial"
    assert owner["next_cursor"]


async def test_policy_epoch_change_invalidates_mid_chain(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    first = await run_agent_query(query=_raw_request(), page_size=2)
    token = first["next_cursor"]
    await commit_service.commit(
        KernelCommitBatch(workspace_id="ws-agent", records=(_epoch(1, 7),))
    )

    outcome = await run_agent_query(
        continuation=token, workspace_id="ws-agent", page_size=2
    )
    assert outcome["status"] == "invalidated"
    assert outcome["error_code"] == "authorization_changed"
    assert outcome["result"] is None
    assert outcome["next_cursor"] is None


async def test_cursor_key_rotation_rejects_old_tokens(query_runtime, monkeypatch):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    first = await run_agent_query(query=_raw_request(), page_size=2)
    token = first["next_cursor"]

    monkeypatch.setenv("MARKER_QUERY_CURSOR_KEY", "pr79b-rotated-key")
    reset_query_runtime()
    outcome = await run_agent_query(
        continuation=token, workspace_id="ws-agent", page_size=2
    )
    assert outcome["status"] == "invalidated"
    assert outcome["error_code"] == "cursor_invalid"


async def test_encryption_key_fallback_derives_cursor_key(payload_env, monkeypatch):
    factory, _store, commit_service = payload_env
    monkeypatch.setenv("MARKER_QUERY_CURSOR_KEY", "")
    monkeypatch.setenv("ENCRYPTION_KEY", "pr79b-fallback-encryption-key")
    configure_query_runtime(factory)
    try:
        await _publish(factory, commit_service, "ws-agent")
        first = await run_agent_query(query=_raw_request(), page_size=2)
        assert first["status"] == "partial"

        reset_query_runtime()
        second = await run_agent_query(
            continuation=first["next_cursor"],
            workspace_id="ws-agent",
            page_size=10,
        )
        assert second["status"] == "complete"
    finally:
        reset_query_runtime()


async def test_high_assurance_without_partition_fails_closed(query_runtime):
    request = _raw_request()
    request["assurance"] = "high"

    envelope = await run_agent_query(query=request, page_size=10)

    assert envelope["status"] == "policy_fail_closed"
    assert envelope["error_code"] == "policy_fail_closed"
    assert envelope["result"] is None
    assert envelope["next_cursor"] is None


def test_outcome_envelope_represents_every_backend_status():
    from app.agent_query import _outcome_envelope
    from app.context_runtime.continuation import ContinuationOutcome

    statuses = [
        "complete",
        "partial",
        "invalidated",
        "stale",
        "loop_limit",
        "policy_fail_closed",
        "execution_failure",
    ]
    for status in statuses:
        outcome = ContinuationOutcome(
            status=status,
            result=(
                {"cumulative_budget": {"pages": 1}}
                if status in {"complete", "partial", "loop_limit"}
                else None
            ),
            next_cursor="cursor-token" if status == "partial" else None,
            reason="reason" if status != "complete" else None,
            error_code="code" if status != "complete" else None,
        )
        envelope = _outcome_envelope(outcome)
        assert envelope["schema_version"] == QUERY_RESULT_SCHEMA_VERSION
        assert envelope["status"] == status
        assert (envelope["next_cursor"] is not None) == (status == "partial")
        assert isinstance(envelope["result"], dict) == (
            status in {"complete", "partial", "loop_limit"}
        )


async def test_caller_context_cannot_impersonate_transport_principal(query_runtime):
    factory, commit_service = query_runtime
    await _publish(factory, commit_service, "ws-agent")

    request = _raw_request()
    request["context"] = {"security_context_id": "principal-a"}
    issued = await run_agent_query(
        query=request, page_size=2, principal_id="principal-b"
    )
    assert issued["status"] == "partial"

    # The context hint names principal-a, but the cursor is bound to the
    # authenticated caller principal-b; principal-a cannot resume it even
    # though the packet context claims that identity.
    hijack = await run_agent_query(
        continuation=issued["next_cursor"],
        workspace_id="ws-agent",
        principal_id="principal-a",
    )
    assert hijack["status"] == "invalidated"
    assert hijack["error_code"] == "cursor_invalid"

    owner = await run_agent_query(
        continuation=issued["next_cursor"],
        workspace_id="ws-agent",
        page_size=2,
        principal_id="principal-b",
    )
    assert owner["status"] == "partial"
