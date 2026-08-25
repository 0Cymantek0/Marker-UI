"""PR79B protocol-faithful transport conformance.

Spawns the real MCP server process over stdio and streamable HTTP against a
seeded durable database and drives the query/continuation and event-resume
contracts through the real MCP client SDK, including cross-principal cursor
rejection and resume across a full server restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_context_runtime_service import _publish

pytestmark = pytest.mark.asyncio

BACKEND_DIR = Path(__file__).resolve().parents[1]
QUERY_WORKSPACE = "ws-conf"
TOKEN_A = "pr79b-conformance-token-a"
TOKEN_B = "pr79b-conformance-token-b"
TOKENS_ENV = (
    f"{TOKEN_A}=queries:read events:read;{TOKEN_B}=queries:read events:read"
)


def _db_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _server_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MARKER_PRELOAD_MODELS"] = "false"
    env["ENCRYPTION_KEY"] = "cHJlNzliLXRyYW5zcG9ydC1jb25mb3JtYW5jZS1rZXk="
    env["MARKER_DATABASE_URL"] = _db_url(db_path)
    env.pop("MARKER_AUTH_TOKENS", None)
    env.pop("MARKER_MCP_AUTH_TOKEN", None)
    env.pop("MARKER_MCP_AUTH_SCOPES", None)
    env.pop("MARKER_MCP_TOOL_PROFILE", None)
    return env


def _http_env(db_path: Path) -> dict[str, str]:
    env = _server_env(db_path)
    env["MARKER_AUTH_TOKENS"] = TOKENS_ENV
    return env


async def _seed(db_path: Path, tmp_path: Path) -> None:
    from app.db_migration import upgrade_database
    from app.kernel.commit import KernelCommitService
    from app.kernel.events import append
    from app.kernel.payloads import LocalPayloadStore

    await upgrade_database(url=_db_url(db_path))
    engine = create_async_engine(
        _db_url(db_path), connect_args={"check_same_thread": False}
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = LocalPayloadStore(tmp_path / "payloads")
        commit_service = KernelCommitService(factory, payload_store=store)
        await _publish(factory, commit_service, QUERY_WORKSPACE)
        for index in range(3):
            await append(
                factory,
                workspace_id=QUERY_WORKSPACE,
                stream="work",
                event_type="work.accepted",
                payload={"index": index},
            )
    finally:
        await engine.dispose()


async def _append_direct(db_path: Path, workspace: str, count: int) -> None:
    from app.kernel.events import append

    engine = create_async_engine(
        _db_url(db_path), connect_args={"check_same_thread": False}
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        for index in range(count):
            await append(
                factory,
                workspace_id=workspace,
                stream="work",
                event_type="work.accepted",
                payload={"direct": True, "offset": index},
            )
    finally:
        await engine.dispose()


async def _call_tool(session, name: str, arguments: dict) -> dict:
    response = await session.call_tool(name, arguments)
    assert not response.isError, f"{name} failed: {response.content}"
    data = getattr(response, "structuredContent", None)
    if data is None:
        data = json.loads(response.content[0].text)
    return data


@asynccontextmanager
async def _authenticated_session(url: str, token: str):
    """Real streamable-HTTP MCP client session with a bearer principal."""

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # Startup has its own bounded readiness loop. Keep operation hangs bounded
    # while allowing modest scheduling delay under parallel workers.
    timeout = httpx.Timeout(30.0, connect=10.0, pool=10.0)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=timeout
    ) as http:
        async with streamable_http_client(url, http_client=http) as (
            read,
            write,
            _get_session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _fresh_query_args(page_size: int = 2) -> dict:
    return {
        "query": {
            "schema_version": "marker.query.v1",
            "workspace_id": QUERY_WORKSPACE,
            "operations": [
                {"op": "lexical_search", "text": "needle", "limit": 25}
            ],
        },
        "page_size": page_size,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _spawn_http(db_path: Path, port: int) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.cli",
        "mcp",
        "start",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--tool-profile",
        "minimal",
        cwd=str(BACKEND_DIR),
        env=_http_env(db_path),
    )
    for _ in range(240):
        if proc.returncode is not None:
            raise RuntimeError("streamable HTTP server exited during startup")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.5)
            continue
        writer.close()
        await writer.wait_closed()
        return proc
    proc.kill()
    raise RuntimeError("streamable HTTP server did not accept connections")


async def _stop_server(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def test_stdio_client_discovers_and_drives_full_query_chain(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db_path = tmp_path / "stdio-conf.db"
    await _seed(db_path, tmp_path)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"],
        cwd=str(BACKEND_DIR),
        env=_server_env(db_path),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"marker_query", "marker_events"}.issubset(names)
            query_tool = next(
                tool for tool in tools.tools if tool.name == "marker_query"
            )
            assert query_tool.outputSchema["type"] == "object"
            assert "status" in query_tool.outputSchema["properties"]

            first = await _call_tool(session, "marker_query", _fresh_query_args())
            assert first["schema_version"] == "marker.query_result.v1"
            assert first["status"] == "partial"
            assert first["next_cursor"]

            evidence = list(first["result"]["packet"]["evidence"])
            pages = [first["result"]["cumulative_budget"]["pages"]]
            cursor = first["next_cursor"]
            statuses = [first["status"]]
            while cursor is not None:
                page = await _call_tool(
                    session,
                    "marker_query",
                    {
                        "continuation": cursor,
                        "workspace_id": QUERY_WORKSPACE,
                        "page_size": 2,
                    },
                )
                statuses.append(page["status"])
                packet = (page["result"] or {}).get("packet")
                if packet:
                    evidence.extend(packet["evidence"])
                budget = (page["result"] or {}).get("cumulative_budget")
                if budget:
                    pages.append(budget["pages"])
                cursor = page["next_cursor"]

            assert statuses[-1] == "complete"
            keys = [(unit["record_id"], unit["node_id"]) for unit in evidence]
            assert len(keys) == 6
            assert len(set(keys)) == 6
            assert pages == [1, 2, 3, 4]

            events = await _call_tool(
                session, "marker_events", {"workspace_id": QUERY_WORKSPACE}
            )
            assert events["schema_version"] == "marker.events.v1"
            assert [e["semantic_sequence"] for e in events["events"]] == [1, 2, 3]
            assert events["has_more"] is False


async def test_http_transport_binds_cursors_to_authenticated_principals(
    tmp_path,
):
    db_path = tmp_path / "http-conf.db"
    await _seed(db_path, tmp_path)
    port = _free_port()
    proc = await _spawn_http(db_path, port)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with _authenticated_session(url, TOKEN_A) as session:
            first = await _call_tool(session, "marker_query", _fresh_query_args())
            assert first["status"] == "partial"
            token = first["next_cursor"]

        async with _authenticated_session(url, TOKEN_B) as session:
            hijack = await _call_tool(
                session,
                "marker_query",
                {
                    "continuation": token,
                    "workspace_id": QUERY_WORKSPACE,
                    "page_size": 2,
                },
            )
            assert hijack["status"] == "invalidated"
            assert hijack["error_code"] == "cursor_invalid"
            assert hijack["result"] is None

        async with _authenticated_session(url, TOKEN_A) as session:
            owner = await _call_tool(
                session,
                "marker_query",
                {
                    "continuation": token,
                    "workspace_id": QUERY_WORKSPACE,
                    "page_size": 2,
                },
            )
            assert owner["status"] == "partial"
            assert owner["next_cursor"]
    finally:
        await _stop_server(proc)


async def test_http_transport_rejects_unknown_bearer(tmp_path):
    db_path = tmp_path / "http-auth.db"
    await _seed(db_path, tmp_path)
    port = _free_port()
    proc = await _spawn_http(db_path, port)
    try:
        with pytest.raises(Exception):
            async with _authenticated_session(
                f"http://127.0.0.1:{port}/mcp", "not-a-configured-token"
            ) as session:
                await _call_tool(
                    session,
                    "marker_events",
                    {"workspace_id": QUERY_WORKSPACE},
                )
    finally:
        await _stop_server(proc)


async def test_http_event_resume_survives_disconnect_and_server_restart(
    tmp_path,
):
    from app.kernel import events as kernel_events

    db_path = tmp_path / "http-resume.db"
    await _seed(db_path, tmp_path)
    port = _free_port()
    proc = await _spawn_http(db_path, port)
    url = f"http://127.0.0.1:{port}/mcp"

    async def read_page(page_url: str, after: int) -> dict:
        async with _authenticated_session(page_url, TOKEN_A) as session:
            return await _call_tool(
                session,
                "marker_events",
                {
                    "workspace_id": QUERY_WORKSPACE,
                    "after_sequence": after,
                },
            )

    try:
        # Connected client receives through sequence 3 and records the
        # server-issued resume position.
        first = await read_page(url, 0)
        assert [e["semantic_sequence"] for e in first["events"]] == [1, 2, 3]
        resume_at = first["next_after_sequence"]
        assert resume_at == 3

        # Client disconnects; durable work continues with nobody connected.
        await _append_direct(db_path, QUERY_WORKSPACE, 2)
        engine = create_async_engine(
            _db_url(db_path), connect_args={"check_same_thread": False}
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            latest = await kernel_events.get_latest_sequence(
                factory, workspace_id=QUERY_WORKSPACE
            )
        finally:
            await engine.dispose()
        assert latest == 5

        # Reconnect: exactly the missing tail, in authoritative order.
        resumed = await read_page(url, resume_at)
        assert [e["semantic_sequence"] for e in resumed["events"]] == [4, 5]
        assert resumed["has_more"] is False

        # Cross-stream isolation: another workspace's events never leak.
        await _append_direct(db_path, "ws-other", 2)
        isolated = await read_page(url, resume_at)
        assert [e["semantic_sequence"] for e in isolated["events"]] == [4, 5]
    finally:
        await _stop_server(proc)

    # Server process dies; more durable work lands with no server at all.
    await _append_direct(db_path, QUERY_WORKSPACE, 2)

    # Restart over the same durable database and resume from 5.
    port = _free_port()
    proc = await _spawn_http(db_path, port)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        after_restart = await read_page(url, 5)
        assert [e["semantic_sequence"] for e in after_restart["events"]] == [6, 7]
        assert after_restart["latest_sequence"] == 7
        assert after_restart["has_more"] is False
    finally:
        await _stop_server(proc)
