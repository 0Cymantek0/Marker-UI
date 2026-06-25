"""MCP server for Marker.

Default transport is stdio for local coding agents. Streamable HTTP is also
available for multi-client local/remote deployments.
"""

import ipaddress
import os
import secrets
from typing import Annotated, Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from app.agent_api import (
    AgentConversionOptions,
    MAX_READ_CHARS,
    SERVICE_NAME,
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
    read_output,
    set_setting,
    self_test,
    submit_conversion_job,
)


class StaticTokenVerifier:
    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="marker-mcp", scopes=["marker:mcp"])
        return None


class MarkerOutputModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class CapabilitiesOutput(MarkerOutputModel):
    service: str = Field(description="Service identifier.", examples=[SERVICE_NAME])
    tools: list[str] = Field(description="Available MCP tool names.", examples=[["marker_convert_file"]])
    allowed_extensions: list[str] = Field(description="Supported file extensions.", examples=[[".pdf", ".csv"]])
    output_formats: list[str] = Field(description="Supported output formats.", examples=[["markdown", "json"]])
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
    offset: int = Field(description="Returned chunk start offset.", examples=[0])
    limit: int = Field(description="Requested maximum characters.", examples=[20000])
    text: str = Field(description="Output text chunk.", examples=["# Converted"])
    text_chars: int = Field(description="Total text characters in file.", examples=[50000])
    has_more: bool = Field(description="True when more text remains.", examples=[True])
    next_offset: int | None = Field(default=None, description="Offset for next page.", examples=[20000])


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
    files_removed: bool = Field(description="True when files were removed.", examples=[True])


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
    capabilities_ok: bool = Field(description="Capability check result.", examples=[True])
    conversion_ok: bool | None = Field(default=None, description="Conversion smoke result when run.", examples=[True])
    notes: list[str] = Field(description="Diagnostic notes.", examples=[[]])
    registered_tools: list[str] | None = Field(default=None, description="Registered MCP tool names.", examples=[["marker_convert_file"]])
    tools_ok: bool | None = Field(default=None, description="Tool registration check result.", examples=[True])


PathParam = Annotated[str, Field(description="Local file path. Example: C:\\path\\to\\document.pdf.", examples=["C:\\path\\to\\document.pdf"])]
UrlParam = Annotated[str, Field(description="Public http(s) URL. Example: https://example.com/document.pdf.", examples=["https://example.com/document.pdf"])]
DirParam = Annotated[str, Field(description="Output directory path. Example: C:\\path\\to\\out.", examples=["C:\\path\\to\\out"])]
OutputPathParam = Annotated[str, Field(description="Exact output file path that must not already exist.", examples=["C:\\path\\to\\out\\document.md"])]
OutputFormatParam = Annotated[str, Field(description="Output format: markdown, json, html, or chunks.", examples=["markdown"])]
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
DensityParam = Annotated[float, Field(ge=-1.0, le=1.0, description="Optional density threshold from 0 to 1; -1 means unset.", examples=[0.2])]
ConfidenceParam = Annotated[float, Field(ge=-1.0, le=1.0, description="Optional confidence threshold from 0 to 1; -1 means unset.", examples=[0.35])]
JobIdParam = Annotated[str, Field(description="Conversion job id.", examples=["11111111-1111-4111-8111-111111111111"])]
SettingKeyParam = Annotated[str, Field(description="Settings key.", examples=["openai_api_key"])]
SettingValueParam = Annotated[str, Field(description="Settings value; sensitive keys are encrypted on write.", examples=["dummy-api-key"])]
CategoryParam = Annotated[str, Field(description="Settings category.", examples=["llm"])]
PageParam = Annotated[int, Field(ge=1, description="One-based page number.", examples=[1])]
PageSizeParam = Annotated[int, Field(ge=1, le=100, description="Items per page.", examples=[20])]
OffsetParam = Annotated[int, Field(ge=0, description="Character offset.", examples=[0])]
LimitParam = Annotated[int, Field(ge=1, le=MAX_READ_CHARS, description="Maximum characters to return.", examples=[20000])]
SizeParam = Annotated[int, Field(ge=0, description="Input size in bytes for metadata-only planning.", examples=[1048576])]


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

    return capabilities()


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
    return await plan_conversion(
        local_file_path=_none_if_blank(local_file_path),
        filename=_none_if_blank(filename),
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
        "openWorldHint": False,
    },
)
async def marker_convert_file(
    local_file_path: PathParam = "",
    source_url: UrlParam = "",
    output_dir: DirParam = "",
    output_path: OutputPathParam = "",
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
    audio_output_mode: Annotated[str, Field(description="Audio output mode: transcript, enhanced, notes, meeting_notes, or lecture_notes.", examples=["transcript"])] = "",
    audio_model: Annotated[str, Field(description="Audio transcription model id.", examples=["base"])] = "",
    audio_vocabulary: TextParam = "",
    audio_context: TextParam = "",
    audio_low_confidence_threshold: ConfidenceParam = -1.0,
    audio_word_timestamps: Annotated[bool, Field(description="Include word-level timestamps for audio.", examples=[False])] = False,
    disable_multiprocessing: Annotated[bool, Field(description="Disable multiprocessing during conversion.", examples=[False])] = False,
    strip_existing_ocr: Annotated[bool, Field(description="Strip existing OCR text before re-OCR.", examples=[False])] = False,
    redo_inline_math: Annotated[bool, Field(description="Reprocess inline math.", examples=[False])] = False,
    debug: Annotated[bool, Field(description="Enable debug conversion artifacts/logging.", examples=[False])] = False,
    router_enabled: OptionalBoolParam = None,
    smart_router_level: Annotated[str, Field(description="Image router level: disabled, smart, or beeg_brain.", examples=["smart"])] = "",
    dedup_enabled: OptionalBoolParam = None,
    downscale_vlm_crops: OptionalBoolParam = None,
    batch_enabled: OptionalBoolParam = None,
    ocr_engine: Annotated[str, Field(description="OCR engine: surya, glm_ocr, paddleocr_vl, or mistral_ocr.", examples=["surya"])] = "",
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
                archive_max_depth=archive_max_depth,
                archive_max_converted_children=archive_max_converted_children,
                archive_recursive=archive_recursive,
            ),
            **parse_extra_options_json(extra_options_json),
        },
    )
    return await convert_document(
        local_file_path=_none_if_blank(local_file_path),
        source_url=_none_if_blank(source_url),
        output_dir=_none_if_blank(output_dir),
        output_path=_none_if_blank(output_path),
        max_chars=max_chars,
        options=options,
    )


@mcp.tool(
    name="marker_submit_job",
    title="Submit Marker Job",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def marker_submit_job(
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
    audio_output_mode: Annotated[str, Field(description="Audio output mode: transcript, enhanced, notes, meeting_notes, or lecture_notes.", examples=["transcript"])] = "",
    audio_model: Annotated[str, Field(description="Audio transcription model id.", examples=["base"])] = "",
    audio_vocabulary: TextParam = "",
    audio_context: TextParam = "",
    audio_low_confidence_threshold: ConfidenceParam = -1.0,
    audio_word_timestamps: Annotated[bool, Field(description="Include word-level timestamps for audio.", examples=[False])] = False,
    disable_multiprocessing: Annotated[bool, Field(description="Disable multiprocessing during conversion.", examples=[False])] = False,
    strip_existing_ocr: Annotated[bool, Field(description="Strip existing OCR text before re-OCR.", examples=[False])] = False,
    redo_inline_math: Annotated[bool, Field(description="Reprocess inline math.", examples=[False])] = False,
    debug: Annotated[bool, Field(description="Enable debug conversion artifacts/logging.", examples=[False])] = False,
    text_data_max_rows: PositiveRowsParam = 0,
    archive_max_files: PositiveRowsParam = 0,
    archive_inline_bytes: PositivePixelsParam = 0,
    archive_max_child_bytes: PositivePixelsParam = 0,
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
                archive_max_depth=archive_max_depth,
                archive_max_converted_children=archive_max_converted_children,
                archive_recursive=archive_recursive,
            ),
            **parse_extra_options_json(extra_options_json),
        },
    )
    return await submit_conversion_job(
        local_file_path=_none_if_blank(local_file_path),
        source_url=_none_if_blank(source_url),
        output_dir=_none_if_blank(output_dir),
        options=options,
    )


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

    return read_output(output_path, offset=offset, limit=limit)


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

    return await get_job_status(
        job_id,
        include_result_text=include_result_text,
        max_chars=max_chars,
    )


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
) -> DeleteJobOutput:
    """Cancel/delete one job and optionally remove its upload/output files."""

    return await delete_job(job_id, delete_files=delete_files)


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
) -> SettingOutput:
    """Read one persisted setting with sensitive values masked."""

    return await get_setting(key)


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
) -> DeleteSettingOutput:
    """Delete one persisted setting key."""

    return await delete_setting(key)


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
    data["registered_tools"] = sorted(tool.name for tool in registered)
    data["tools_ok"] = sorted(data["expected_tools"]) == data["registered_tools"]
    return data


def run(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_token: str | None = None,
) -> None:
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
            issuer_url = _auth_base_url(host, port)
            mcp.settings.auth = AuthSettings(
                issuer_url=issuer_url,
                resource_server_url=issuer_url,
                required_scopes=["marker:mcp"],
            )
            mcp._token_verifier = StaticTokenVerifier(token)
        else:
            mcp.settings.auth = None
            mcp._token_verifier = None
    mcp.run(transport=transport)


def _none_if_blank(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None


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
    if ocr_engine in {"surya", "glm_ocr", "paddleocr_vl", "mistral_ocr"}:
        options["ocr_engine"] = ocr_engine
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
    if archive_max_depth >= 0:
        options["archive_max_depth"] = archive_max_depth
    if archive_max_converted_children > 0:
        options["archive_max_converted_children"] = archive_max_converted_children
    return options


if __name__ == "__main__":
    run()
