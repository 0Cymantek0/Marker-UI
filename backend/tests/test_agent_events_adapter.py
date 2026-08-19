"""PR79B agent events adapter coverage."""

from __future__ import annotations

import json

import pytest

from app.agent_events import (
    EVENTS_RESULT_SCHEMA_VERSION,
    configure_events_runtime,
    read_agent_events,
)
from app.errors import UsageError
from app.kernel import events as kernel_events

pytestmark = pytest.mark.asyncio


@pytest.fixture
def events_runtime(payload_env):
    factory, _store, _commit_service = payload_env
    configure_events_runtime(factory)
    return factory


async def _append(factory, workspace: str, stream: str, count: int) -> None:
    for index in range(count):
        await kernel_events.append(
            factory,
            workspace_id=workspace,
            stream=stream,
            event_type="work.accepted",
            payload={"index": index},
        )


async def test_read_returns_ordered_events_with_resume_cursor(events_runtime):
    factory = events_runtime
    await _append(factory, "ws-events", "work", 3)

    page = await read_agent_events(workspace_id="ws-events")

    assert page["schema_version"] == EVENTS_RESULT_SCHEMA_VERSION
    assert page["workspace_id"] == "ws-events"
    assert page["stream"] == "work"
    assert [event["semantic_sequence"] for event in page["events"]] == [1, 2, 3]
    assert page["latest_sequence"] == 3
    assert page["next_after_sequence"] == 3
    assert page["has_more"] is False
    json.dumps(page)


async def test_resume_after_sequence_returns_only_missing_tail(events_runtime):
    factory = events_runtime
    await _append(factory, "ws-events", "work", 3)

    first = await read_agent_events(workspace_id="ws-events", limit=2)
    assert [event["semantic_sequence"] for event in first["events"]] == [1, 2]
    assert first["has_more"] is True
    assert first["next_after_sequence"] == 2

    # Client disconnects; more durable work happens with nobody connected.
    await _append(factory, "ws-events", "work", 2)

    resumed = await read_agent_events(
        workspace_id="ws-events", after_sequence=first["next_after_sequence"]
    )
    assert [event["semantic_sequence"] for event in resumed["events"]] == [3, 4, 5]
    assert resumed["latest_sequence"] == 5
    assert resumed["has_more"] is False


async def test_replaying_same_position_is_deterministic(events_runtime):
    factory = events_runtime
    await _append(factory, "ws-events", "work", 2)

    first = await read_agent_events(workspace_id="ws-events", after_sequence=1)
    second = await read_agent_events(workspace_id="ws-events", after_sequence=1)
    assert first == second


async def test_reads_are_isolated_per_workspace_and_stream(events_runtime):
    factory = events_runtime
    await _append(factory, "ws-a", "work", 2)
    await _append(factory, "ws-b", "work", 2)
    await _append(factory, "ws-a", "audit", 1)

    page_a = await read_agent_events(workspace_id="ws-a")
    page_b = await read_agent_events(workspace_id="ws-b")
    page_audit = await read_agent_events(workspace_id="ws-a", stream="audit")

    assert page_a["latest_sequence"] == 2
    assert page_b["latest_sequence"] == 2
    assert page_audit["latest_sequence"] == 1
    assert page_audit["events"][0]["payload"] == {"index": 0}


async def test_empty_stream_reports_zero_latest(events_runtime):
    page = await read_agent_events(workspace_id="ws-empty")
    assert page["events"] == []
    assert page["latest_sequence"] == 0
    assert page["has_more"] is False
    assert page["next_after_sequence"] == 0


async def test_invalid_arguments_are_usage_errors(events_runtime):
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id="")
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id=" padded ")
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id="x" * 129)
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id="ws", stream="Bad Stream")
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id="ws", after_sequence=-1)
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id="ws", limit=0)
    with pytest.raises(UsageError):
        await read_agent_events(workspace_id="ws", limit=501)
