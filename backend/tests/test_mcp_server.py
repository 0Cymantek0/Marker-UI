"""MCP v1 resources, prompts, and split tool tests (UCM-008)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest


@pytest.mark.asyncio
async def test_mcp_registers_v1_tools_resources_templates_and_prompts():
    import app.mcp_server as mcp_server

    mcp_server.configure_mcp_tool_profile("full")
    try:
        tools = {tool.name for tool in await mcp_server.mcp.list_tools()}
        resources = {str(resource.uri) for resource in await mcp_server.mcp.list_resources()}
        templates = {str(template.uriTemplate) for template in await mcp_server.mcp.list_resource_templates()}
        prompts = {prompt.name for prompt in await mcp_server.mcp.list_prompts()}
    finally:
        mcp_server.configure_mcp_tool_profile("minimal")

    assert set(mcp_server.MCP_FULL_TOOL_NAMES).issubset(tools)
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
    assert "marker_convert_file" in capabilities_payload["tools"]
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
    assert "output_format=chunks for native Markdown-derived chunks" in text
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


@pytest.mark.asyncio
async def test_mcp_manifest_tools_read_sibling_manifest_for_json_output(tmp_path: Path):
    import app.mcp_server as mcp_server

    output_path, manifest_path = _write_output_and_manifest(tmp_path)

    manifest_result = await mcp_server.marker_get_output_manifest(str(output_path))
    assets_result = await mcp_server.marker_list_output_assets(str(output_path))

    assert manifest_result["manifest_path"] == str(manifest_path.resolve())
    assert manifest_result["manifest"]["output"]["text_path"] == str(output_path.resolve())
    assert assets_result["manifest_path"] == str(manifest_path.resolve())
    assert assets_result["assets"] == [{"name": "asset.txt", "path": str((tmp_path / "asset.txt").resolve())}]


@pytest.mark.asyncio
async def test_mcp_manifest_resources_share_manifest_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.mcp_resources as mcp_resources
    import app.mcp_server as mcp_server

    output_path, _manifest_path = _write_output_and_manifest(tmp_path)

    output_id = quote(str(output_path), safe="")
    output_resource = await mcp_server.mcp.read_resource(f"marker://outputs/{output_id}/manifest")
    output_manifest = json.loads(output_resource[0].content)
    assert output_manifest["output"]["text_path"] == str(output_path.resolve())

    async def fake_get_job_status(job_id: str) -> dict:
        return {
            "job_id": job_id,
            "result_path": str(output_path),
            "conversion_metadata": {"manifest_path": output_manifest["output"]["manifest_path"]},
        }

    monkeypatch.setattr(mcp_resources, "get_job_status", fake_get_job_status)

    job_manifest_resource = await mcp_server.mcp.read_resource("marker://jobs/job-1/manifest")
    job_assets_resource = await mcp_server.mcp.read_resource("marker://jobs/job-1/assets")

    job_manifest = json.loads(job_manifest_resource[0].content)
    job_assets = json.loads(job_assets_resource[0].content)
    assert job_manifest["output"]["text_path"] == str(output_path.resolve())
    assert job_assets["assets"][0]["name"] == "asset.txt"


def _write_output_and_manifest(tmp_path: Path) -> tuple[Path, Path]:
    output_path = tmp_path / "chunks.json"
    asset_path = tmp_path / "asset.txt"
    manifest_path = tmp_path / "chunks.marker.json"
    output_path.write_text('{"chunks":[]}', encoding="utf-8")
    asset_path.write_text("asset", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "marker.output_manifest.v1",
                "output": {
                    "text_path": str(output_path.resolve()),
                    "manifest_path": str(manifest_path.resolve()),
                    "assets": [{"name": "asset.txt", "path": str(asset_path.resolve())}],
                },
            }
        ),
        encoding="utf-8",
    )
    return output_path, manifest_path
