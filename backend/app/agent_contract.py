"""Shared typed contracts for Marker agent-facing surfaces.

Keep this module free of Marker/Surya imports. CLI, MCP, REST documentation,
and tests can import it to produce stable JSON schemas without warming neural
models or importing converter implementations.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ERROR_SCHEMA_VERSION
from app.services.output_writer import OUTPUT_MANIFEST_SCHEMA_VERSION


CONTRACT_SCHEMA_VERSION = "marker.agent_contract.v1"

OutputFormat = Literal["markdown", "json", "html", "chunks"]
ImageHandlingMode = Literal["extraction", "understanding", "both"]
ConversionProfile = Literal["auto", "fast", "high_accuracy"]
AudioOutputMode = Literal["transcript", "enhanced", "notes", "meeting_notes", "lecture_notes"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FlexibleContractModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ConversionOptionsModel(ContractModel):
    output_format: OutputFormat = "markdown"
    converter_cls: str | None = None
    engine_override: str | None = None
    conversion_profile: ConversionProfile | None = None
    use_llm: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    image_handling_mode: ImageHandlingMode = "extraction"
    allow_cloud_vlm: bool = False
    force_ocr: bool = False
    paginate_output: bool = False
    disable_image_extraction: bool = False
    page_range: str | None = None
    lang: str | None = None
    audio_output_mode: AudioOutputMode | None = None
    audio_model: str | None = None
    audio_vocabulary: str | None = None
    audio_context: str | None = None
    audio_low_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    audio_word_timestamps: bool = False
    disable_multiprocessing: bool = False
    strip_existing_ocr: bool = False
    redo_inline_math: bool = False
    debug: bool = False
    extra_options: dict[str, Any] = Field(default_factory=dict)


class PlanRequestModel(ContractModel):
    local_file_path: str | None = None
    filename: str | None = None
    size: int = Field(default=0, ge=0)
    options: ConversionOptionsModel = Field(default_factory=ConversionOptionsModel)


class PlanResultModel(FlexibleContractModel):
    filename: str
    size: int = Field(ge=0)
    preliminary: bool
    plan: dict[str, Any]
    probe_result: dict[str, Any] | None = None
    mixed_engine_segments: list[dict[str, Any]] | None = None


class ConvertRequestModel(ContractModel):
    local_file_path: str | None = None
    source_url: str | None = None
    output_dir: str | None = None
    output_path: str | None = None
    max_chars: int = Field(default=20_000, ge=0, le=100_000)
    options: ConversionOptionsModel = Field(default_factory=ConversionOptionsModel)


class OutputPathsModel(ContractModel):
    text_path: str
    manifest_path: str | None = None
    asset_paths: list[str] = Field(default_factory=list)
    media_type: str = "text/markdown"


class ConvertResultModel(FlexibleContractModel):
    ok: bool
    source: dict[str, Any]
    output: OutputPathsModel
    text_preview: str
    text_chars: int = Field(ge=0)
    truncated: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    next_step: str | None = None


class SubmitJobRequestModel(ContractModel):
    local_file_path: str | None = None
    source_url: str | None = None
    output_dir: str | None = None
    options: ConversionOptionsModel = Field(default_factory=ConversionOptionsModel)


class SubmitJobResultModel(FlexibleContractModel):
    job_id: str
    status: str
    filename: str
    output_format: OutputFormat | str
    next_step: str


class JobStatusModel(FlexibleContractModel):
    job_id: str
    status: str
    progress: int | None = Field(default=None, ge=0, le=100)
    filename: str | None = None
    input_format: str | None = None
    output_format: str | None = None
    converter: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    error_message: str | None = None
    result_path: str | None = None
    result_chars: int | None = Field(default=None, ge=0)
    conversion_metadata: dict[str, Any] = Field(default_factory=dict)
    result_text: str | None = None
    truncated: bool | None = None
    next_step: str | None = None


class OutputManifestOutputModel(ContractModel):
    final_path: str
    text_path: str
    manifest_path: str
    media_type: str
    text_chars: int = Field(ge=0)
    text_sha256: str
    asset_count: int = Field(ge=0)
    assets: list[dict[str, Any]] = Field(default_factory=list)


class OutputManifestModel(ContractModel):
    schema_version: Literal["marker.output_manifest.v1"] = OUTPUT_MANIFEST_SCHEMA_VERSION
    created_at: str
    job_id: str | None = None
    source: dict[str, Any]
    output: OutputManifestOutputModel
    conversion: dict[str, Any]


class MarkerErrorModel(ContractModel):
    schema_version: Literal["marker.error.v1"] = ERROR_SCHEMA_VERSION
    ok: Literal[False] = False
    error: dict[str, Any]


class BatchRequestItemModel(ContractModel):
    local_file_path: str | None = None
    source_url: str | None = None
    output_dir: str | None = None
    output_path: str | None = None
    options: ConversionOptionsModel = Field(default_factory=ConversionOptionsModel)


class BatchRequestModel(ContractModel):
    items: list[BatchRequestItemModel]
    continue_on_error: bool = True
    resume: bool = False


class BatchResultModel(FlexibleContractModel):
    ok: bool
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(default=0, ge=0)
    results_path: str | None = None
    failed_path: str | None = None
    exit_code: int = 0


class OptionMetadataModel(ContractModel):
    name: str
    cli_flag: str | None = None
    mcp_name: str | None = None
    type: str
    default: Any = None
    category: str
    description: str


OPTION_METADATA: tuple[OptionMetadataModel, ...] = (
    OptionMetadataModel(name="output_format", cli_flag="--output-format", type="enum", default="markdown", category="output", description="Output format: markdown, json, html, or chunks."),
    OptionMetadataModel(name="converter_cls", cli_flag="--converter-cls", type="string", category="routing", description="Optional Marker converter class override."),
    OptionMetadataModel(name="engine_override", cli_flag="--engine-override", type="string", category="routing", description="Optional universal converter engine override."),
    OptionMetadataModel(name="conversion_profile", cli_flag="--conversion-profile", type="enum", category="routing", description="Conversion policy profile."),
    OptionMetadataModel(name="use_llm", cli_flag="--use-llm", type="boolean", default=False, category="llm", description="Enable LLM-assisted conversion."),
    OptionMetadataModel(name="llm_provider", cli_flag="--llm-provider", type="string", category="llm", description="Configured provider id."),
    OptionMetadataModel(name="llm_model", cli_flag="--llm-model", type="string", category="llm", description="Configured model id."),
    OptionMetadataModel(name="image_handling_mode", cli_flag="--image-handling-mode", type="enum", default="extraction", category="images", description="Image extraction/understanding mode."),
    OptionMetadataModel(name="allow_cloud_vlm", cli_flag="--allow-cloud-vlm", type="boolean", default=False, category="images", description="Explicitly allow cloud VLM calls."),
    OptionMetadataModel(name="force_ocr", cli_flag="--force-ocr", type="boolean", default=False, category="pdf", description="Force OCR even when text exists."),
    OptionMetadataModel(name="paginate_output", cli_flag="--paginate-output", type="boolean", default=False, category="output", description="Paginate generated output where supported."),
    OptionMetadataModel(name="disable_image_extraction", cli_flag="--disable-image-extraction", type="boolean", default=False, category="images", description="Skip extracted image sidecars."),
    OptionMetadataModel(name="page_range", cli_flag="--page-range", type="string", category="pdf", description="PDF page range such as 1-3,5."),
    OptionMetadataModel(name="lang", cli_flag="--lang", type="string", category="ocr", description="OCR language hint."),
    OptionMetadataModel(name="audio_output_mode", cli_flag="--audio-output-mode", type="enum", category="audio", description="Audio transcript or note mode."),
    OptionMetadataModel(name="audio_model", cli_flag="--audio-model", type="string", category="audio", description="Local audio transcription model."),
    OptionMetadataModel(name="audio_vocabulary", cli_flag="--audio-vocabulary", type="string", category="audio", description="Vocabulary hints for audio transcription."),
    OptionMetadataModel(name="audio_context", cli_flag="--audio-context", type="string", category="audio", description="Context used to organize audio output."),
    OptionMetadataModel(name="audio_low_confidence_threshold", cli_flag="--audio-low-confidence-threshold", type="number", category="audio", description="Audio low-confidence threshold."),
    OptionMetadataModel(name="audio_word_timestamps", cli_flag="--audio-word-timestamps", type="boolean", default=False, category="audio", description="Request word-level timestamps."),
    OptionMetadataModel(name="disable_multiprocessing", cli_flag="--disable-multiprocessing", type="boolean", default=False, category="runtime", description="Run conversion single-threaded where supported."),
    OptionMetadataModel(name="strip_existing_ocr", cli_flag="--strip-existing-ocr", type="boolean", default=False, category="pdf", description="Strip existing OCR text before re-OCR."),
    OptionMetadataModel(name="redo_inline_math", cli_flag="--redo-inline-math", type="boolean", default=False, category="pdf", description="Reprocess inline math."),
    OptionMetadataModel(name="debug", cli_flag="--debug", type="boolean", default=False, category="runtime", description="Enable debug conversion artifacts/logging."),
    OptionMetadataModel(name="extra_options", cli_flag="--option/--options-json", type="object", default={}, category="advanced", description="Advanced GUI-compatible backend options."),
)


CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "ConversionOptionsModel": ConversionOptionsModel,
    "PlanRequestModel": PlanRequestModel,
    "PlanResultModel": PlanResultModel,
    "ConvertRequestModel": ConvertRequestModel,
    "ConvertResultModel": ConvertResultModel,
    "SubmitJobRequestModel": SubmitJobRequestModel,
    "SubmitJobResultModel": SubmitJobResultModel,
    "JobStatusModel": JobStatusModel,
    "OutputManifestModel": OutputManifestModel,
    "MarkerErrorModel": MarkerErrorModel,
    "BatchRequestModel": BatchRequestModel,
    "BatchResultModel": BatchResultModel,
    "OptionMetadataModel": OptionMetadataModel,
}


def export_json_schemas() -> dict[str, Any]:
    """Return JSON schemas and option metadata for agent-facing contracts."""

    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "models": {
            name: model.model_json_schema()
            for name, model in CONTRACT_MODELS.items()
        },
        "option_metadata": [
            item.model_dump(mode="json")
            for item in OPTION_METADATA
        ],
    }
