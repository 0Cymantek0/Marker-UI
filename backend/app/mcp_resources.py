"""MCP resources for browsable Marker state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from app.agent_api import (
    SERVICE_NAME,
    capabilities,
    get_job_status,
    list_jobs,
    list_settings,
    read_output,
)
from app.agent_contract import CONTRACT_SCHEMA_VERSION, export_json_schemas


def register_mcp_resources(
    mcp: Any,
    *,
    tool_names: list[str] | None = None,
    resource_uris: list[str] | None = None,
    prompt_names: list[str] | None = None,
) -> None:
    @mcp.resource(
        "marker://capabilities",
        name="marker_capabilities",
        title="Marker Capabilities",
        description="Supported formats, tools, resources, prompts, and conversion modes.",
        mime_type="application/json",
    )
    async def marker_capabilities_resource() -> dict[str, Any]:
        data = capabilities()
        if tool_names is not None:
            data["tools"] = tool_names
        if resource_uris is not None:
            data["resources"] = resource_uris
        if prompt_names is not None:
            data["prompts"] = prompt_names
        return data

    @mcp.resource(
        "marker://health",
        name="marker_health",
        title="Marker Health",
        description="Lightweight MCP health status.",
        mime_type="application/json",
    )
    def marker_health_resource() -> dict[str, Any]:
        return {"service": SERVICE_NAME, "status": "ok"}

    @mcp.resource(
        "marker://version",
        name="marker_version",
        title="Marker Version",
        description="Version and contract schema information.",
        mime_type="application/json",
    )
    def marker_version_resource() -> dict[str, Any]:
        return {
            "service": SERVICE_NAME,
            "version": os.getenv("MARKER_VERSION", "unknown"),
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        }

    @mcp.resource(
        "marker://jobs",
        name="marker_jobs",
        title="Marker Jobs",
        description="First page of conversion job history.",
        mime_type="application/json",
    )
    async def marker_jobs_resource() -> dict[str, Any]:
        return await list_jobs(page=1, page_size=20)

    @mcp.resource(
        "marker://jobs/{job_id}",
        name="marker_job",
        title="Marker Job",
        description="One conversion job status and metadata.",
        mime_type="application/json",
    )
    async def marker_job_resource(job_id: str) -> dict[str, Any]:
        return await get_job_status(job_id)

    @mcp.resource(
        "marker://jobs/{job_id}/manifest",
        name="marker_job_manifest",
        title="Marker Job Manifest",
        description="Output manifest associated with a completed job.",
        mime_type="application/json",
    )
    async def marker_job_manifest_resource(job_id: str) -> dict[str, Any]:
        status = await get_job_status(job_id)
        return _manifest_for_job_status(status)[1]

    @mcp.resource(
        "marker://jobs/{job_id}/output",
        name="marker_job_output",
        title="Marker Job Output",
        description="First output text chunk associated with a completed job.",
        mime_type="text/plain",
    )
    async def marker_job_output_resource(job_id: str) -> str:
        status = await get_job_status(job_id)
        _, manifest = _manifest_for_job_status(status)
        text_path = _output_text_path_from_manifest(manifest) or status.get("result_path")
        if not text_path:
            return ""
        if Path(text_path).is_dir():
            return ""
        return read_output(str(text_path), offset=0, limit=20_000)["text"]

    @mcp.resource(
        "marker://jobs/{job_id}/assets",
        name="marker_job_assets",
        title="Marker Job Assets",
        description="Output asset entries associated with a completed job.",
        mime_type="application/json",
    )
    async def marker_job_assets_resource(job_id: str) -> dict[str, Any]:
        status = await get_job_status(job_id)
        manifest_path, manifest = _manifest_for_job_status(status)
        output = manifest.get("output") if isinstance(manifest, dict) else {}
        assets = output.get("assets", []) if isinstance(output, dict) else []
        return {"manifest_path": str(manifest_path) if manifest_path else None, "assets": assets}

    @mcp.resource(
        "marker://outputs/{output_id}/manifest",
        name="marker_output_manifest",
        title="Marker Output Manifest",
        description="Manifest for a URL-encoded output path.",
        mime_type="application/json",
    )
    def marker_output_manifest_resource(output_id: str) -> dict[str, Any]:
        _, manifest = _manifest_for_output_path(Path(unquote(output_id)).expanduser())
        return manifest

    @mcp.resource(
        "marker://docs/agent-guide",
        name="marker_agent_guide",
        title="Marker Agent Guide",
        description="Recommended agent workflow for safe document conversion.",
        mime_type="text/markdown",
    )
    def marker_agent_guide_resource() -> str:
        return (
            "# Marker Agent Guide\n\n"
            "1. Read `marker://capabilities`.\n"
            "2. Plan local files with `marker_plan_local_file` or URLs with `marker_plan_url`.\n"
            "3. Convert locally with `marker_convert_local_file` or public URLs with `marker_convert_url`.\n"
            "4. Keep `allow_cloud_vlm=false` unless the user explicitly approves cloud image understanding.\n"
            "5. Read long outputs with `marker_read_output_chunk` and inspect manifests/assets before summarizing.\n"
        )

    @mcp.resource(
        "marker://docs/options",
        name="marker_options",
        title="Marker Options",
        description="Agent-facing conversion option metadata.",
        mime_type="application/json",
    )
    def marker_options_resource() -> dict[str, Any]:
        return {"schema_version": CONTRACT_SCHEMA_VERSION, "options": export_json_schemas()["option_metadata"]}

    @mcp.resource(
        "marker://settings",
        name="marker_settings",
        title="Marker Settings",
        description="Masked settings grouped by category.",
        mime_type="application/json",
    )
    async def marker_settings_resource() -> dict[str, Any]:
        return await list_settings()


def _manifest_for_job_status(status: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    metadata = status.get("conversion_metadata") if isinstance(status, dict) else {}
    manifest_path = metadata.get("manifest_path") if isinstance(metadata, dict) else None
    if manifest_path:
        return _manifest_for_output_path(Path(str(manifest_path)).expanduser())
    result_path = status.get("result_path") if isinstance(status, dict) else None
    if result_path:
        return _manifest_for_output_path(Path(str(result_path)).expanduser())
    return None, {}


def _manifest_for_output_path(path: Path) -> tuple[Path | None, dict[str, Any]]:
    candidates = [path]
    if path.suffix != ".json":
        candidates.append(path.with_name(f"{path.stem}.marker.json"))
    if path.is_dir():
        candidates.extend(sorted(path.glob("*.marker.json")))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict) and manifest.get("schema_version") == "marker.output_manifest.v1":
            return candidate, manifest
    return None, {}


def _output_text_path_from_manifest(manifest: dict[str, Any]) -> str | None:
    output = manifest.get("output") if isinstance(manifest, dict) else None
    if not isinstance(output, dict):
        return None
    text_path = output.get("text_path")
    return str(text_path) if text_path else None
