"""MCP server for Marker.

Default transport is stdio for local coding agents. Streamable HTTP is also
available for multi-client local/remote deployments.
"""

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote, urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field

from app.agent_contract import AUDIO_OUTPUT_MODES, CONTRACT_SCHEMA_VERSION, export_json_schemas
from app.agent_surface import (
    MCP_ALL_TOOL_NAMES as SURFACE_MCP_ALL_TOOL_NAMES,
    MCP_ADMIN_TOOL_NAMES as SURFACE_MCP_ADMIN_TOOL_NAMES,
    MCP_FULL_TOOL_NAMES as SURFACE_MCP_FULL_TOOL_NAMES,
    MCP_MINIMAL_TOOL_NAMES as SURFACE_MCP_MINIMAL_TOOL_NAMES,
    MCP_PROMPT_NAMES as SURFACE_MCP_PROMPT_NAMES,
    MCP_RESOURCE_URIS as SURFACE_MCP_RESOURCE_URIS,
    MCP_SETTINGS_WRITE_TOOL_NAMES,
    MCP_TOOL_PROFILES,
    MCP_V1_TOOL_NAMES as SURFACE_MCP_V1_TOOL_NAMES,
    MCP_V2_TOOL_NAMES as SURFACE_MCP_V2_TOOL_NAMES,
    tool_names_for_profile as surface_tool_names_for_profile,
)
from app.conversion.formats import OUTPUT_FORMATS_DESCRIPTION
from app.agent_api import (
    AgentConversionOptions,
    MAX_READ_CHARS,
    SERVICE_NAME,
    cancel_job,
    capabilities,
    convert_document,
    delete_job,
    delete_setting,
    get_job_status,
    get_setting,
    list_jobs,
    list_settings,
    parse_extra_options_json,
    plan_conversion,
    purge_job_files,
    read_output,
    read_output_chunk,
    set_setting,
    self_test,
    submit_conversion_job,
)
from app.mcp_prompts import register_mcp_prompts
from app.mcp_resources import register_mcp_resources
from app.security.auth import ScopedStaticTokenVerifier, configured_static_tokens, require_mcp_scopes
from app.security.scopes import (
    DEFAULT_MCP_SCOPES,
    SCOPE_CAPABILITIES_READ,
    SCOPE_JOBS_READ,
    SCOPE_JOBS_WRITE,
    SCOPE_OUTPUTS_READ,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
)
from app.services.output_manifest_reader import manifest_for_output_path
from app.services.policy import scoped_client_workspace_roots
from app.services.safe_url_fetcher import assert_safe_source_url


class MarkerOutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class CapabilitiesOutput(MarkerOutputModel):
    service: str = Field(description="Service identifier.", examples=[SERVICE_NAME])
    tools: list[str] = Field(description="Available MCP tool names.", examples=[["marker_convert_file"]])
    allowed_extensions: list[str] = Field(description="Supported file extensions.", examples=[[".pdf", ".csv"]])
    output_formats: list[str] = Field(description="Supported output formats.", examples=[["markdown", "json"]])
    input_formats: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Input format groups with the output formats each group can actually render.",
        examples=[[
            {"extensions": [".docx"], "engine": "office_docx", "output_formats": ["markdown", "chunks"]},
            {"extensions": [".pdf"], "engine": "marker_pdf", "output_formats": ["markdown", "json", "html", "chunks"]},
        ]],
    )
    conversion_profiles: list[str] = Field(description="Conversion profile names.", examples=[["auto", "fast"]])
    image_handling_modes: list[str] = Field(description="Image handling modes.", examples=[["extraction", "understanding"]])
    audio_output_modes: list[str] = Field(description="Audio summary modes.", examples=[["transcript", "notes"]])
    converters: list[dict[str, Any]] = Field(description="Registered converter summaries.", examples=[[{"engine": "text_data"}]])
    agent_guidance: str = Field(description="Recommended agent workflow.", examples=["Plan, convert, then page output."])


class PlanOutput(MarkerOutputModel):
    filename: str = Field(description="Effective source filename.", examples=["document.pdf"])
    size: int = Field(description="Input size in bytes.", examples=[1024])
    preliminary: bool = Field(description="True when planning used metadata only.", examples=[False])
    plan: dict[str, Any] = Field(description="Routing plan and reasons.", examples=[{"engine": "text_data"}])
    probe_result: dict[str, Any] | None = Field(default=None, description="PDF probe metadata when available.", examples=[{"page_count": 3}])
    mixed_engine_segments: list[dict[str, Any]] | None = Field(default=None, description="Mixed PDF segment plan when applicable.", examples=[[{"engine": "marker"}]])


class HealthOutput(MarkerOutputModel):
    service: str = Field(description="Service identifier.", examples=[SERVICE_NAME])
    status: str = Field(description="Health status.", examples=["ok"])


class VersionOutput(MarkerOutputModel):
    service: str = Field(description="Service identifier.", examples=[SERVICE_NAME])
    version: str = Field(description="Package/build version when available.", examples=["unknown"])
    contract_schema_version: str = Field(description="Agent contract schema version.", examples=[CONTRACT_SCHEMA_VERSION])


class ConvertOutput(MarkerOutputModel):
    ok: bool = Field(description="True when conversion succeeded.", examples=[True])
    source: dict[str, Any] = Field(description="Source path/name or URL.", examples=[{"name": "scores.csv"}])
    output: dict[str, Any] = Field(description="Saved output paths and media type.", examples=[{"text_path": "C:\\path\\to\\scores.md"}])
    text_preview: str = Field(description="Bounded converted text preview.", examples=["# Converted"])
    text_chars: int = Field(description="Total converted text characters.", examples=[1200])
    truncated: bool = Field(description="True when preview omits remaining text.", examples=[False])
    metadata: dict[str, Any] = Field(description="Conversion metadata.", examples=[{"engine": {"engine": "text_data"}}])
    next_step: str | None = Field(default=None, description="Suggested follow-up tool call.", examples=["Call marker_read_output with next_offset."])


class SubmitJobOutput(MarkerOutputModel):
    job_id: str = Field(description="Submitted conversion job id.", examples=["11111111-1111-4111-8111-111111111111"])
    status: str = Field(description="Initial job status.", examples=["pending"])
    filename: str = Field(description="Original source filename.", examples=["document.pdf"])
    output_format: str = Field(description="Requested output format.", examples=["markdown"])
    next_step: str = Field(description="Polling instruction.", examples=["Call marker_get_job_status until completed."])


class ReadOutputResult(MarkerOutputModel):
    path: str = Field(description="Resolved output file path.", examples=["C:\\path\\to\\document.md"])
    offset: int = Field(default=0, description="Returned text page start offset (offset mode).", examples=[0])
    limit: int = Field(default=20_000, description="Requested maximum characters (offset mode).", examples=[20000])
    text: str = Field(description="Output text page (offset mode) or semantic chunk text (semantic mode).", examples=["# Converted"])
    text_chars: int = Field(default=0, description="Total text characters in file (offset mode).", examples=[50000])
    has_more: bool = Field(default=False, description="True when more text/chunks remain.", examples=[True])
    next_offset: int | None = Field(default=None, description="Offset for next page (offset mode).", examples=[20000])
    chunk_kind: str = Field(description="Chunking mode: offset_text (character-offset paging) or semantic_markdown (structure-aware RAG chunk).", examples=["offset_text"])
    is_semantic_chunk: bool = Field(description="True when this result is a semantic chunk from a marker.chunks.v1 envelope; false for offset paging.", examples=[False])
    # Semantic-mode fields (absent in offset mode).
    chunk_index: int | None = Field(default=None, description="Index of the returned semantic chunk (semantic mode).", examples=[0])
    chunk_count: int | None = Field(default=None, description="Total semantic chunks in the envelope (semantic mode).", examples=[12])
    next_chunk_index: int | None = Field(default=None, description="Next chunk index, or null if this was the last (semantic mode).", examples=[1])
    schema_version: str | None = Field(default=None, description="Chunk envelope schema version (semantic mode).", examples=["marker.chunks.v1"])
    chunk: dict[str, Any] | None = Field(default=None, description="Full semantic chunk object with id/chunk_id, contextual_text, heading_path/section_path, content_types, line and char spans, counts, source_refs, previous_id, and next_id (semantic mode).", examples=[{"id": "chunk_0000_abc123", "heading_path": ["Title"], "content_types": ["text"]}])


class ManifestToolOutput(MarkerOutputModel):
    manifest_path: str | None = Field(default=None, description="Resolved manifest path when file-backed.", examples=["C:\\path\\to\\document.marker.json"])
    manifest: dict[str, Any] = Field(description="Output manifest JSON.", examples=[{"schema_version": "marker.output_manifest.v1"}])


class AssetsToolOutput(MarkerOutputModel):
    manifest_path: str | None = Field(default=None, description="Resolved manifest path when file-backed.", examples=["C:\\path\\to\\document.marker.json"])
    assets: list[dict[str, Any]] = Field(description="Manifest asset entries.", examples=[[{"name": "image.png"}]])


class JobsOutput(MarkerOutputModel):
    page: int = Field(description="Current page.", examples=[1])
    page_size: int = Field(description="Jobs per page.", examples=[20])
    total: int = Field(description="Total jobs.", examples=[3])
    has_more: bool = Field(description="True when another page exists.", examples=[False])
    next_page: int | None = Field(default=None, description="Next page number.", examples=[2])
    jobs: list[dict[str, Any]] = Field(description="Job summaries.", examples=[[{"status": "completed"}]])


class JobStatusOutput(MarkerOutputModel):
    job_id: str = Field(description="Conversion job id.", examples=["11111111-1111-4111-8111-111111111111"])
    status: str = Field(description="Job status.", examples=["completed"])
    filename: str | None = Field(default=None, description="Stored filename.", examples=["document.pdf"])
    progress: int | None = Field(default=None, description="Progress percentage.", examples=[100])


class DeleteJobOutput(MarkerOutputModel):
    status: str = Field(description="Deletion status.", examples=["deleted"])
    job_id: str = Field(description="Deleted job id.", examples=["11111111-1111-4111-8111-111111111111"])
    files_removed: list[str] = Field(
        description="Resolved paths of files/directories removed during deletion. Empty when none were removed.",
        examples=[["C:\\path\\to\\output.md"]],
    )


class PurgeJobFilesOutput(MarkerOutputModel):
    status: str = Field(description="Purge status.", examples=["purged"])
    job_id: str = Field(description="Purged job id.", examples=["11111111-1111-4111-8111-111111111111"])
    files_removed: list[str] = Field(
        description="Resolved paths of upload/output artifacts removed while keeping the job row.",
        examples=[["C:\\path\\to\\output.md"]],
    )


class CancelJobOutput(MarkerOutputModel):
    status: str = Field(description="Cancellation status.", examples=["cancelled"])
    job_id: str = Field(description="Cancelled job id.", examples=["11111111-1111-4111-8111-111111111111"])
    cancelled: bool = Field(description="True when the job was cancelled by this call or was already cancelled.", examples=[True])


class SettingsOutput(MarkerOutputModel):
    settings: dict[str, Any] = Field(description="Settings grouped by category with secrets masked.", examples=[{"llm": []}])
    total: int = Field(description="Setting count.", examples=[2])
    masked: bool = Field(description="True when sensitive values are masked.", examples=[True])


class SettingOutput(MarkerOutputModel):
    key: str = Field(description="Setting key.", examples=["openai_api_key"])
    value: Any = Field(description="Masked or plain setting value.", examples=["********"])
    category: str = Field(description="Setting category.", examples=["llm"])


class DeleteSettingOutput(MarkerOutputModel):
    status: str = Field(description="Deletion status.", examples=["deleted"])
    key: str = Field(description="Deleted setting key.", examples=["openai_api_key"])


class SelfTestOutput(MarkerOutputModel):
    service: str = Field(description="Service identifier.", examples=[SERVICE_NAME])
    expected_tools: list[str] = Field(description="Expected tool names.", examples=[["marker_convert_file"]])
    tool_profile: str | None = Field(default=None, description="Active MCP tool profile.", examples=["minimal"])
    settings_write_enabled: bool | None = Field(default=None, description="True when settings write/delete tools are registered.", examples=[False])
    capabilities_ok: bool = Field(description="Capability check result.", examples=[True])
    conversion_ok: bool | None = Field(default=None, description="Conversion smoke result when run.", examples=[True])
    notes: list[str] = Field(description="Diagnostic notes.", examples=[[]])
    registered_tools: list[str] | None = Field(default=None, description="Registered MCP tool names.", examples=[["marker_convert_file"]])
    tools_ok: bool | None = Field(default=None, description="Tool registration check result.", examples=[True])
    expected_resources: list[str] | None = Field(default=None, description="Expected MCP resource/template URIs.", examples=[["marker://capabilities"]])
    registered_resources: list[str] | None = Field(default=None, description="Registered MCP resource/template URIs.", examples=[["marker://capabilities"]])
    resources_ok: bool | None = Field(default=None, description="Resource registration check result.", examples=[True])
    expected_prompts: list[str] | None = Field(default=None, description="Expected MCP prompt names.", examples=[["convert_for_rag"]])
    registered_prompts: list[str] | None = Field(default=None, description="Registered MCP prompt names.", examples=[["convert_for_rag"]])
    prompts_ok: bool | None = Field(default=None, description="Prompt registration check result.", examples=[True])
    contract_schema_version: str | None = Field(default=None, description="Agent contract schema version.", examples=[CONTRACT_SCHEMA_VERSION])
    expected_schemas: list[str] | None = Field(default=None, description="Expected exported JSON schema model names.", examples=[["ConvertRequestModel"]])
    registered_schemas: list[str] | None = Field(default=None, description="Exported JSON schema model names.", examples=[["ConvertRequestModel"]])
    schemas_ok: bool | None = Field(default=None, description="JSON schema export check result.", examples=[True])


class SourceInput(MarkerOutputModel):
    kind: Literal["local_path", "url"] = Field(
        description="Source kind: local_path for workspace files, url for safe public HTTP(S).",
        examples=["local_path"],
    )
    path: str = Field(
        default="",
        description="Local file path when kind is local_path.",
        examples=["C:\\path\\to\\document.pdf"],
    )
    url: str = Field(
        default="",
        description="Public HTTP(S) URL when kind is url.",
        examples=["https://example.com/document.pdf"],
    )


PathParam = Annotated[str, Field(description="Local file path. Example: C:\\path\\to\\document.pdf.", examples=["C:\\path\\to\\document.pdf"])]
SourceParam = Annotated[
    SourceInput,
    Field(
        description="Conversion source object.",
        examples=[{"kind": "local_path", "path": "C:\\path\\to\\document.pdf"}],
    ),
]
UrlParam = Annotated[str, Field(description="Public http(s) URL. Example: https://example.com/document.pdf.", examples=["https://example.com/document.pdf"])]
DirParam = Annotated[str, Field(description="Output directory path. Example: C:\\path\\to\\out.", examples=["C:\\path\\to\\out"])]
OutputPathParam = Annotated[str, Field(description="Exact output file path. Existing files are refused unless overwrite is true.", examples=["C:\\path\\to\\out\\document.md"])]
OverwriteParam = Annotated[bool, Field(description="Replace an existing explicit output path and manifest when true.", examples=[False])]
OutputFormatParam = Annotated[str, Field(description=f"Output format: {OUTPUT_FORMATS_DESCRIPTION}.", examples=["markdown"])]
ConverterParam = Annotated[str, Field(description="Optional converter class override.", examples=["TableConverter"])]
EngineParam = Annotated[str, Field(description="Optional engine override such as text_data or marker.", examples=["text_data"])]
ProfileParam = Annotated[str, Field(description="Conversion profile: auto, fast, or high_accuracy.", examples=["auto"])]
ImageModeParam = Annotated[str, Field(description="Image handling mode: extraction, understanding, or both.", examples=["extraction"])]
JsonOptionsParam = Annotated[str, Field(description="JSON object of advanced backend options.", examples=['{"text_data_max_rows": 1000}'])]
TextParam = Annotated[str, Field(description="Free-form text option.", examples=["domain vocabulary"])]
BoolParam = Annotated[bool, Field(description="Boolean feature toggle.", examples=[False])]
OptionalBoolParam = Annotated[bool | None, Field(description="Optional boolean; omit/null preserves backend default.", examples=[None])]
PreviewCharsParam = Annotated[int, Field(ge=0, le=MAX_READ_CHARS, description="Maximum preview/result characters.", examples=[20000])]
PositiveRowsParam = Annotated[int, Field(ge=0, description="Optional non-negative limit; 0 means unset.", examples=[1000])]
OptionalDepthParam = Annotated[int, Field(ge=-1, description="Optional depth/retry/distance; -1 means unset.", examples=[-1])]
PositivePixelsParam = Annotated[int, Field(ge=0, description="Optional positive pixel/count limit; 0 means unset.", examples=[2048])]
ArchiveCompressionRatioParam = Annotated[float, Field(ge=0.0, description="Optional archive compression ratio limit; 0 means unset.", examples=[100.0])]
DensityParam = Annotated[float, Field(ge=-1.0, le=1.0, description="Optional density threshold from 0 to 1; -1 means unset.", examples=[0.2])]
ConfidenceParam = Annotated[float, Field(ge=-1.0, le=1.0, description="Optional confidence threshold from 0 to 1; -1 means unset.", examples=[0.35])]
AudioProviderParam = Annotated[str, Field(description="Audio STT provider id. Local default is local_faster_whisper; cloud providers require audio_allow_cloud_stt.", examples=["local_faster_whisper"])]
AudioAliasesParam = Annotated[str, Field(description='JSON object mapping speaker labels to user-confirmed aliases, e.g. {"speaker_0":"Alice"}.', examples=['{"speaker_0":"Alice"}'])]
AudioListParam = Annotated[str, Field(description="Comma-separated values or JSON string array.", examples=["pack_meeting,pack_product"])]
JobIdParam = Annotated[str, Field(description="Conversion job id.", examples=["11111111-1111-4111-8111-111111111111"])]
SettingKeyParam = Annotated[str, Field(description="Settings key.", examples=["openai_api_key"])]
SettingValueParam = Annotated[str, Field(description="Settings value; sensitive keys are encrypted on write.", examples=["dummy-api-key"])]
CategoryParam = Annotated[str, Field(description="Settings category.", examples=["llm"])]
PageParam = Annotated[int, Field(ge=1, description="One-based page number.", examples=[1])]
PageSizeParam = Annotated[int, Field(ge=1, le=100, description="Items per page.", examples=[20])]
OffsetParam = Annotated[int, Field(ge=0, description="Character offset.", examples=[0])]
LimitParam = Annotated[int, Field(ge=1, le=MAX_READ_CHARS, description="Maximum characters to return.", examples=[20000])]
SizeParam = Annotated[int, Field(ge=0, description="Input size in bytes for metadata-only planning.", examples=[1048576])]
ChunkModeParam = Annotated[
    Literal["offset", "semantic"],
    Field(description="Chunk read mode. 'offset' (default) returns character-offset text paging; 'semantic' returns the Nth structure-aware chunk from a marker.chunks.v1 envelope.", examples=["offset"]),
]
ChunkIndexParam = Annotated[int, Field(ge=0, description="Zero-based semantic chunk index (mode='semantic' only).", examples=[0])]


INSTRUCTIONS = (
    "Marker converts PDFs, Office files, archives, audio, video, images, and "
    "text/data files to agent-readable Markdown. Plan before large PDFs. Convert "
    "with output_dir and bounded max_chars, then page long outputs via "
    "marker_read_output. Cloud/VLM use is opt-in through allow_cloud_vlm."
)

mcp = FastMCP(
    SERVICE_NAME,
    instructions=INSTRUCTIONS,
    json_response=True,
    stateless_http=True,
)

MCP_V2_TOOL_NAMES = list(SURFACE_MCP_V2_TOOL_NAMES)
MCP_V1_TOOL_NAMES = list(SURFACE_MCP_V1_TOOL_NAMES)
MCP_ALL_TOOL_NAMES = list(SURFACE_MCP_ALL_TOOL_NAMES)
MCP_MINIMAL_TOOL_NAMES = list(SURFACE_MCP_MINIMAL_TOOL_NAMES)
MCP_FULL_TOOL_NAMES = list(SURFACE_MCP_FULL_TOOL_NAMES)
MCP_ADMIN_TOOL_NAMES = list(SURFACE_MCP_ADMIN_TOOL_NAMES)
MCP_ACTIVE_TOOL_PROFILE = "minimal"
MCP_ACTIVE_TOOL_NAMES = list(MCP_MINIMAL_TOOL_NAMES)
_ALL_MCP_TOOLS: dict[str, Any] | None = None

MCP_RESOURCE_URIS = list(SURFACE_MCP_RESOURCE_URIS)
MCP_PROMPT_NAMES = list(SURFACE_MCP_PROMPT_NAMES)

register_mcp_resources(
    mcp,
    tool_names=MCP_ACTIVE_TOOL_NAMES,
    resource_uris=MCP_RESOURCE_URIS,
    prompt_names=MCP_PROMPT_NAMES,
)
register_mcp_prompts(mcp)


@mcp.tool(
    name="marker_capabilities",
    title="Marker Capabilities",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_capabilities() -> CapabilitiesOutput:
    """Canonical v2 capability tool."""

    require_mcp_scopes(SCOPE_CAPABILITIES_READ)
    data = capabilities()
    data["tools"] = list(MCP_ACTIVE_TOOL_NAMES)
    data["resources"] = MCP_RESOURCE_URIS
    data["prompts"] = MCP_PROMPT_NAMES
    return data


@mcp.tool(
    name="marker_plan",
    title="Plan Marker Conversion",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def marker_plan(
    source: SourceParam,
    size: SizeParam = 0,
    output_format: OutputFormatParam = "markdown",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    force_ocr: Annotated[bool, Field(description="Force OCR during planning and conversion.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> PlanOutput:
    """Canonical v2 planning tool using one source object."""

    require_mcp_scopes(SCOPE_CAPABILITIES_READ)
    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=False,
        extra_options_json=extra_options_json,
    )
    options.force_ocr = force_ocr
    if source.kind == "url":
        url = _required_source_value(source.url, "source.url")
        assert_safe_source_url(url)
        filename = Path(unquote(urlparse(url).path)).name or "document"
        return await plan_conversion(filename=filename, size=size, options=options)
    return await plan_conversion(
        local_file_path=_required_source_value(source.path, "source.path"),
        size=size,
        options=options,
    )


@mcp.tool(
    name="marker_convert",
    title="Convert With Marker",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def marker_convert(
    ctx: Context,
    source: SourceParam,
    output_dir: DirParam = "",
    output_path: OutputPathParam = "",
    overwrite: OverwriteParam = False,
    output_format: OutputFormatParam = "markdown",
    max_chars: PreviewCharsParam = 20_000,
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> ConvertOutput:
    """Canonical v2 conversion tool using one source object."""

    require_mcp_scopes(SCOPE_JOBS_WRITE)
    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options_json=extra_options_json,
    )
    roots = await _client_workspace_roots(ctx)
    local_file_path, source_url = _source_to_agent_kwargs(source)
    with scoped_client_workspace_roots(roots):
        result = await convert_document(
            local_file_path=local_file_path,
            source_url=source_url,
            output_dir=_none_if_blank(output_dir),
            output_path=_none_if_blank(output_path),
            overwrite=overwrite,
            max_chars=max_chars,
            options=options,
        )
    return _with_output_resource_links(result)


@mcp.tool(
    name="marker_submit",
    title="Submit Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def marker_submit(
    ctx: Context,
    source: SourceParam,
    output_dir: DirParam = "",
    output_format: OutputFormatParam = "markdown",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> SubmitJobOutput:
    """Canonical v2 async job submission tool using one source object."""

    require_mcp_scopes(SCOPE_JOBS_WRITE)
    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options_json=extra_options_json,
    )
    local_file_path, source_url = _source_to_agent_kwargs(source)
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        result = await submit_conversion_job(
            local_file_path=local_file_path,
            source_url=source_url,
            output_dir=_none_if_blank(output_dir),
            options=options,
        )
    return _with_job_resource_links(result)


@mcp.tool(
    name="marker_job_status",
    title="Get Marker Job Status",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_job_status(
    job_id: JobIdParam,
    include_result_text: Annotated[bool, Field(description="Include full result text when available.", examples=[False])] = False,
    max_chars: PreviewCharsParam = 20_000,
) -> JobStatusOutput:
    """Canonical v2 job status tool."""

    require_mcp_scopes(SCOPE_JOBS_READ)
    return _with_job_resource_links(
        await get_job_status(
            job_id,
            include_result_text=include_result_text,
            max_chars=max_chars,
        )
    )


@mcp.tool(
    name="marker_output_manifest",
    title="Get Marker Output Manifest",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def marker_output_manifest(
    output_path: Annotated[str, Field(description="Output text path returned by marker_convert.", examples=["C:\\path\\to\\out\\document.md"])]
) -> ManifestToolOutput:
    """Canonical v2 output manifest tool."""

    require_mcp_scopes(SCOPE_OUTPUTS_READ)
    manifest_path, manifest = manifest_for_output_path(Path(output_path))
    return {"manifest_path": str(manifest_path) if manifest_path else None, "manifest": manifest}


@mcp.tool(
    name="marker_list_capabilities",
    title="List Marker Capabilities",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_list_capabilities() -> CapabilitiesOutput:
    """Return supported extensions, engines, output modes, and tool names."""

    data = capabilities()
    data["tools"] = list(MCP_ACTIVE_TOOL_NAMES)
    data["resources"] = MCP_RESOURCE_URIS
    data["prompts"] = MCP_PROMPT_NAMES
    return data


@mcp.tool(
    name="marker_get_capabilities",
    title="Get Marker Capabilities",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_get_capabilities() -> CapabilitiesOutput:
    """Alias for marker_list_capabilities using v1 naming."""

    return await marker_list_capabilities()


@mcp.tool(
    name="marker_get_health",
    title="Get Marker Health",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_get_health() -> HealthOutput:
    """Return lightweight MCP server health."""

    return {"service": SERVICE_NAME, "status": "ok"}


@mcp.tool(
    name="marker_get_version",
    title="Get Marker Version",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_get_version() -> VersionOutput:
    """Return package/build and schema version information."""

    return {
        "service": SERVICE_NAME,
        "version": os.getenv("MARKER_VERSION", "unknown"),
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
    }


@mcp.tool(
    name="marker_plan_conversion",
    title="Plan Marker Conversion",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_plan_conversion(
    ctx: Context,
    local_file_path: PathParam = "",
    filename: Annotated[str, Field(description="Filename for metadata-only planning.", examples=["document.pdf"])] = "",
    size: SizeParam = 0,
    engine_override: EngineParam = "",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    force_ocr: Annotated[bool, Field(description="Force OCR during planning and conversion.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> PlanOutput:
    """Predict engine, resource needs, probe result, and routing reasons.

    Use this before converting large PDFs or when choosing between fast and
    high-accuracy modes. This tool never writes output files.
    """

    options = AgentConversionOptions(
        engine_override=_none_if_blank(engine_override),
        conversion_profile=_none_if_blank(conversion_profile),
        image_handling_mode=image_handling_mode,
        force_ocr=force_ocr,
        extra_options=parse_extra_options_json(extra_options_json),
    )
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        return await plan_conversion(
            local_file_path=_none_if_blank(local_file_path),
            filename=_none_if_blank(filename),
            size=size,
            options=options,
        )


@mcp.tool(
    name="marker_plan_local_file",
    title="Plan Local File Conversion",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_plan_local_file(
    ctx: Context,
    local_file_path: PathParam,
    engine_override: EngineParam = "",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    force_ocr: Annotated[bool, Field(description="Force OCR during planning and conversion.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> PlanOutput:
    """Plan conversion for one local file inside allowed roots."""

    options = AgentConversionOptions(
        engine_override=_none_if_blank(engine_override),
        conversion_profile=_none_if_blank(conversion_profile),
        image_handling_mode=image_handling_mode,
        force_ocr=force_ocr,
        extra_options=parse_extra_options_json(extra_options_json),
    )
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        return await plan_conversion(local_file_path=local_file_path, options=options)


@mcp.tool(
    name="marker_plan_url",
    title="Plan URL Conversion",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def marker_plan_url(
    source_url: UrlParam,
    filename: Annotated[str, Field(description="Optional filename override for metadata planning.", examples=["document.pdf"])] = "",
    size: SizeParam = 0,
    engine_override: EngineParam = "",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    force_ocr: Annotated[bool, Field(description="Force OCR during planning and conversion.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> PlanOutput:
    """Plan conversion for a public URL without downloading it."""

    assert_safe_source_url(source_url)
    parsed_name = Path(unquote(urlparse(source_url).path)).name or "document"
    options = AgentConversionOptions(
        engine_override=_none_if_blank(engine_override),
        conversion_profile=_none_if_blank(conversion_profile),
        image_handling_mode=image_handling_mode,
        force_ocr=force_ocr,
        extra_options=parse_extra_options_json(extra_options_json),
    )
    return await plan_conversion(
        filename=_none_if_blank(filename) or parsed_name,
        size=size,
        options=options,
    )


@mcp.tool(
    name="marker_convert_file",
    title="Convert File With Marker",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def marker_convert_file(
    ctx: Context,
    local_file_path: PathParam = "",
    source_url: UrlParam = "",
    output_dir: DirParam = "",
    output_path: OutputPathParam = "",
    overwrite: OverwriteParam = False,
    output_format: OutputFormatParam = "markdown",
    max_chars: PreviewCharsParam = 20_000,
    converter_cls: ConverterParam = "",
    engine_override: EngineParam = "",
    conversion_profile: ProfileParam = "",
    use_llm: Annotated[bool, Field(description="Enable LLM post-processing.", examples=[False])] = False,
    llm_provider: Annotated[str, Field(description="Configured LLM provider id.", examples=["openai"])] = "",
    llm_model: Annotated[str, Field(description="Configured LLM model id.", examples=["gpt-4o-mini"])] = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    force_ocr: Annotated[bool, Field(description="Force OCR even when text exists.", examples=[False])] = False,
    paginate_output: Annotated[bool, Field(description="Paginate generated output where supported.", examples=[False])] = False,
    disable_image_extraction: Annotated[bool, Field(description="Disable image extraction from documents.", examples=[False])] = False,
    page_range: Annotated[str, Field(description="Page range for PDF conversion. Example: 1-3,5.", examples=["1-3"])] = "",
    lang: Annotated[str, Field(description="OCR language hint.", examples=["eng"])] = "",
    audio_output_mode: Annotated[str, Field(description=f"Audio output mode: {', '.join(AUDIO_OUTPUT_MODES)}.", examples=["transcript"])] = "",
    audio_model: Annotated[str, Field(description="Audio transcription model id.", examples=["base"])] = "",
    audio_vocabulary: TextParam = "",
    audio_context: TextParam = "",
    audio_low_confidence_threshold: ConfidenceParam = -1.0,
    audio_word_timestamps: Annotated[bool, Field(description="Include word-level timestamps for audio.", examples=[False])] = False,
    audio_provider: AudioProviderParam = "",
    audio_language: Annotated[str, Field(description="Spoken language hint for audio transcription.", examples=["en"])] = "",
    audio_device: Annotated[str, Field(description="Local audio inference device such as cpu or cuda.", examples=["cpu"])] = "",
    audio_compute_type: Annotated[str, Field(description="Local faster-whisper compute type such as int8 or float16.", examples=["int8"])] = "",
    audio_beam_size: PositiveRowsParam = 0,
    audio_vad_filter: OptionalBoolParam = None,
    audio_diarization: Annotated[bool, Field(description="Request speaker diarization when provider supports it.", examples=[False])] = False,
    audio_min_speakers: PositiveRowsParam = 0,
    audio_max_speakers: PositiveRowsParam = 0,
    audio_speaker_aliases_json: AudioAliasesParam = "",
    audio_vocabulary_pack_ids: AudioListParam = "",
    audio_confidence_heatmap: OptionalBoolParam = None,
    audio_quality_diagnostics: OptionalBoolParam = None,
    audio_review_required_on_low_confidence: Annotated[bool, Field(description="Flag output for review when low-confidence audio appears.", examples=[False])] = False,
    audio_text_enhancement_enabled: Annotated[bool, Field(description="Enable deterministic source-bound audio text enhancement.", examples=[False])] = False,
    audio_text_enhancement_strength: Annotated[int, Field(ge=0, le=5, description="Audio text enhancement strength from 0 to 5.", examples=[1])] = 0,
    audio_structural_enhancement_enabled: Annotated[bool, Field(description="Restructure transcript into notes while preserving source references.", examples=[False])] = False,
    audio_structural_enhancement_mode: Annotated[str, Field(description="Audio structure mode: auto, meeting_notes, lecture_notes, interview_qna, action_decision_log, or timeline.", examples=["meeting_notes"])] = "",
    audio_enhancement_allow_cloud: Annotated[bool, Field(description="Explicit opt-in for cloud audio enhancement. No cloud enhancement adapter ships by default.", examples=[False])] = False,
    audio_fusion_mode: Annotated[str, Field(description="Audio/context fusion mode such as audio_first or contradiction_audit.", examples=["audio_first"])] = "",
    audio_contradiction_detection: Annotated[bool, Field(description="Detect possible contradictory spoken claims.", examples=[False])] = False,
    audio_allow_cloud_stt: Annotated[bool, Field(description="Explicit opt-in to send audio to a cloud STT provider.", examples=[False])] = False,
    audio_benchmark_compare: Annotated[bool, Field(description="Reserved for audio provider comparison; current builds reject it because no benchmark runner ships.", examples=[False])] = False,
    audio_compare_providers: AudioListParam = "",
    disable_multiprocessing: Annotated[bool, Field(description="Disable multiprocessing during conversion.", examples=[False])] = False,
    strip_existing_ocr: Annotated[bool, Field(description="Strip existing OCR text before re-OCR.", examples=[False])] = False,
    redo_inline_math: Annotated[bool, Field(description="Reprocess inline math.", examples=[False])] = False,
    debug: Annotated[bool, Field(description="Enable debug conversion artifacts/logging.", examples=[False])] = False,
    router_enabled: OptionalBoolParam = None,
    smart_router_level: Annotated[str, Field(description="Image router level: disabled, smart, or beeg_brain.", examples=["smart"])] = "",
    dedup_enabled: OptionalBoolParam = None,
    downscale_vlm_crops: OptionalBoolParam = None,
    batch_enabled: OptionalBoolParam = None,
    ocr_engine: Annotated[str, Field(description="Local OCR engine: surya or hybrid_ocr.", examples=["surya"])] = "",
    hybrid_ocr_profile: Annotated[str, Field(description="Hybrid OCR profile: balanced, max_accuracy, or low_vram.", examples=["balanced"])] = "",
    hybrid_ocr_require_specialists: OptionalBoolParam = None,
    decorative_max_text_density: DensityParam = -1.0,
    ocr_min_text_density: DensityParam = -1.0,
    ocr_min_lines: PositiveRowsParam = 0,
    dedup_max_distance: OptionalDepthParam = -1,
    vlm_crop_max_px: PositivePixelsParam = 0,
    vlm_batch_size: PositivePixelsParam = 0,
    max_batch_retries: OptionalDepthParam = -1,
    text_data_max_rows: PositiveRowsParam = 0,
    archive_max_files: PositiveRowsParam = 0,
    archive_inline_bytes: PositivePixelsParam = 0,
    archive_max_child_bytes: PositivePixelsParam = 0,
    archive_max_total_uncompressed_bytes: PositivePixelsParam = 0,
    archive_max_compression_ratio: ArchiveCompressionRatioParam = 0.0,
    archive_max_depth: OptionalDepthParam = -1,
    archive_max_converted_children: PositiveRowsParam = 0,
    archive_recursive: OptionalBoolParam = None,
    extra_options_json: JsonOptionsParam = "",
) -> ConvertOutput:
    """Convert a document through the real Marker conversion service.

    Returns a bounded preview plus output file paths. For long outputs, call
    marker_read_output using output.text_path and the returned offsets.
    """

    options = AgentConversionOptions(
        output_format=output_format,
        converter_cls=_none_if_blank(converter_cls),
        engine_override=_none_if_blank(engine_override),
        conversion_profile=_none_if_blank(conversion_profile),
        use_llm=use_llm,
        llm_provider=_none_if_blank(llm_provider),
        llm_model=_none_if_blank(llm_model),
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        force_ocr=force_ocr,
        paginate_output=paginate_output,
        disable_image_extraction=disable_image_extraction,
        page_range=_none_if_blank(page_range),
        lang=_none_if_blank(lang),
        audio_output_mode=_none_if_blank(audio_output_mode),
        audio_model=_none_if_blank(audio_model),
        audio_vocabulary=_none_if_blank(audio_vocabulary),
        audio_context=_none_if_blank(audio_context),
        audio_low_confidence_threshold=(
            audio_low_confidence_threshold
            if audio_low_confidence_threshold >= 0
            else None
        ),
        audio_word_timestamps=audio_word_timestamps,
        **_advanced_audio_options(
            audio_provider=audio_provider,
            audio_language=audio_language,
            audio_device=audio_device,
            audio_compute_type=audio_compute_type,
            audio_beam_size=audio_beam_size,
            audio_vad_filter=audio_vad_filter,
            audio_diarization=audio_diarization,
            audio_min_speakers=audio_min_speakers,
            audio_max_speakers=audio_max_speakers,
            audio_speaker_aliases_json=audio_speaker_aliases_json,
            audio_vocabulary_pack_ids=audio_vocabulary_pack_ids,
            audio_confidence_heatmap=audio_confidence_heatmap,
            audio_quality_diagnostics=audio_quality_diagnostics,
            audio_review_required_on_low_confidence=audio_review_required_on_low_confidence,
            audio_text_enhancement_enabled=audio_text_enhancement_enabled,
            audio_text_enhancement_strength=audio_text_enhancement_strength,
            audio_structural_enhancement_enabled=audio_structural_enhancement_enabled,
            audio_structural_enhancement_mode=audio_structural_enhancement_mode,
            audio_enhancement_allow_cloud=audio_enhancement_allow_cloud,
            audio_fusion_mode=audio_fusion_mode,
            audio_contradiction_detection=audio_contradiction_detection,
            audio_allow_cloud_stt=audio_allow_cloud_stt,
            audio_benchmark_compare=audio_benchmark_compare,
            audio_compare_providers=audio_compare_providers,
        ),
        disable_multiprocessing=disable_multiprocessing,
        strip_existing_ocr=strip_existing_ocr,
        redo_inline_math=redo_inline_math,
        debug=debug,
        extra_options={
            **_image_understanding_extra_options(
                router_enabled=router_enabled,
                smart_router_level=smart_router_level,
                dedup_enabled=dedup_enabled,
                downscale_vlm_crops=downscale_vlm_crops,
                batch_enabled=batch_enabled,
                ocr_engine=ocr_engine,
                hybrid_ocr_profile=hybrid_ocr_profile,
                hybrid_ocr_require_specialists=hybrid_ocr_require_specialists,
                decorative_max_text_density=decorative_max_text_density,
                ocr_min_text_density=ocr_min_text_density,
                ocr_min_lines=ocr_min_lines,
                dedup_max_distance=dedup_max_distance,
                vlm_crop_max_px=vlm_crop_max_px,
                vlm_batch_size=vlm_batch_size,
                max_batch_retries=max_batch_retries,
            ),
            **_agent_productivity_extra_options(
                text_data_max_rows=text_data_max_rows,
                archive_max_files=archive_max_files,
                archive_inline_bytes=archive_inline_bytes,
                archive_max_child_bytes=archive_max_child_bytes,
                archive_max_total_uncompressed_bytes=archive_max_total_uncompressed_bytes,
                archive_max_compression_ratio=archive_max_compression_ratio,
                archive_max_depth=archive_max_depth,
                archive_max_converted_children=archive_max_converted_children,
                archive_recursive=archive_recursive,
            ),
            **parse_extra_options_json(extra_options_json),
        },
    )
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        result = await convert_document(
            local_file_path=_none_if_blank(local_file_path),
            source_url=_none_if_blank(source_url),
            output_dir=_none_if_blank(output_dir),
            output_path=_none_if_blank(output_path),
            overwrite=overwrite,
            max_chars=max_chars,
            options=options,
        )
    return _with_output_resource_links(result)


@mcp.tool(
    name="marker_convert_local_file",
    title="Convert Local File With Marker",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def marker_convert_local_file(
    ctx: Context,
    local_file_path: PathParam,
    output_dir: DirParam = "",
    output_path: OutputPathParam = "",
    overwrite: OverwriteParam = False,
    output_format: OutputFormatParam = "markdown",
    max_chars: PreviewCharsParam = 20_000,
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> ConvertOutput:
    """Convert one local file inside allowed roots."""

    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options_json=extra_options_json,
    )
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        result = await convert_document(
            local_file_path=local_file_path,
            output_dir=_none_if_blank(output_dir),
            output_path=_none_if_blank(output_path),
            overwrite=overwrite,
            max_chars=max_chars,
            options=options,
        )
    return _with_output_resource_links(result)


@mcp.tool(
    name="marker_convert_url",
    title="Convert URL With Marker",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def marker_convert_url(
    source_url: UrlParam,
    output_dir: DirParam = "",
    output_path: OutputPathParam = "",
    overwrite: OverwriteParam = False,
    output_format: OutputFormatParam = "markdown",
    max_chars: PreviewCharsParam = 20_000,
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> ConvertOutput:
    """Download a safe public URL and convert it."""

    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options_json=extra_options_json,
    )
    result = await convert_document(
        source_url=source_url,
        output_dir=_none_if_blank(output_dir),
        output_path=_none_if_blank(output_path),
        overwrite=overwrite,
        max_chars=max_chars,
        options=options,
    )
    return _with_output_resource_links(result)


@mcp.tool(
    name="marker_submit_job",
    title="Submit Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def marker_submit_job(
    ctx: Context,
    local_file_path: PathParam = "",
    source_url: UrlParam = "",
    output_dir: DirParam = "",
    output_format: OutputFormatParam = "markdown",
    converter_cls: ConverterParam = "",
    engine_override: EngineParam = "",
    conversion_profile: ProfileParam = "",
    use_llm: Annotated[bool, Field(description="Enable LLM post-processing.", examples=[False])] = False,
    llm_provider: Annotated[str, Field(description="Configured LLM provider id.", examples=["openai"])] = "",
    llm_model: Annotated[str, Field(description="Configured LLM model id.", examples=["gpt-4o-mini"])] = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    force_ocr: Annotated[bool, Field(description="Force OCR even when text exists.", examples=[False])] = False,
    paginate_output: Annotated[bool, Field(description="Paginate generated output where supported.", examples=[False])] = False,
    disable_image_extraction: Annotated[bool, Field(description="Disable image extraction from documents.", examples=[False])] = False,
    page_range: Annotated[str, Field(description="Page range for PDF conversion. Example: 1-3,5.", examples=["1-3"])] = "",
    lang: Annotated[str, Field(description="OCR language hint.", examples=["eng"])] = "",
    audio_output_mode: Annotated[str, Field(description=f"Audio output mode: {', '.join(AUDIO_OUTPUT_MODES)}.", examples=["transcript"])] = "",
    audio_model: Annotated[str, Field(description="Audio transcription model id.", examples=["base"])] = "",
    audio_vocabulary: TextParam = "",
    audio_context: TextParam = "",
    audio_low_confidence_threshold: ConfidenceParam = -1.0,
    audio_word_timestamps: Annotated[bool, Field(description="Include word-level timestamps for audio.", examples=[False])] = False,
    audio_provider: AudioProviderParam = "",
    audio_language: Annotated[str, Field(description="Spoken language hint for audio transcription.", examples=["en"])] = "",
    audio_device: Annotated[str, Field(description="Local audio inference device such as cpu or cuda.", examples=["cpu"])] = "",
    audio_compute_type: Annotated[str, Field(description="Local faster-whisper compute type such as int8 or float16.", examples=["int8"])] = "",
    audio_beam_size: PositiveRowsParam = 0,
    audio_vad_filter: OptionalBoolParam = None,
    audio_diarization: Annotated[bool, Field(description="Request speaker diarization when provider supports it.", examples=[False])] = False,
    audio_min_speakers: PositiveRowsParam = 0,
    audio_max_speakers: PositiveRowsParam = 0,
    audio_speaker_aliases_json: AudioAliasesParam = "",
    audio_vocabulary_pack_ids: AudioListParam = "",
    audio_confidence_heatmap: OptionalBoolParam = None,
    audio_quality_diagnostics: OptionalBoolParam = None,
    audio_review_required_on_low_confidence: Annotated[bool, Field(description="Flag output for review when low-confidence audio appears.", examples=[False])] = False,
    audio_text_enhancement_enabled: Annotated[bool, Field(description="Enable deterministic source-bound audio text enhancement.", examples=[False])] = False,
    audio_text_enhancement_strength: Annotated[int, Field(ge=0, le=5, description="Audio text enhancement strength from 0 to 5.", examples=[1])] = 0,
    audio_structural_enhancement_enabled: Annotated[bool, Field(description="Restructure transcript into notes while preserving source references.", examples=[False])] = False,
    audio_structural_enhancement_mode: Annotated[str, Field(description="Audio structure mode: auto, meeting_notes, lecture_notes, interview_qna, action_decision_log, or timeline.", examples=["meeting_notes"])] = "",
    audio_enhancement_allow_cloud: Annotated[bool, Field(description="Explicit opt-in for cloud audio enhancement. No cloud enhancement adapter ships by default.", examples=[False])] = False,
    audio_fusion_mode: Annotated[str, Field(description="Audio/context fusion mode such as audio_first or contradiction_audit.", examples=["audio_first"])] = "",
    audio_contradiction_detection: Annotated[bool, Field(description="Detect possible contradictory spoken claims.", examples=[False])] = False,
    audio_allow_cloud_stt: Annotated[bool, Field(description="Explicit opt-in to send audio to a cloud STT provider.", examples=[False])] = False,
    audio_benchmark_compare: Annotated[bool, Field(description="Reserved for audio provider comparison; current builds reject it because no benchmark runner ships.", examples=[False])] = False,
    audio_compare_providers: AudioListParam = "",
    disable_multiprocessing: Annotated[bool, Field(description="Disable multiprocessing during conversion.", examples=[False])] = False,
    strip_existing_ocr: Annotated[bool, Field(description="Strip existing OCR text before re-OCR.", examples=[False])] = False,
    redo_inline_math: Annotated[bool, Field(description="Reprocess inline math.", examples=[False])] = False,
    debug: Annotated[bool, Field(description="Enable debug conversion artifacts/logging.", examples=[False])] = False,
    text_data_max_rows: PositiveRowsParam = 0,
    archive_max_files: PositiveRowsParam = 0,
    archive_inline_bytes: PositivePixelsParam = 0,
    archive_max_child_bytes: PositivePixelsParam = 0,
    archive_max_total_uncompressed_bytes: PositivePixelsParam = 0,
    archive_max_compression_ratio: ArchiveCompressionRatioParam = 0.0,
    archive_max_depth: OptionalDepthParam = -1,
    archive_max_converted_children: PositiveRowsParam = 0,
    archive_recursive: OptionalBoolParam = None,
    extra_options_json: JsonOptionsParam = "",
) -> SubmitJobOutput:
    """Submit a GUI-compatible async conversion job and return its job id.

    Use this for long documents, batch-like agent workflows, or when GUI
    history/status parity matters. Poll marker_get_job_status afterwards.
    """

    options = AgentConversionOptions(
        output_format=output_format,
        converter_cls=_none_if_blank(converter_cls),
        engine_override=_none_if_blank(engine_override),
        conversion_profile=_none_if_blank(conversion_profile),
        use_llm=use_llm,
        llm_provider=_none_if_blank(llm_provider),
        llm_model=_none_if_blank(llm_model),
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        force_ocr=force_ocr,
        paginate_output=paginate_output,
        disable_image_extraction=disable_image_extraction,
        page_range=_none_if_blank(page_range),
        lang=_none_if_blank(lang),
        audio_output_mode=_none_if_blank(audio_output_mode),
        audio_model=_none_if_blank(audio_model),
        audio_vocabulary=_none_if_blank(audio_vocabulary),
        audio_context=_none_if_blank(audio_context),
        audio_low_confidence_threshold=(
            audio_low_confidence_threshold
            if audio_low_confidence_threshold >= 0
            else None
        ),
        audio_word_timestamps=audio_word_timestamps,
        **_advanced_audio_options(
            audio_provider=audio_provider,
            audio_language=audio_language,
            audio_device=audio_device,
            audio_compute_type=audio_compute_type,
            audio_beam_size=audio_beam_size,
            audio_vad_filter=audio_vad_filter,
            audio_diarization=audio_diarization,
            audio_min_speakers=audio_min_speakers,
            audio_max_speakers=audio_max_speakers,
            audio_speaker_aliases_json=audio_speaker_aliases_json,
            audio_vocabulary_pack_ids=audio_vocabulary_pack_ids,
            audio_confidence_heatmap=audio_confidence_heatmap,
            audio_quality_diagnostics=audio_quality_diagnostics,
            audio_review_required_on_low_confidence=audio_review_required_on_low_confidence,
            audio_text_enhancement_enabled=audio_text_enhancement_enabled,
            audio_text_enhancement_strength=audio_text_enhancement_strength,
            audio_structural_enhancement_enabled=audio_structural_enhancement_enabled,
            audio_structural_enhancement_mode=audio_structural_enhancement_mode,
            audio_enhancement_allow_cloud=audio_enhancement_allow_cloud,
            audio_fusion_mode=audio_fusion_mode,
            audio_contradiction_detection=audio_contradiction_detection,
            audio_allow_cloud_stt=audio_allow_cloud_stt,
            audio_benchmark_compare=audio_benchmark_compare,
            audio_compare_providers=audio_compare_providers,
        ),
        disable_multiprocessing=disable_multiprocessing,
        strip_existing_ocr=strip_existing_ocr,
        redo_inline_math=redo_inline_math,
        debug=debug,
        extra_options={
            **_agent_productivity_extra_options(
                text_data_max_rows=text_data_max_rows,
                archive_max_files=archive_max_files,
                archive_inline_bytes=archive_inline_bytes,
                archive_max_child_bytes=archive_max_child_bytes,
                archive_max_total_uncompressed_bytes=archive_max_total_uncompressed_bytes,
                archive_max_compression_ratio=archive_max_compression_ratio,
                archive_max_depth=archive_max_depth,
                archive_max_converted_children=archive_max_converted_children,
                archive_recursive=archive_recursive,
            ),
            **parse_extra_options_json(extra_options_json),
        },
    )
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        result = await submit_conversion_job(
            local_file_path=_none_if_blank(local_file_path),
            source_url=_none_if_blank(source_url),
            output_dir=_none_if_blank(output_dir),
            options=options,
        )
    return _with_job_resource_links(result)


@mcp.tool(
    name="marker_submit_local_job",
    title="Submit Local Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def marker_submit_local_job(
    ctx: Context,
    local_file_path: PathParam,
    output_dir: DirParam = "",
    output_format: OutputFormatParam = "markdown",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> SubmitJobOutput:
    """Submit an async conversion job for one local file inside allowed roots."""

    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options_json=extra_options_json,
    )
    roots = await _client_workspace_roots(ctx)
    with scoped_client_workspace_roots(roots):
        result = await submit_conversion_job(
            local_file_path=local_file_path,
            output_dir=_none_if_blank(output_dir),
            options=options,
        )
    return _with_job_resource_links(result)


@mcp.tool(
    name="marker_submit_url_job",
    title="Submit URL Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def marker_submit_url_job(
    source_url: UrlParam,
    output_dir: DirParam = "",
    output_format: OutputFormatParam = "markdown",
    conversion_profile: ProfileParam = "",
    image_handling_mode: ImageModeParam = "extraction",
    allow_cloud_vlm: Annotated[bool, Field(description="Allow cloud VLM calls for image understanding. Keep false unless user permits.", examples=[False])] = False,
    extra_options_json: JsonOptionsParam = "",
) -> SubmitJobOutput:
    """Submit an async conversion job for a safe public URL."""

    options = _split_conversion_options(
        output_format=output_format,
        conversion_profile=conversion_profile,
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options_json=extra_options_json,
    )
    result = await submit_conversion_job(
        source_url=source_url,
        output_dir=_none_if_blank(output_dir),
        options=options,
    )
    return _with_job_resource_links(result)


@mcp.tool(
    name="marker_read_output",
    title="Read Marker Output",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_read_output(
    output_path: Annotated[str, Field(description="Path returned as output.text_path by marker_convert_file or marker_get_job_status.", examples=["C:\\path\\to\\document.md"])],
    offset: OffsetParam = 0,
    limit: LimitParam = 20_000,
) -> ReadOutputResult:
    """Read a bounded slice of a converted Markdown/JSON/HTML output file."""

    require_mcp_scopes(SCOPE_OUTPUTS_READ)
    return read_output(output_path, offset=offset, limit=limit)


@mcp.tool(
    name="marker_read_output_chunk",
    title="Read Marker Output Chunk",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_read_output_chunk(
    output_path: Annotated[str, Field(description="Path returned as output.text_path by conversion/job status. For semantic mode, point this at the chunks .json output.", examples=["C:\\path\\to\\document.md"])],
    mode: ChunkModeParam = "offset",
    chunk_index: ChunkIndexParam = 0,
    offset: OffsetParam = 0,
    limit: LimitParam = 20_000,
) -> ReadOutputResult:
    """Read one chunk from a converted output file.

    ``mode="offset"`` (default, backward compatible) returns a bounded
    character-offset text page — useful for streaming large outputs.

    ``mode="semantic"`` reads the Nth semantic chunk (``chunk_index``) from a
    persisted ``marker.chunks.v1`` envelope, returning structural metadata
    (heading path, line span, token estimate). Use this for RAG retrieval
    when the conversion produced a chunks artifact.
    """

    require_mcp_scopes(SCOPE_OUTPUTS_READ)
    return read_output_chunk(
        output_path,
        mode=mode,
        chunk_index=chunk_index,
        offset=offset,
        limit=limit,
    )


@mcp.tool(
    name="marker_get_output_manifest",
    title="Get Marker Output Manifest",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_get_output_manifest(
    output_path: Annotated[str, Field(description="Output text path or manifest path.", examples=["C:\\path\\to\\document.md"])],
) -> ManifestToolOutput:
    """Read the Marker output manifest associated with an output path."""

    require_mcp_scopes(SCOPE_OUTPUTS_READ)
    manifest_path, manifest = manifest_for_output_path(Path(output_path))
    return {"manifest_path": str(manifest_path.resolve()) if manifest_path else None, "manifest": manifest}


@mcp.tool(
    name="marker_list_output_assets",
    title="List Marker Output Assets",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_list_output_assets(
    output_path: Annotated[str, Field(description="Output text path or manifest path.", examples=["C:\\path\\to\\document.md"])],
) -> AssetsToolOutput:
    """List sidecar assets recorded in a Marker output manifest."""

    require_mcp_scopes(SCOPE_OUTPUTS_READ)
    manifest_path, manifest = manifest_for_output_path(Path(output_path))
    output = manifest.get("output") if isinstance(manifest, dict) else {}
    assets = output.get("assets", []) if isinstance(output, dict) else []
    return {"manifest_path": str(manifest_path.resolve()) if manifest_path else None, "assets": assets}


@mcp.tool(
    name="marker_list_jobs",
    title="List Marker Jobs",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_list_jobs(
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
) -> JobsOutput:
    """List conversion history with pagination and without full result text."""

    return await list_jobs(page=page, page_size=page_size)


@mcp.tool(
    name="marker_get_job_status",
    title="Get Marker Job Status",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_get_job_status(
    job_id: JobIdParam,
    include_result_text: Annotated[bool, Field(description="Include bounded result text in response.", examples=[False])] = False,
    max_chars: PreviewCharsParam = 20_000,
) -> JobStatusOutput:
    """Get one job status, metadata, paths, and optional bounded result text."""

    result = await get_job_status(
        job_id,
        include_result_text=include_result_text,
        max_chars=max_chars,
    )
    return _with_job_resource_links(result)


@mcp.tool(
    name="marker_cancel_job",
    title="Cancel Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def marker_cancel_job(
    job_id: JobIdParam,
) -> CancelJobOutput:
    """Cancel one job best-effort without deleting its job record or files."""

    require_mcp_scopes(SCOPE_JOBS_WRITE)
    return await cancel_job(job_id)


@mcp.tool(
    name="marker_delete_job",
    title="Delete Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def marker_delete_job(
    job_id: JobIdParam,
    delete_files: Annotated[bool, Field(description="Also delete upload/output files associated with job.", examples=[True])] = True,
    force: Annotated[bool, Field(description="Explicitly cancel and delete a pending/running job. Leave false to delete terminal history only.", examples=[False])] = False,
) -> DeleteJobOutput:
    """Delete one terminal job, or force-delete a live job explicitly."""

    require_mcp_scopes(SCOPE_JOBS_WRITE)
    return await delete_job(job_id, delete_files=delete_files, force=force)


@mcp.tool(
    name="marker_purge_job_files",
    title="Purge Marker Job Files",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def marker_purge_job_files(
    job_id: JobIdParam,
) -> PurgeJobFilesOutput:
    """Remove upload/output files for a terminal job without deleting history."""

    require_mcp_scopes(SCOPE_JOBS_WRITE)
    return await purge_job_files(job_id)


@mcp.tool(
    name="marker_list_settings",
    title="List Marker Settings",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_list_settings(
    category: CategoryParam = "",
) -> SettingsOutput:
    """List persisted settings grouped by category with sensitive values masked."""

    require_mcp_scopes(SCOPE_SETTINGS_READ)
    return await list_settings(category=_none_if_blank(category))


@mcp.tool(
    name="marker_get_setting",
    title="Get Marker Setting",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_get_setting(
    key: SettingKeyParam,
    category: Annotated[str, Field(description="Optional settings category guard.", examples=["llm"])] = "",
) -> SettingOutput:
    """Read one persisted setting with sensitive values masked."""

    require_mcp_scopes(SCOPE_SETTINGS_READ)
    return await get_setting(key, category=_none_if_blank(category))


@mcp.tool(
    name="marker_set_setting",
    title="Set Marker Setting",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_set_setting(
    key: SettingKeyParam,
    value: SettingValueParam,
    category: CategoryParam = "general",
) -> SettingOutput:
    """Set one setting using the same encryption and masking rules as the GUI."""

    require_mcp_scopes(SCOPE_SETTINGS_WRITE)
    return await set_setting(key, value, category=category)


@mcp.tool(
    name="marker_delete_setting",
    title="Delete Marker Setting",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_delete_setting(
    key: SettingKeyParam,
    category: Annotated[str, Field(description="Optional settings category guard.", examples=["llm"])] = "",
) -> DeleteSettingOutput:
    """Delete one persisted setting key."""

    require_mcp_scopes(SCOPE_SETTINGS_WRITE)
    return await delete_setting(key, category=_none_if_blank(category))


@mcp.tool(
    name="marker_self_test",
    title="Self-Test Marker MCP",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def marker_self_test(
    include_conversion: Annotated[bool, Field(description="Run deterministic conversion smoke check.", examples=[True])] = True,
) -> SelfTestOutput:
    """Report expected tools and optionally verify a real deterministic conversion."""

    data = await self_test(include_conversion=include_conversion)
    registered = await mcp.list_tools()
    resources = await mcp.list_resources()
    templates = await mcp.list_resource_templates()
    prompts = await mcp.list_prompts()
    data["expected_tools"] = list(MCP_ACTIVE_TOOL_NAMES)
    data["tool_profile"] = MCP_ACTIVE_TOOL_PROFILE
    data["settings_write_enabled"] = mcp_settings_write_enabled()
    data["registered_tools"] = sorted(tool.name for tool in registered)
    data["tools_ok"] = sorted(MCP_ACTIVE_TOOL_NAMES) == data["registered_tools"]
    data["expected_resources"] = MCP_RESOURCE_URIS
    data["registered_resources"] = sorted(
        [str(resource.uri) for resource in resources]
        + [str(template.uriTemplate) for template in templates]
    )
    data["resources_ok"] = sorted(MCP_RESOURCE_URIS) == data["registered_resources"]
    data["expected_prompts"] = MCP_PROMPT_NAMES
    data["registered_prompts"] = sorted(prompt.name for prompt in prompts)
    data["prompts_ok"] = sorted(MCP_PROMPT_NAMES) == data["registered_prompts"]
    schemas = export_json_schemas()
    expected_schema_names = sorted(
        [
            "ConversionOptionsModel",
            "ConvertRequestModel",
            "ConvertResultModel",
            "OutputManifestModel",
            "MarkerErrorModel",
            "BatchRequestModel",
            "BatchResultModel",
        ]
    )
    registered_schema_names = sorted(schemas.get("models", {}).keys())
    data["contract_schema_version"] = schemas.get("schema_version")
    data["expected_schemas"] = expected_schema_names
    data["registered_schemas"] = registered_schema_names
    data["schemas_ok"] = (
        data["contract_schema_version"] == CONTRACT_SCHEMA_VERSION
        and set(expected_schema_names).issubset(set(registered_schema_names))
        and bool(schemas.get("option_metadata"))
    )
    return data


def tool_names_for_profile(profile: str | None = None) -> list[str]:
    normalized = (profile or os.getenv("MARKER_MCP_TOOL_PROFILE") or "minimal").strip().lower()
    return surface_tool_names_for_profile(
        normalized,
        settings_write_enabled=mcp_settings_write_enabled(),
    )


def mcp_settings_write_enabled() -> bool:
    return os.getenv("MARKER_MCP_ENABLE_SETTINGS_WRITE", "false").strip().lower() in {
        "true",
        "1",
        "yes",
    }


def configure_mcp_tool_profile(profile: str | None = None) -> str:
    """Apply MCP tool profile to the live FastMCP tool registry."""

    global MCP_ACTIVE_TOOL_PROFILE, _ALL_MCP_TOOLS
    normalized = (profile or os.getenv("MARKER_MCP_TOOL_PROFILE") or "minimal").strip().lower()
    names = tool_names_for_profile(normalized)
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):  # pragma: no cover - depends on FastMCP internals
        raise RuntimeError("FastMCP tool manager does not expose a mutable tool registry")
    if _ALL_MCP_TOOLS is None:
        _ALL_MCP_TOOLS = dict(tools)
    missing = [name for name in names if name not in _ALL_MCP_TOOLS]
    if missing:
        raise RuntimeError(f"MCP tool profile '{normalized}' references missing tools: {missing}")
    tools.clear()
    tools.update({name: _ALL_MCP_TOOLS[name] for name in names})
    MCP_ACTIVE_TOOL_NAMES[:] = names
    MCP_ACTIVE_TOOL_PROFILE = normalized
    return normalized


configure_mcp_tool_profile()


def run(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_token: str | None = None,
    tool_profile: str | None = None,
) -> None:
    configure_mcp_tool_profile(tool_profile)
    if transport == "streamable-http":
        token = auth_token or os.getenv("MARKER_MCP_AUTH_TOKEN", "").strip()
        if not _is_loopback_host(host) and not token:
            raise ValueError(
                "Refusing streamable HTTP on non-loopback host without "
                "MARKER_MCP_AUTH_TOKEN. Bind to 127.0.0.1 or set a bearer token."
            )
        mcp.settings.host = host
        mcp.settings.port = port
        if token:
            token_scopes = configured_static_tokens(surface="mcp")
            token_scopes.setdefault(token, DEFAULT_MCP_SCOPES)
            issuer_url = _auth_base_url(host, port)
            mcp.settings.auth = AuthSettings(
                issuer_url=issuer_url,
                resource_server_url=issuer_url,
                required_scopes=[],
            )
            mcp._token_verifier = ScopedStaticTokenVerifier(token_scopes)
        else:
            mcp.settings.auth = None
            mcp._token_verifier = None
    mcp.run(transport=transport)


def _source_to_agent_kwargs(source: SourceInput) -> tuple[str | None, str | None]:
    if source.kind == "url":
        return None, _required_source_value(source.url, "source.url")
    return _required_source_value(source.path, "source.path"), None


def _required_source_value(value: str, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _none_if_blank(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None


def _split_conversion_options(
    *,
    output_format: str,
    conversion_profile: str,
    image_handling_mode: str,
    allow_cloud_vlm: bool,
    extra_options_json: str,
) -> AgentConversionOptions:
    return AgentConversionOptions(
        output_format=output_format,
        conversion_profile=_none_if_blank(conversion_profile),
        image_handling_mode=image_handling_mode,
        allow_cloud_vlm=allow_cloud_vlm,
        extra_options=parse_extra_options_json(extra_options_json),
    )


def _with_output_resource_links(result: dict[str, Any]) -> dict[str, Any]:
    output = result.get("output")
    if not isinstance(output, dict):
        return result
    text_path = output.get("text_path")
    manifest_path = output.get("manifest_path")
    links: dict[str, str] = {}
    if text_path:
        output_id = quote(str(text_path), safe="")
        links["manifest"] = f"marker://outputs/{output_id}/manifest"
    if manifest_path:
        output["manifest_uri"] = links.get("manifest")
    if links:
        result["resource_links"] = links
    return result


def _with_job_resource_links(result: dict[str, Any]) -> dict[str, Any]:
    job_id = result.get("job_id")
    if not job_id:
        return result
    links = {
        "job": f"marker://jobs/{job_id}",
        "manifest": f"marker://jobs/{job_id}/manifest",
        "output": f"marker://jobs/{job_id}/output",
        "assets": f"marker://jobs/{job_id}/assets",
    }
    result["resource_links"] = links
    return result


def _advanced_audio_options(
    *,
    audio_provider: str,
    audio_language: str,
    audio_device: str,
    audio_compute_type: str,
    audio_beam_size: int,
    audio_vad_filter: bool | None,
    audio_diarization: bool,
    audio_min_speakers: int,
    audio_max_speakers: int,
    audio_speaker_aliases_json: str,
    audio_vocabulary_pack_ids: str,
    audio_confidence_heatmap: bool | None,
    audio_quality_diagnostics: bool | None,
    audio_review_required_on_low_confidence: bool,
    audio_text_enhancement_enabled: bool,
    audio_text_enhancement_strength: int,
    audio_structural_enhancement_enabled: bool,
    audio_structural_enhancement_mode: str,
    audio_enhancement_allow_cloud: bool,
    audio_fusion_mode: str,
    audio_contradiction_detection: bool,
    audio_allow_cloud_stt: bool,
    audio_benchmark_compare: bool,
    audio_compare_providers: str,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if audio_provider:
        options["audio_provider"] = audio_provider.strip()
    if audio_language:
        options["audio_language"] = audio_language.strip()
    if audio_device:
        options["audio_device"] = audio_device.strip()
    if audio_compute_type:
        options["audio_compute_type"] = audio_compute_type.strip()
    if audio_beam_size > 0:
        options["audio_beam_size"] = audio_beam_size
    if audio_vad_filter is not None:
        options["audio_vad_filter"] = audio_vad_filter
    if audio_diarization:
        options["audio_diarization"] = True
    if audio_min_speakers > 0:
        options["audio_min_speakers"] = audio_min_speakers
    if audio_max_speakers > 0:
        options["audio_max_speakers"] = audio_max_speakers
    aliases = _json_string_map(audio_speaker_aliases_json)
    if aliases:
        options["audio_speaker_aliases"] = aliases
    pack_ids = _string_list(audio_vocabulary_pack_ids)
    if pack_ids:
        options["audio_vocabulary_pack_ids"] = pack_ids
    if audio_confidence_heatmap is not None:
        options["audio_confidence_heatmap"] = audio_confidence_heatmap
    if audio_quality_diagnostics is not None:
        options["audio_quality_diagnostics"] = audio_quality_diagnostics
    if audio_review_required_on_low_confidence:
        options["audio_review_required_on_low_confidence"] = True
    if audio_text_enhancement_enabled:
        options["audio_text_enhancement_enabled"] = True
    if audio_text_enhancement_strength:
        options["audio_text_enhancement_strength"] = audio_text_enhancement_strength
    if audio_structural_enhancement_enabled:
        options["audio_structural_enhancement_enabled"] = True
    if audio_structural_enhancement_mode:
        options["audio_structural_enhancement_mode"] = audio_structural_enhancement_mode.strip()
    if audio_enhancement_allow_cloud:
        options["audio_enhancement_allow_cloud"] = True
    if audio_fusion_mode:
        options["audio_fusion_mode"] = audio_fusion_mode.strip()
    if audio_contradiction_detection:
        options["audio_contradiction_detection"] = True
    if audio_allow_cloud_stt:
        options["audio_allow_cloud_stt"] = True
    if audio_benchmark_compare:
        options["audio_benchmark_compare"] = True
    compare_providers = _string_list(audio_compare_providers)
    if compare_providers:
        options["audio_compare_providers"] = compare_providers
    return options


def _json_string_map(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    parsed = parse_extra_options_json(raw)
    return {
        str(key).strip(): str(value).strip()
        for key, value in parsed.items()
        if str(key).strip() and str(value).strip()
    }


def _string_list(raw: str) -> list[str]:
    if not raw.strip():
        return []
    stripped = raw.strip()
    if stripped.startswith("["):
        data = parse_extra_options_json(f'{{"items": {stripped}}}').get("items")
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    return [part.strip() for part in stripped.split(",") if part.strip()]


async def _client_workspace_roots(ctx: Context | None) -> list[Path] | None:
    if ctx is None:
        return None
    try:
        result = await ctx.session.list_roots()
    except Exception:
        return None
    roots = getattr(result, "roots", None)
    if roots is None:
        return None
    paths: list[Path] = []
    for root in roots:
        path = _path_from_root_uri(getattr(root, "uri", None))
        if path is not None:
            paths.append(path)
    return paths


def _path_from_root_uri(uri: Any) -> Path | None:
    if uri is None:
        return None
    parsed = urlparse(str(uri))
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path or "")
        if parsed.netloc:
            raw_path = f"//{parsed.netloc}{raw_path}"
    else:
        raw_path = unquote(str(uri))
    if os.name == "nt" and raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    if not raw_path:
        return None
    return Path(raw_path).expanduser()


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _auth_base_url(host: str, port: int) -> str:
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in url_host and not url_host.startswith("["):
        url_host = f"[{url_host}]"
    return f"http://{url_host}:{port}"


def _image_understanding_extra_options(
    *,
    router_enabled: bool | None,
    smart_router_level: str,
    dedup_enabled: bool | None,
    downscale_vlm_crops: bool | None,
    batch_enabled: bool | None,
    ocr_engine: str,
    decorative_max_text_density: float,
    ocr_min_text_density: float,
    ocr_min_lines: int,
    dedup_max_distance: int,
    vlm_crop_max_px: int,
    vlm_batch_size: int,
    max_batch_retries: int,
    hybrid_ocr_profile: str = "",
    hybrid_ocr_require_specialists: bool | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if router_enabled is not None:
        options["router_enabled"] = router_enabled
    if dedup_enabled is not None:
        options["dedup_enabled"] = dedup_enabled
    if downscale_vlm_crops is not None:
        options["downscale_vlm_crops"] = downscale_vlm_crops
    if batch_enabled is not None:
        options["batch_enabled"] = batch_enabled
    if smart_router_level in {"disabled", "smart", "beeg_brain"}:
        options["smart_router_level"] = smart_router_level
    if ocr_engine in {"surya", "hybrid_ocr"}:
        options["ocr_engine"] = ocr_engine
    if hybrid_ocr_profile in {"balanced", "max_accuracy", "low_vram"}:
        options["hybrid_ocr_profile"] = hybrid_ocr_profile
    if hybrid_ocr_require_specialists is not None:
        options["hybrid_ocr_require_specialists"] = hybrid_ocr_require_specialists
    if decorative_max_text_density >= 0:
        options["decorative_max_text_density"] = decorative_max_text_density
    if ocr_min_text_density >= 0:
        options["ocr_min_text_density"] = ocr_min_text_density
    if ocr_min_lines > 0:
        options["ocr_min_lines"] = ocr_min_lines
    if dedup_max_distance >= 0:
        options["dedup_max_distance"] = dedup_max_distance
    if vlm_crop_max_px > 0:
        options["vlm_crop_max_px"] = vlm_crop_max_px
    if vlm_batch_size > 0:
        options["vlm_batch_size"] = vlm_batch_size
    if max_batch_retries >= 0:
        options["max_batch_retries"] = max_batch_retries
    return options


def _agent_productivity_extra_options(
    *,
    text_data_max_rows: int = 0,
    archive_max_files: int = 0,
    archive_inline_bytes: int = 0,
    archive_max_child_bytes: int = 0,
    archive_max_total_uncompressed_bytes: int = 0,
    archive_max_compression_ratio: float = 0.0,
    archive_max_depth: int = -1,
    archive_max_converted_children: int = 0,
    archive_recursive: bool | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if archive_recursive is not None:
        options["archive_recursive"] = archive_recursive
    if text_data_max_rows > 0:
        options["text_data_max_rows"] = text_data_max_rows
    if archive_max_files > 0:
        options["archive_max_files"] = archive_max_files
    if archive_inline_bytes > 0:
        options["archive_inline_bytes"] = archive_inline_bytes
    if archive_max_child_bytes > 0:
        options["archive_max_child_bytes"] = archive_max_child_bytes
    if archive_max_total_uncompressed_bytes > 0:
        options["archive_max_total_uncompressed_bytes"] = archive_max_total_uncompressed_bytes
    if archive_max_compression_ratio > 0:
        options["archive_max_compression_ratio"] = archive_max_compression_ratio
    if archive_max_depth >= 0:
        options["archive_max_depth"] = archive_max_depth
    if archive_max_converted_children > 0:
        options["archive_max_converted_children"] = archive_max_converted_children
    return options


if __name__ == "__main__":
    run()
