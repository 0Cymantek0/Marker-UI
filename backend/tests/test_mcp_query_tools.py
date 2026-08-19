"""MCP tool-layer coverage for marker_query and marker_events (PR79B)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent_events import configure_events_runtime
from app.agent_query import configure_query_runtime, reset_query_runtime
from app.context_runtime import QUERY_SCHEMA_VERSION
from app.errors import UsageError
from tests.test_context_runtime_service import _publish

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tool_runtime(payload_env, monkeypatch):
    factory, _store, commit_service = payload_env
    monkeypatch.setenv("MARKER_QUERY_CURSOR_KEY", "pr79b-mcp-tool-test-key")
    configure_query_runtime(factory)
    configure_events_runtime(factory)
    yield factory, commit_service
    reset_query_runtime()


def _query_payload(workspace: str = "ws-mcp") -> dict:
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": [{"op": "lexical_search", "text": "needle", "limit": 25}],
    }


async def test_query_tool_requires_queries_read_scope(tool_runtime, monkeypatch):
    import app.security.auth as auth
    import app.mcp_server as mcp_server

    factory, commit_service = tool_runtime
    await _publish(factory, commit_service, "ws-mcp")
    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(scopes=[]))

    with pytest.raises(PermissionError, match="queries:read"):
        await mcp_server.marker_query(query=_query_payload(), page_size=2)


async def test_events_tool_requires_events_read_scope(tool_runtime, monkeypatch):
    import app.security.auth as auth
    import app.mcp_server as mcp_server

    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(scopes=[]))

    with pytest.raises(PermissionError, match="events:read"):
        await mcp_server.marker_events(workspace_id="ws-mcp")


async def test_stdio_caller_runs_unbound_query_chain(tool_runtime):
    import app.mcp_server as mcp_server

    factory, commit_service = tool_runtime
    await _publish(factory, commit_service, "ws-mcp")

    first = await mcp_server.marker_query(query=_query_payload(), page_size=2)
    assert first.status == "partial"
    assert first.next_cursor
    assert first.result["packet"]["schema_version"] == "marker.evidence_packet.v1"

    final = await mcp_server.marker_query(
        continuation=first.next_cursor,
        workspace_id="ws-mcp",
        page_size=10,
    )
    assert final.status == "complete"
    assert final.next_cursor is None


async def test_authenticated_caller_binds_cursor_to_token(tool_runtime, monkeypatch):
    import app.mcp_server as mcp_server
    import app.security.auth as auth

    factory, commit_service = tool_runtime
    await _publish(factory, commit_service, "ws-mcp")

    def token_for(principal_token: str):
        return SimpleNamespace(
            scopes=["queries:read"], token=principal_token, client_id="marker-mcp"
        )

    monkeypatch.setattr(
        auth, "get_access_token", lambda: token_for("token-a")
    )
    monkeypatch.setattr(
        mcp_server, "get_access_token", lambda: token_for("token-a")
    )

    first = await mcp_server.marker_query(query=_query_payload(), page_size=2)
    assert first.status == "partial"

    monkeypatch.setattr(
        mcp_server, "get_access_token", lambda: token_for("token-b")
    )
    hijack = await mcp_server.marker_query(
        continuation=first.next_cursor,
        workspace_id="ws-mcp",
        page_size=2,
    )
    assert hijack.status == "invalidated"
    assert hijack.error_code == "cursor_invalid"
    assert hijack.result is None

    monkeypatch.setattr(
        mcp_server, "get_access_token", lambda: token_for("token-a")
    )
    owner = await mcp_server.marker_query(
        continuation=first.next_cursor,
        workspace_id="ws-mcp",
        page_size=2,
    )
    assert owner.status == "partial"


async def test_query_tool_maps_contract_failure_to_usage_error(tool_runtime):
    import app.mcp_server as mcp_server

    payload = _query_payload()
    payload["operations"] = [{"op": "visual_search", "text": "needle"}]
    with pytest.raises(UsageError):
        await mcp_server.marker_query(query=payload)


async def test_events_tool_returns_structured_page(tool_runtime):
    from app.kernel import events as kernel_events

    import app.mcp_server as mcp_server

    factory, _commit = tool_runtime
    await kernel_events.append(
        factory,
        workspace_id="ws-mcp",
        stream="work",
        event_type="work.accepted",
        payload={"index": 0},
    )

    page = await mcp_server.marker_events(workspace_id="ws-mcp")
    assert page.schema_version == "marker.events.v1"
    assert page.latest_sequence == 1
    assert page.events[0]["event_type"] == "work.accepted"
    assert page.has_more is False

    resumed = await mcp_server.marker_events(
        workspace_id="ws-mcp", after_sequence=1
    )
    assert resumed.events == []
    assert resumed.next_after_sequence == 1


def test_streamable_http_enables_auth_from_static_token_map(monkeypatch):
    """A scoped MARKER_AUTH_TOKENS map alone must not fall back to no-auth."""

    import app.mcp_server as mcp_server

    called = {}

    def fake_run(*, transport: str):
        called["transport"] = transport

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)
    monkeypatch.setenv("MARKER_AUTH_TOKENS", "token-a=queries:read events:read")
    monkeypatch.delenv("MARKER_MCP_AUTH_TOKEN", raising=False)

    mcp_server.run(transport="streamable-http", host="127.0.0.1", port=8765)

    assert called == {"transport": "streamable-http"}
    assert mcp_server.mcp.settings.auth is not None
    assert mcp_server.mcp._token_verifier is not None

    verifier = mcp_server.mcp._token_verifier
    import asyncio

    access = asyncio.run(verifier.verify_token("token-a"))
    assert access is not None
    assert "queries:read" in access.scopes
    assert asyncio.run(verifier.verify_token("unknown-token")) is None


def test_streamable_http_stays_no_auth_on_loopback_without_tokens(monkeypatch):
    import app.mcp_server as mcp_server

    called = {}

    def fake_run(*, transport: str):
        called["transport"] = transport

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)
    monkeypatch.delenv("MARKER_AUTH_TOKENS", raising=False)
    monkeypatch.delenv("MARKER_MCP_AUTH_TOKEN", raising=False)

    mcp_server.run(transport="streamable-http", host="127.0.0.1", port=8765)

    assert called == {"transport": "streamable-http"}
    assert mcp_server.mcp.settings.auth is None
    assert mcp_server.mcp._token_verifier is None
