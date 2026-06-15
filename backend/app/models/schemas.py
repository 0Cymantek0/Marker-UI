"""Pydantic v2 request/response schemas for Marker UI API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class LLMService(str, Enum):
    gemini = "gemini"
    openai = "openai"
    claude = "claude"
    vertex = "vertex"
    azure = "azure"
    ollama = "ollama"
    no_llm = "no_llm"


class OutputFormat(str, Enum):
    markdown = "markdown"
    json = "json"
    html = "html"
    chunks = "chunks"


class JobStatusEnum(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# ---------------------------------------------------------------------------
# LLM Configuration
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    """Full LLM provider configuration.

    Field names follow marker-pdf's actual config keys exactly:
    - Gemini: gemini_api_key, gemini_model_name
    - Vertex: vertex_project_id, vertex_location, gemini_model_name
    - OpenAI: openai_api_key, openai_base_url, openai_model
    - Azure: azure_endpoint, azure_api_key, azure_api_version, azure_deployment_name
    - Claude: claude_api_key, claude_model_name
    - Ollama: ollama_base_url, ollama_model
    """

    # --- Service selection ---
    llm_service: LLMService = LLMService.no_llm

    # --- Gemini ---
    gemini_api_key: Optional[str] = None
    gemini_model_name: Optional[str] = None  # default: gemini-2.5-flash

    # --- Google Vertex ---
    vertex_project_id: Optional[str] = None
    vertex_location: Optional[str] = None  # default: us-central1

    # --- OpenAI (and OpenAI-compatible endpoints) ---
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None  # default: https://api.openai.com/v1
    openai_model: Optional[str] = None  # default: gpt-4.1-mini

    # --- Azure OpenAI ---
    azure_endpoint: Optional[str] = None
    azure_api_key: Optional[str] = None
    azure_api_version: Optional[str] = None  # default: 2023-05-15
    azure_deployment_name: Optional[str] = None

    # --- Claude ---
    claude_api_key: Optional[str] = None
    claude_model_name: Optional[str] = None  # default: claude-sonnet-4-20250514

    # --- Ollama ---
    ollama_base_url: Optional[str] = None  # default: http://localhost:11434
    ollama_model: Optional[str] = None  # default: qwen3-vl:8b

    # --- Shared LLM params ---
    timeout: int = Field(default=60, ge=1)
    max_retries: int = Field(default=3, ge=0)
    retry_wait_time: int = Field(default=3, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

class ConversionRequest(BaseModel):
    """Parameters for a single document conversion."""

    output_format: OutputFormat = OutputFormat.markdown
    converter_cls: Optional[str] = None  # e.g. "PdfConverter", "TableConverter"
    use_llm: bool = False
    force_ocr: bool = False
    paginate_output: bool = False
    disable_image_extraction: bool = False
    page_range: Optional[str] = None  # e.g. "1-5,8,10-12"
    lang: Optional[str] = None
    processors: Optional[str] = None  # comma-separated processor names
    disable_multiprocessing: bool = False
    strip_existing_ocr: bool = False
    redo_inline_math: bool = False
    block_correction_prompt: Optional[str] = None
    debug: bool = False


class ConversionResponse(BaseModel):
    """Returned immediately after a conversion job is submitted."""

    job_id: str
    status: str
    filename: str
    output_format: str


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------

class JobStatusResponse(BaseModel):
    """Polled or pushed job status."""

    job_id: str
    status: str
    progress: int = 0
    error_message: Optional[str] = None
    result_text: Optional[str] = None
    # Conversion metadata (e.g. per-image understanding info for the badge UI).
    image_understanding: Optional[list[dict]] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    filename: Optional[str] = None
    output_format: Optional[str] = None
    converter: Optional[str] = None
    message: Optional[str] = None
    logs: Optional[list[str]] = None
    elapsed: Optional[int] = None
    eta: Optional[int] = None


class HistoryResponse(BaseModel):
    """Paginated list of jobs with total count."""

    jobs: list[JobStatusResponse]
    total: int


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsResponse(BaseModel):
    """A single persisted setting."""

    key: str
    value: str
    category: str


class SettingsUpdateRequest(BaseModel):
    """Upsert a single setting."""

    key: str
    value: str
    category: str = "general"


class SettingsBatchUpdateRequest(BaseModel):
    """Upsert multiple settings at once."""

    settings: list[SettingsUpdateRequest]


# ---------------------------------------------------------------------------
# GPU Acceleration
# ---------------------------------------------------------------------------

class GPUStatusResponse(BaseModel):
    """Response representing the GPU acceleration installation status."""

    status: str
    progress: int
    logs: list[str]
    error_message: Optional[str] = None
    cuda_available: bool


class GPUToggleRequest(BaseModel):
    """Request to enable or disable GPU acceleration preference."""

    enabled: bool


# ---------------------------------------------------------------------------
# Dynamic LLM Providers
# ---------------------------------------------------------------------------

class ModelConfig(BaseModel):
    model_id: str
    context_window: Optional[int] = None
    max_retries: Optional[int] = None
    max_output_tokens: Optional[int] = None
    timeout: Optional[int] = None
    vision_capable: bool = False


class LLMProvider(BaseModel):
    id: str
    type: str  # gemini, claude, ollama, azure, vertex, openai, custom_openai
    label: str
    api_key: Optional[str] = None
    fallback_api_keys: list[str] = Field(default_factory=list)
    base_url: Optional[str] = None
    models: list[ModelConfig] = Field(default_factory=list)


class ActiveLLM(BaseModel):
    provider_id: str
    model_id: str


class FetchModelsRequest(BaseModel):
    provider_id: Optional[str] = None
    type: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None


