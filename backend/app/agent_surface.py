"""Canonical agent-facing surface metadata.

Keep this module import-light. CLI, MCP, resources, docs, and agent
capabilities all need the same names without importing FastMCP or converters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.security.scopes import (
    SCOPE_ANSWERS_WRITE,
    SCOPE_CAPABILITIES_READ,
    SCOPE_EVENTS_READ,
    SCOPE_JOBS_READ,
    SCOPE_JOBS_WRITE,
    SCOPE_OUTPUTS_READ,
    SCOPE_QUERIES_READ,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
)


ToolProfile = Literal["minimal", "full", "admin"]


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    title: str
    description: str
    annotations: dict[str, bool]
    profile: ToolProfile = "minimal"
    scopes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    canonical_name: str | None = None
    deprecated: bool = False


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
    "marker_query",
    "marker_events",
)

#: Answer-evidence tools stay out of the minimal profile's bounded
#: default surface: they serve the disclosure-audit workflow, so agents
#: opt in through the full/admin profiles.
MCP_ANSWER_TOOL_NAMES: tuple[str, ...] = (
    "marker_answer_trace",
    "marker_answer_assessment",
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
    dict.fromkeys(
        (*MCP_V2_TOOL_NAMES, *MCP_ANSWER_TOOL_NAMES, *MCP_V1_TOOL_NAMES)
    )
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

_TOOL_TITLES: dict[str, str] = {
    "marker_capabilities": "Marker Capabilities",
    "marker_plan": "Plan Marker Conversion",
    "marker_convert": "Convert With Marker",
    "marker_submit": "Submit Marker Job",
    "marker_job_status": "Get Marker Job Status",
    "marker_output_manifest": "Get Marker Output Manifest",
    "marker_query": "Query Marker Workspace",
    "marker_events": "Read Marker Workspace Events",
    "marker_answer_trace": "Commit Answer Context Trace",
    "marker_answer_assessment": "Record Answer Support Assessment",
    "marker_list_capabilities": "List Marker Capabilities",
    "marker_get_capabilities": "Get Marker Capabilities",
    "marker_self_test": "Self-Test Marker MCP",
    "marker_get_health": "Get Marker Health",
    "marker_get_version": "Get Marker Version",
    "marker_plan_conversion": "Plan Marker Conversion",
    "marker_plan_local_file": "Plan Local File Conversion",
    "marker_plan_url": "Plan URL Conversion",
    "marker_convert_file": "Convert File With Marker",
    "marker_convert_local_file": "Convert Local File With Marker",
    "marker_convert_url": "Convert URL With Marker",
    "marker_submit_job": "Submit Marker Job",
    "marker_submit_local_job": "Submit Local Marker Job",
    "marker_submit_url_job": "Submit URL Marker Job",
    "marker_read_output": "Read Marker Output",
    "marker_read_output_chunk": "Read Marker Output Chunk",
    "marker_get_output_manifest": "Get Marker Output Manifest",
    "marker_list_output_assets": "List Marker Output Assets",
    "marker_list_jobs": "List Marker Jobs",
    "marker_get_job_status": "Get Marker Job Status",
    "marker_cancel_job": "Cancel Marker Job",
    "marker_delete_job": "Delete Marker Job",
    "marker_purge_job_files": "Purge Marker Job Files",
    "marker_list_settings": "List Marker Settings",
    "marker_get_setting": "Get Marker Setting",
    "marker_set_setting": "Set Marker Setting",
    "marker_delete_setting": "Delete Marker Setting",
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "marker_capabilities": "Canonical v2 capability tool.",
    "marker_plan": "Canonical v2 planning tool using one source object.",
    "marker_convert": "Canonical v2 conversion tool using one source object.",
    "marker_submit": "Canonical v2 async job submission tool using one source object.",
    "marker_job_status": "Canonical v2 job status tool.",
    "marker_output_manifest": "Canonical v2 output manifest tool.",
    "marker_query": (
        "Run one bounded typed snapshot query (marker.query.v1) against a published "
        "workspace and continue partial results with the server-issued cursor."
    ),
    "marker_events": (
        "Read the durable per-workspace semantic event log for disconnect-safe "
        "resume by authoritative sequence."
    ),
    "marker_answer_trace": (
        "Commit one external answer bound to its disclosed context, or read it back."
    ),
    "marker_answer_assessment": (
        "Append an independent support assessment for one committed answer trace."
    ),
    "marker_list_capabilities": "Return supported extensions, engines, output modes, and tool names.",
    "marker_get_capabilities": "Alias for marker_list_capabilities using v1 naming.",
    "marker_self_test": "Report expected tools and optionally verify a real deterministic conversion.",
    "marker_get_health": "Return lightweight MCP server health.",
    "marker_get_version": "Return package/build and schema version information.",
    "marker_plan_conversion": "Predict engine, resource needs, probe result, and routing reasons.",
    "marker_plan_local_file": "Plan conversion for one local file inside allowed roots.",
    "marker_plan_url": "Plan conversion for a public URL without downloading it.",
    "marker_convert_file": "Convert a document through the real Marker conversion service.",
    "marker_convert_local_file": "Convert one local file inside allowed roots.",
    "marker_convert_url": "Download a safe public URL and convert it.",
    "marker_submit_job": "Submit a GUI-compatible async conversion job and return its job id.",
    "marker_submit_local_job": "Submit an async conversion job for one local file inside allowed roots.",
    "marker_submit_url_job": "Submit an async conversion job for a safe public URL.",
    "marker_read_output": "Read a bounded slice of a converted Markdown/JSON/HTML output file.",
    "marker_read_output_chunk": "Read one chunk from a converted output file.",
    "marker_get_output_manifest": "Read the Marker output manifest associated with an output path.",
    "marker_list_output_assets": "List sidecar assets recorded in a Marker output manifest.",
    "marker_list_jobs": "List conversion history with pagination and without full result text.",
    "marker_get_job_status": "Get one job status, metadata, paths, and optional bounded result text.",
    "marker_cancel_job": "Cancel one job best-effort without deleting its job record or files.",
    "marker_delete_job": "Delete one terminal job, or force-delete a live job explicitly.",
    "marker_purge_job_files": "Remove upload/output files for a terminal job without deleting history.",
    "marker_list_settings": "List persisted settings grouped by category with sensitive values masked.",
    "marker_get_setting": "Read one persisted setting with sensitive values masked.",
    "marker_set_setting": "Set one setting using the same encryption and masking rules as the GUI.",
    "marker_delete_setting": "Delete one persisted setting key.",
}

_READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_LOCAL_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
_OPEN_READ_ANNOTATIONS = {
    **_READ_ONLY_ANNOTATIONS,
    "openWorldHint": True,
}
_OPEN_WRITE_ANNOTATIONS = {
    **_LOCAL_WRITE_ANNOTATIONS,
    "openWorldHint": True,
}
_DESTRUCTIVE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}
_IDEMPOTENT_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_IDEMPOTENT_DESTRUCTIVE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

_TOOL_ANNOTATIONS: dict[str, dict[str, bool]] = {
    **{name: dict(_READ_ONLY_ANNOTATIONS) for name in (
        "marker_capabilities",
        "marker_job_status",
        "marker_output_manifest",
        "marker_list_capabilities",
        "marker_get_capabilities",
        "marker_self_test",
        "marker_get_health",
        "marker_get_version",
        "marker_plan_conversion",
        "marker_plan_local_file",
        "marker_read_output",
        "marker_read_output_chunk",
        "marker_get_output_manifest",
        "marker_list_output_assets",
        "marker_list_jobs",
        "marker_get_job_status",
        "marker_list_settings",
        "marker_get_setting",
        "marker_query",
        "marker_events",
    )},
    **{name: dict(_OPEN_READ_ANNOTATIONS) for name in ("marker_plan", "marker_plan_url")},
    **{name: dict(_LOCAL_WRITE_ANNOTATIONS) for name in ("marker_convert_local_file", "marker_submit_local_job")},
    **{name: dict(_OPEN_WRITE_ANNOTATIONS) for name in (
        "marker_convert",
        "marker_submit",
        "marker_convert_file",
        "marker_convert_url",
        "marker_submit_job",
        "marker_submit_url_job",
    )},
    **{name: dict(_DESTRUCTIVE_ANNOTATIONS) for name in (
        "marker_cancel_job",
        "marker_delete_job",
        "marker_purge_job_files",
    )},
    "marker_set_setting": dict(_IDEMPOTENT_WRITE_ANNOTATIONS),
    "marker_delete_setting": dict(_IDEMPOTENT_DESTRUCTIVE_ANNOTATIONS),
    "marker_answer_trace": dict(_IDEMPOTENT_WRITE_ANNOTATIONS),
    "marker_answer_assessment": dict(_IDEMPOTENT_WRITE_ANNOTATIONS),
}

_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "marker_capabilities": ("marker_list_capabilities", "marker_get_capabilities"),
    "marker_plan": ("marker_plan_conversion", "marker_plan_local_file", "marker_plan_url"),
    "marker_convert": ("marker_convert_file", "marker_convert_local_file", "marker_convert_url"),
    "marker_submit": ("marker_submit_job", "marker_submit_local_job", "marker_submit_url_job"),
    "marker_job_status": ("marker_get_job_status",),
    "marker_output_manifest": ("marker_get_output_manifest",),
}
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical
    for canonical, aliases in _CANONICAL_ALIASES.items()
    for alias in aliases
}

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
    if name == "marker_query":
        return (SCOPE_QUERIES_READ,)
    if name == "marker_events":
        return (SCOPE_EVENTS_READ,)
    if name in {"marker_answer_trace", "marker_answer_assessment"}:
        return (SCOPE_ANSWERS_WRITE,)
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
        title=_TOOL_TITLES[name],
        description=_TOOL_DESCRIPTIONS[name],
        annotations=dict(_TOOL_ANNOTATIONS[name]),
        profile=(
            "minimal"
            if name in MCP_MINIMAL_TOOL_NAMES
            else "admin"
            if name in MCP_ADMIN_ONLY_TOOL_NAMES
            else "full"
        ),
        scopes=_tool_scopes(name),
        aliases=_CANONICAL_ALIASES.get(name, ()),
        canonical_name=_ALIAS_TO_CANONICAL.get(name),
        deprecated=name in _ALIAS_TO_CANONICAL,
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


def tool_title(name: str) -> str:
    return MCP_TOOL_SPEC_BY_NAME[name].title


def tool_annotations(name: str) -> dict[str, bool]:
    return dict(MCP_TOOL_SPEC_BY_NAME[name].annotations)
