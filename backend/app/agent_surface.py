"""Canonical agent-facing surface metadata.

Keep this module import-light. CLI, MCP, resources, docs, and agent
capabilities all need the same names without importing FastMCP or converters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.security.scopes import (
    SCOPE_CAPABILITIES_READ,
    SCOPE_JOBS_READ,
    SCOPE_JOBS_WRITE,
    SCOPE_OUTPUTS_READ,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
)


ToolProfile = Literal["minimal", "full", "admin"]


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    profile: ToolProfile = "minimal"
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentResourceSpec:
    uri: str
    scopes: tuple[str, ...]


MCP_TOOL_PROFILES: tuple[ToolProfile, ...] = ("minimal", "full", "admin")

MCP_V2_TOOL_NAMES: tuple[str, ...] = (
    "marker_capabilities",
    "marker_plan",
    "marker_convert",
    "marker_submit",
    "marker_job_status",
    "marker_cancel_job",
    "marker_read_output",
    "marker_output_manifest",
)

MCP_V1_TOOL_NAMES: tuple[str, ...] = (
    "marker_list_capabilities",
    "marker_get_capabilities",
    "marker_self_test",
    "marker_get_health",
    "marker_get_version",
    "marker_plan_conversion",
    "marker_plan_local_file",
    "marker_plan_url",
    "marker_convert_file",
    "marker_convert_local_file",
    "marker_convert_url",
    "marker_submit_job",
    "marker_submit_local_job",
    "marker_submit_url_job",
    "marker_read_output",
    "marker_read_output_chunk",
    "marker_get_output_manifest",
    "marker_list_output_assets",
    "marker_list_jobs",
    "marker_get_job_status",
    "marker_cancel_job",
    "marker_delete_job",
    "marker_purge_job_files",
    "marker_list_settings",
    "marker_get_setting",
    "marker_set_setting",
    "marker_delete_setting",
)

MCP_ALL_TOOL_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys((*MCP_V2_TOOL_NAMES, *MCP_V1_TOOL_NAMES))
)
MCP_MINIMAL_TOOL_NAMES: tuple[str, ...] = MCP_V2_TOOL_NAMES

MCP_SETTINGS_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {"marker_set_setting", "marker_delete_setting"}
)
MCP_ADMIN_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {"marker_delete_job", "marker_purge_job_files", *MCP_SETTINGS_WRITE_TOOL_NAMES}
)
MCP_FULL_TOOL_NAMES: tuple[str, ...] = tuple(
    name for name in MCP_ALL_TOOL_NAMES if name not in MCP_ADMIN_ONLY_TOOL_NAMES
)
MCP_ADMIN_TOOL_NAMES: tuple[str, ...] = MCP_ALL_TOOL_NAMES
DEFAULT_AGENT_TOOL_NAMES: tuple[str, ...] = MCP_MINIMAL_TOOL_NAMES

MCP_RESOURCE_URIS: tuple[str, ...] = (
    "marker://capabilities",
    "marker://health",
    "marker://version",
    "marker://jobs",
    "marker://jobs/{job_id}",
    "marker://jobs/{job_id}/manifest",
    "marker://jobs/{job_id}/output",
    "marker://jobs/{job_id}/assets",
    "marker://outputs/{output_id}/manifest",
    "marker://docs/agent-guide",
    "marker://docs/options",
    "marker://settings",
)

MCP_PROMPT_NAMES: tuple[str, ...] = (
    "convert_for_rag",
    "extract_tables_from_document",
    "summarize_converted_document_with_citations",
    "convert_and_compare_two_documents",
    "batch_convert_folder",
    "inspect_conversion_quality",
    "convert_audio_to_meeting_notes",
    "extract_figures_and_diagrams",
)


def _tool_scopes(name: str) -> tuple[str, ...]:
    if name in {
        "marker_list_capabilities",
        "marker_get_capabilities",
        "marker_self_test",
        "marker_get_health",
        "marker_get_version",
        "marker_capabilities",
        "marker_plan_conversion",
        "marker_plan_local_file",
        "marker_plan_url",
        "marker_plan",
    }:
        return (SCOPE_CAPABILITIES_READ,)
    if name in {
        "marker_convert_file",
        "marker_convert_local_file",
        "marker_convert_url",
        "marker_convert",
        "marker_submit_job",
        "marker_submit_local_job",
        "marker_submit_url_job",
        "marker_submit",
        "marker_cancel_job",
        "marker_delete_job",
        "marker_purge_job_files",
    }:
        return (SCOPE_JOBS_WRITE,)
    if name in {
        "marker_read_output",
        "marker_read_output_chunk",
        "marker_get_output_manifest",
        "marker_output_manifest",
        "marker_list_output_assets",
    }:
        return (SCOPE_OUTPUTS_READ,)
    if name in {"marker_list_jobs", "marker_get_job_status", "marker_job_status"}:
        return (SCOPE_JOBS_READ,)
    if name in {"marker_list_settings", "marker_get_setting"}:
        return (SCOPE_SETTINGS_READ,)
    if name in MCP_SETTINGS_WRITE_TOOL_NAMES:
        return (SCOPE_SETTINGS_WRITE,)
    return ()


MCP_TOOL_SPECS: tuple[AgentToolSpec, ...] = tuple(
    AgentToolSpec(
        name=name,
        profile=(
            "minimal"
            if name in MCP_MINIMAL_TOOL_NAMES
            else "admin"
            if name in MCP_ADMIN_ONLY_TOOL_NAMES
            else "full"
        ),
        scopes=_tool_scopes(name),
    )
    for name in MCP_ALL_TOOL_NAMES
)

MCP_RESOURCE_SPECS: tuple[AgentResourceSpec, ...] = (
    AgentResourceSpec("marker://capabilities", (SCOPE_CAPABILITIES_READ,)),
    AgentResourceSpec("marker://health", (SCOPE_CAPABILITIES_READ,)),
    AgentResourceSpec("marker://version", (SCOPE_CAPABILITIES_READ,)),
    AgentResourceSpec("marker://jobs", (SCOPE_JOBS_READ,)),
    AgentResourceSpec("marker://jobs/{job_id}", (SCOPE_JOBS_READ,)),
    AgentResourceSpec(
        "marker://jobs/{job_id}/manifest",
        (SCOPE_JOBS_READ, SCOPE_OUTPUTS_READ),
    ),
    AgentResourceSpec(
        "marker://jobs/{job_id}/output",
        (SCOPE_JOBS_READ, SCOPE_OUTPUTS_READ),
    ),
    AgentResourceSpec(
        "marker://jobs/{job_id}/assets",
        (SCOPE_JOBS_READ, SCOPE_OUTPUTS_READ),
    ),
    AgentResourceSpec("marker://outputs/{output_id}/manifest", (SCOPE_OUTPUTS_READ,)),
    AgentResourceSpec("marker://docs/agent-guide", (SCOPE_CAPABILITIES_READ,)),
    AgentResourceSpec("marker://docs/options", (SCOPE_CAPABILITIES_READ,)),
    AgentResourceSpec("marker://settings", (SCOPE_SETTINGS_READ,)),
)

MCP_TOOL_SPEC_BY_NAME: dict[str, AgentToolSpec] = {spec.name: spec for spec in MCP_TOOL_SPECS}
MCP_RESOURCE_SPEC_BY_URI: dict[str, AgentResourceSpec] = {spec.uri: spec for spec in MCP_RESOURCE_SPECS}


def tool_names_for_profile(
    profile: str,
    *,
    settings_write_enabled: bool = False,
) -> list[str]:
    normalized = profile.strip().lower()
    if normalized not in MCP_TOOL_PROFILES:
        raise ValueError(
            f"Unknown MCP tool profile '{normalized}'. Expected one of: {', '.join(MCP_TOOL_PROFILES)}"
        )
    if normalized == "minimal":
        return list(MCP_MINIMAL_TOOL_NAMES)
    if normalized == "full":
        return list(MCP_FULL_TOOL_NAMES)
    names = list(MCP_ADMIN_TOOL_NAMES)
    if not settings_write_enabled:
        names = [name for name in names if name not in MCP_SETTINGS_WRITE_TOOL_NAMES]
    return names


def resource_scopes(uri_template: str) -> tuple[str, ...]:
    return MCP_RESOURCE_SPEC_BY_URI[uri_template].scopes
