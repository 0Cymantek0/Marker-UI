"""Shared typed contracts for Marker agent-facing surfaces.

Keep this module free of Marker/Surya imports. CLI, MCP, REST documentation,
and tests can import it to produce stable JSON schemas without warming neural
models or importing converter implementations.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.conversion.formats import OUTPUT_FORMATS, OUTPUT_FORMATS_DESCRIPTION, OutputFormat
from app.errors import ERROR_SCHEMA_VERSION
from app.services.output_writer import OUTPUT_MANIFEST_SCHEMA_VERSION


CONTRACT_SCHEMA_VERSION = "marker.agent_contract.v1"

ImageHandlingMode = Literal["extraction", "understanding", "both"]
ConversionProfile = Literal["auto", "fast", "high_accuracy"]
AudioOutputMode = Literal[
    "transcript",
    "enhanced",
    "notes",
    "meeting_notes",
    "lecture_notes",
    "interview_qna",
    "action_decision_log",
]
OcrEngine = Literal["surya", "hybrid_ocr"]
HybridOcrProfile = Literal["balanced", "max_accuracy", "low_vram"]
SmartRouterLevel = Literal["disabled", "smart", "beeg_brain"]

AUDIO_OUTPUT_MODES: tuple[AudioOutputMode, ...] = (
    "transcript",
    "enhanced",
    "notes",
    "meeting_notes",
    "lecture_notes",
    "interview_qna",
    "action_decision_log",
)


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

    # --- Advanced Audio & Voice Notes (plan §5.5 + §23.12).
    # Local-first by default. Every field below defaults to the current
    # behavior, so a job that sends none of them is indistinguishable from a
    # pre-enhancement-layer job.
    audio_provider: str = "local_faster_whisper"
    audio_language: str | None = None
    audio_device: str | None = None
    audio_compute_type: str | None = None
    audio_beam_size: int | None = Field(default=None, ge=1)
    audio_vad_filter: bool | None = None
    audio_gap_warning_ms: int | None = Field(default=None, ge=0)

    # Speaker / diarization (plan §10). Diarization is opt-in; without it the
    # transcript falls back to anonymous ``speaker_0`` labels.
    audio_diarization: bool = False
    audio_min_speakers: int | None = Field(default=None, ge=1)
    audio_max_speakers: int | None = Field(default=None, ge=1)
    audio_speaker_aliases: dict[str, str] = Field(default_factory=dict)
    audio_speaker_memory: bool = False
    audio_speaker_memory_scope: str = "machine"

    # Vocabulary packs (plan §9). Packs are resolved server-side from their ids
    # into terms, then merged with the free-text ``audio_vocabulary`` field.
    audio_vocabulary_pack_ids: list[str] = Field(default_factory=list)

    # Quality / confidence (plan §8).
    audio_confidence_heatmap: bool = True
    audio_quality_diagnostics: bool = True
    audio_review_required_on_low_confidence: bool = False

    # Enhancement layer (plan §23). Textual strength 0-5 controls how much the
    # transcript wording may be altered; structural enhancement reorganizes the
    # transcript into a document shape. The two are independent — structural-only
    # mode must preserve the original transcript words exactly.
    audio_text_enhancement_enabled: bool = False
    audio_text_enhancement_strength: int = Field(default=0, ge=0, le=5)
    audio_text_enhancement_provider: str = "local_rule_based"
    audio_text_enhancement_model: str | None = None
    audio_structural_enhancement_enabled: bool = False
    audio_structural_enhancement_mode: str = "auto"
    audio_structural_preserve_words: bool = True
    audio_enhancement_require_source_refs: bool = True
    audio_enhancement_show_diff: bool = True
    audio_enhancement_include_audit: bool = True
    audio_enhancement_fallback_on_validation_failure: bool = True
    audio_enhancement_allow_cloud: bool = False
    audio_enhancement_custom_instructions: str | None = None

    # Fusion (plan §12). Context documents are converted server-side and given
    # distinct source ids; the transcript stays authoritative for what was said.
    audio_fusion_mode: str | None = None
    audio_contradiction_detection: bool = False
    audio_context_trust_policy: str = "transcript_wins"

    # Privacy / providers (plan §3.1). Cloud STT and cloud enhancement each
    # require explicit opt-in before any audio leaves the machine.
    audio_allow_cloud_stt: bool = False

    # Benchmark (plan §13). Reserved until the benchmark runner and at least two
    # shipped STT adapters exist; current builds reject this flag early.
    audio_benchmark_compare: bool = False
    audio_compare_providers: list[str] = Field(default_factory=list)
    audio_compare_metrics: list[str] = Field(default_factory=list)
    disable_multiprocessing: bool = False
    strip_existing_ocr: bool = False
    redo_inline_math: bool = False
    ocr_engine: OcrEngine = "surya"
    hybrid_ocr_profile: HybridOcrProfile = "balanced"
    hybrid_ocr_require_specialists: bool = False
    debug: bool = False

    # Productivity, archive, and image-understanding knobs exposed by CLI/REST/MCP.
    # Keep them first-class here so JSON agent callers get validation and schema
    # docs instead of needing to tunnel stable options through extra_options.
    text_data_max_rows: int | None = Field(default=None, ge=1)
    archive_recursive: bool | None = None
    archive_max_files: int | None = Field(default=None, ge=1)
    archive_inline_bytes: int | None = Field(default=None, ge=1)
    archive_max_child_bytes: int | None = Field(default=None, ge=1)
    archive_max_total_uncompressed_bytes: int | None = Field(default=None, ge=1)
    archive_max_compression_ratio: float | None = Field(default=None, ge=1.0)
    archive_max_depth: int | None = Field(default=None, ge=0)
    archive_max_converted_children: int | None = Field(default=None, ge=1)
    router_enabled: bool | None = None
    smart_router_level: SmartRouterLevel | None = None
    dedup_enabled: bool | None = None
    downscale_vlm_crops: bool | None = None
    batch_enabled: bool | None = None
    decorative_max_text_density: float | None = Field(default=None, ge=0.0, le=1.0)
    ocr_min_text_density: float | None = Field(default=None, ge=0.0, le=1.0)
    ocr_min_lines: int | None = Field(default=None, ge=1)
    dedup_max_distance: int | None = Field(default=None, ge=0, le=64)
    vlm_crop_max_px: int | None = Field(default=None, ge=64, le=4096)
    vlm_batch_size: int | None = Field(default=None, ge=1, le=64)
    max_batch_retries: int | None = Field(default=None, ge=0, le=5)
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
    overwrite: bool = False
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
    OptionMetadataModel(name="output_format", cli_flag="--output-format", type="enum", default="markdown", category="output", description=f"Output format: {OUTPUT_FORMATS_DESCRIPTION}."),
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
    OptionMetadataModel(name="ocr_engine", cli_flag="--ocr-engine", type="enum", default="surya", category="ocr", description="Local OCR engine: surya or hybrid_ocr."),
    OptionMetadataModel(name="hybrid_ocr_profile", cli_flag="--hybrid-ocr-profile", type="enum", default="balanced", category="ocr", description="Hybrid OCR profile: balanced, max_accuracy, or low_vram."),
    OptionMetadataModel(name="hybrid_ocr_require_specialists", cli_flag="--hybrid-ocr-require-specialists", type="boolean", default=False, category="ocr", description="Fail if Hybrid OCR specialists are unavailable."),
    OptionMetadataModel(name="router_enabled", cli_flag="--router-enabled", type="boolean", category="images", description="Override the image-understanding Tier-0 router switch."),
    OptionMetadataModel(name="smart_router_level", cli_flag="--smart-router-level", type="enum", category="images", description="Image routing brain: disabled, smart, or beeg_brain."),
    OptionMetadataModel(name="dedup_enabled", cli_flag="--dedup-enabled", type="boolean", category="images", description="Collapse duplicate image crops before extraction."),
    OptionMetadataModel(name="downscale_vlm_crops", cli_flag="--downscale-vlm-crops", type="boolean", category="images", description="Downscale crops before VLM calls."),
    OptionMetadataModel(name="batch_enabled", cli_flag="--batch-enabled", type="boolean", category="images", description="Batch image routing and extraction calls."),
    OptionMetadataModel(name="decorative_max_text_density", cli_flag="--decorative-max-text-density", type="number", category="images", description="Text-density threshold at or below which an image is decorative."),
    OptionMetadataModel(name="ocr_min_text_density", cli_flag="--ocr-min-text-density", type="number", category="images", description="Text-density threshold at or above which an image routes to local OCR."),
    OptionMetadataModel(name="ocr_min_lines", cli_flag="--ocr-min-lines", type="integer", category="images", description="Minimum detected text lines before OCR routing."),
    OptionMetadataModel(name="dedup_max_distance", cli_flag="--dedup-max-distance", type="integer", category="images", description="Maximum aHash Hamming distance treated as duplicate."),
    OptionMetadataModel(name="vlm_crop_max_px", cli_flag="--vlm-crop-max-px", type="integer", category="images", description="Longest-side pixel cap for crops sent to VLM providers."),
    OptionMetadataModel(name="vlm_batch_size", cli_flag="--vlm-batch-size", type="integer", category="images", description="Maximum images per batched VLM call."),
    OptionMetadataModel(name="max_batch_retries", cli_flag="--max-batch-retries", type="integer", category="images", description="Extra batch attempts used to recover missing or malformed VLM responses."),
    OptionMetadataModel(name="text_data_max_rows", cli_flag="--text-data-max-rows", type="integer", category="text_data", description="Maximum rows rendered for text/data table inputs."),
    OptionMetadataModel(name="archive_recursive", cli_flag="--archive-recursive", type="boolean", category="archives", description="Recursively convert deterministic safe children inside archives."),
    OptionMetadataModel(name="archive_max_files", cli_flag="--archive-max-files", type="integer", category="archives", description="Maximum files scanned inside an archive."),
    OptionMetadataModel(name="archive_inline_bytes", cli_flag="--archive-inline-bytes", type="integer", category="archives", description="Maximum small child bytes to inline in archive output."),
    OptionMetadataModel(name="archive_max_child_bytes", cli_flag="--archive-max-child-bytes", type="integer", category="archives", description="Maximum child file size to parse from an archive."),
    OptionMetadataModel(name="archive_max_total_uncompressed_bytes", cli_flag="--archive-max-total-uncompressed-bytes", type="integer", category="archives", description="Maximum total uncompressed archive bytes to inspect."),
    OptionMetadataModel(name="archive_max_compression_ratio", cli_flag="--archive-max-compression-ratio", type="number", category="archives", description="Maximum allowed archive entry compression ratio."),
    OptionMetadataModel(name="archive_max_depth", cli_flag="--archive-max-depth", type="integer", category="archives", description="Maximum recursive archive depth."),
    OptionMetadataModel(name="archive_max_converted_children", cli_flag="--archive-max-converted-children", type="integer", category="archives", description="Maximum child files converted inside an archive."),
    OptionMetadataModel(name="audio_output_mode", cli_flag="--audio-output-mode", type="enum", category="audio", description="Audio transcript or note mode."),
    OptionMetadataModel(name="audio_model", cli_flag="--audio-model", type="string", category="audio", description="Local audio transcription model."),
    OptionMetadataModel(name="audio_vocabulary", cli_flag="--audio-vocabulary", type="string", category="audio", description="Vocabulary hints for audio transcription."),
    OptionMetadataModel(name="audio_context", cli_flag="--audio-context", type="string", category="audio", description="Context used to organize audio output."),
    OptionMetadataModel(name="audio_low_confidence_threshold", cli_flag="--audio-low-confidence-threshold", type="number", category="audio", description="Audio low-confidence threshold."),
    OptionMetadataModel(name="audio_word_timestamps", cli_flag="--audio-word-timestamps", type="boolean", default=False, category="audio", description="Request word-level timestamps."),
    OptionMetadataModel(name="audio_provider", cli_flag="--audio-provider", type="enum", default="local_faster_whisper", category="audio", description="STT provider: local_faster_whisper, local_whisperx, openai, groq, deepgram, assemblyai, azure."),
    OptionMetadataModel(name="audio_language", cli_flag="--audio-language", type="string", category="audio", description="Spoken language hint for transcription."),
    OptionMetadataModel(name="audio_device", cli_flag="--audio-device", type="string", category="audio", description="Local inference device (cpu/cuda) for faster-whisper."),
    OptionMetadataModel(name="audio_compute_type", cli_flag="--audio-compute-type", type="string", category="audio", description="CTranslate2 compute type (int8/float16/...)."),
    OptionMetadataModel(name="audio_beam_size", cli_flag="--audio-beam-size", type="integer", category="audio", description="Beam size for decoding."),
    OptionMetadataModel(name="audio_vad_filter", cli_flag="--audio-vad-filter", type="boolean", category="audio", description="Apply Silero VAD silence filtering."),
    OptionMetadataModel(name="audio_diarization", cli_flag="--audio-diarization", type="boolean", default=False, category="audio", description="Separate speakers when the provider supports it."),
    OptionMetadataModel(name="audio_min_speakers", cli_flag="--audio-min-speakers", type="integer", category="audio", description="Minimum expected speaker count."),
    OptionMetadataModel(name="audio_max_speakers", cli_flag="--audio-max-speakers", type="integer", category="audio", description="Maximum expected speaker count."),
    OptionMetadataModel(name="audio_speaker_aliases", cli_flag="--audio-speaker-alias", type="object", category="audio", description="Map of speaker labels to confirmed names."),
    OptionMetadataModel(name="audio_vocabulary_pack_ids", cli_flag="--audio-vocabulary-pack-id", type="array", category="audio", description="Saved vocabulary pack ids to compile."),
    OptionMetadataModel(name="audio_confidence_heatmap", cli_flag="--audio-confidence-heatmap", type="boolean", default=True, category="audio", description="Emit per-segment confidence for the heatmap view."),
    OptionMetadataModel(name="audio_quality_diagnostics", cli_flag="--audio-quality-diagnostics", type="boolean", default=True, category="audio", description="Emit the audio quality diagnostics block."),
    OptionMetadataModel(name="audio_text_enhancement_enabled", cli_flag="--audio-text-enhancement", type="boolean", default=False, category="audio", description="Allow textual enhancement of the transcript."),
    OptionMetadataModel(name="audio_text_enhancement_strength", cli_flag="--audio-text-enhancement-strength", type="integer", default=0, category="audio", description="Textual enhancement strength 0-5."),
    OptionMetadataModel(name="audio_structural_enhancement_enabled", cli_flag="--audio-structural-enhancement", type="boolean", default=False, category="audio", description="Restructure the transcript into a document."),
    OptionMetadataModel(name="audio_structural_enhancement_mode", cli_flag="--audio-structural-enhancement-mode", type="enum", default="auto", category="audio", description="Document structure: auto, meeting_notes, lecture_notes, interview_qna, action_decision_log, timeline."),
    OptionMetadataModel(name="audio_fusion_mode", cli_flag="--audio-fusion-mode", type="enum", category="audio", description="Fuse transcript with context documents: audio_first, meeting_followup, lecture_study, research_memo, contradiction_audit, qna_extraction."),
    OptionMetadataModel(name="audio_contradiction_detection", cli_flag="--audio-contradiction-detection", type="boolean", default=False, category="audio", description="Detect contradictory claims across the transcript."),
    OptionMetadataModel(name="audio_allow_cloud_stt", cli_flag="--audio-allow-cloud-stt", type="boolean", default=False, category="audio", description="Explicit opt-in to send audio to a cloud STT provider."),
    OptionMetadataModel(name="audio_benchmark_compare", cli_flag="--audio-benchmark-compare", type="boolean", default=False, category="audio", description="Reserved for provider/model comparison; current builds reject it because no benchmark runner ships."),
    OptionMetadataModel(name="audio_config", type="object", category="audio", description="REST JSON blob of advanced audio controls; CLI callers can use --options-json or --option."),
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
