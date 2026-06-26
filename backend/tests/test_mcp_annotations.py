"""MCP tool annotation tests (UCM-008)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_mcp_v1_split_tool_annotations():
    import app.mcp_server as mcp_server

    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}

    for name in ("marker_plan_url", "marker_convert_url", "marker_submit_url_job"):
        assert tools[name].annotations.openWorldHint is True
        assert tools[name].annotations.readOnlyHint is (name == "marker_plan_url")

    for name in ("marker_plan_local_file", "marker_convert_local_file", "marker_submit_local_job"):
        assert tools[name].annotations.openWorldHint is False

    for name in ("marker_cancel_job", "marker_delete_job", "marker_delete_setting"):
        assert tools[name].annotations.destructiveHint is True
        assert tools[name].annotations.readOnlyHint is False


@pytest.mark.asyncio
async def test_mcp_self_test_validates_tools_resources_and_prompts():
    import app.mcp_server as mcp_server

    payload = await mcp_server.marker_self_test(include_conversion=False)

    assert payload["tools_ok"] is True
    assert payload["resources_ok"] is True
    assert payload["prompts_ok"] is True
    assert "marker_convert_url" in payload["expected_tools"]
    assert "marker://docs/agent-guide" in payload["expected_resources"]
    assert "convert_for_rag" in payload["expected_prompts"]
