"""MCP v1 resources, prompts, and split tool tests (UCM-008)."""

from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_mcp_registers_v1_tools_resources_templates_and_prompts():
    import app.mcp_server as mcp_server

    tools = {tool.name for tool in await mcp_server.mcp.list_tools()}
    resources = {str(resource.uri) for resource in await mcp_server.mcp.list_resources()}
    templates = {str(template.uriTemplate) for template in await mcp_server.mcp.list_resource_templates()}
    prompts = {prompt.name for prompt in await mcp_server.mcp.list_prompts()}

    assert set(mcp_server.MCP_V1_TOOL_NAMES).issubset(tools)
    assert set(mcp_server.MCP_PROMPT_NAMES) == prompts
    assert "marker://capabilities" in resources
    assert "marker://jobs" in resources
    assert "marker://docs/options" in resources
    assert "marker://jobs/{job_id}/manifest" in templates
    assert "marker://outputs/{output_id}/manifest" in templates


@pytest.mark.asyncio
async def test_mcp_static_resources_are_readable():
    import app.mcp_server as mcp_server

    guide = await mcp_server.mcp.read_resource("marker://docs/agent-guide")
    options = await mcp_server.mcp.read_resource("marker://docs/options")
    capabilities = await mcp_server.mcp.read_resource("marker://capabilities")

    assert "Marker Agent Guide" in guide[0].content
    options_payload = json.loads(options[0].content)
    assert options_payload["schema_version"] == "marker.agent_contract.v1"
    assert any(item["name"] == "output_format" for item in options_payload["options"])
    capabilities_payload = json.loads(capabilities[0].content)
    assert "marker_convert_url" in capabilities_payload["tools"]
    assert "marker://docs/agent-guide" in capabilities_payload["resources"]
    assert "convert_for_rag" in capabilities_payload["prompts"]


@pytest.mark.asyncio
async def test_mcp_prompt_template_renders_workflow_text():
    import app.mcp_server as mcp_server

    result = await mcp_server.mcp.get_prompt(
        "convert_for_rag",
        {
            "input_path": "/workspace/report.pdf",
            "output_dir": "/workspace/out",
            "quality": "auto",
            "allow_cloud_vlm": False,
        },
    )

    text = result.messages[0].content.text
    assert "Call capabilities" in text
    assert "/workspace/report.pdf" in text
    assert "allow_cloud_vlm=False" in text
    assert "output_format=chunks only for Marker-backed sources" in text
    assert "bounded offset pages" in text
    assert "read output chunks" not in text


def test_mcp_resource_link_helpers_add_manifest_and_job_uris():
    import app.mcp_server as mcp_server

    converted = mcp_server._with_output_resource_links(
        {"output": {"text_path": r"C:\path\to\doc.md", "manifest_path": r"C:\path\to\doc.marker.json"}}
    )
    job = mcp_server._with_job_resource_links({"job_id": "job-1", "status": "pending"})

    assert converted["output"]["manifest_uri"].startswith("marker://outputs/")
    assert converted["resource_links"]["manifest"].endswith("/manifest")
    assert job["resource_links"] == {
        "job": "marker://jobs/job-1",
        "manifest": "marker://jobs/job-1/manifest",
        "output": "marker://jobs/job-1/output",
        "assets": "marker://jobs/job-1/assets",
    }
