const API_BASE = '/api'

// ─── Types ───────────────────────────────────────────────────────────

export type OutputFormat = 'markdown' | 'json' | 'html' | 'chunks'
export type ConverterType =
  | 'PdfConverter'
  | 'TableConverter'
  | 'OCRConverter'
  | 'ExtractionConverter'
export type ImageHandlingMode = 'understanding' | 'extraction' | 'both'
export type OcrEngine = 'surya' | 'hybrid_ocr'
export type HybridOcrProfile = 'balanced' | 'max_accuracy' | 'low_vram'
export type SmartRouterLevel = 'disabled' | 'smart' | 'beeg_brain'
export type ChunkingStrategy = 'markdown_heading_blocks_v2' | 'unstructured_by_title'
export type AudioOutputMode =
  | 'transcript'
  | 'enhanced'
  | 'notes'
  | 'meeting_notes'
  | 'lecture_notes'
  | 'interview_qna'
  | 'action_decision_log'

export type AudioProviderType =
  | 'local_faster_whisper'
  | 'local_whisperx'
  | 'openai'
  | 'groq'
  | 'deepgram'
  | 'assemblyai'
  | 'azure'
  | 'custom_openai_compatible'

export type AudioStructuralMode =
  | 'auto'
  | 'paragraphs'
  | 'speaker_sections'
  | 'meeting_notes'
  | 'lecture_notes'
  | 'interview_qna'
  | 'action_decision_log'
  | 'timeline'

export interface AudioProviderCapability {
  provider_id: AudioProviderType
  provider_label: string
  implementation_state?: 'implemented' | 'beta' | 'deferred' | 'unsupported'
  available?: boolean
  runtime_type: 'local' | 'cloud' | 'local_optional'
  cloud: boolean
  requires_api_key: boolean
  requires_model_license_acceptance: boolean
  privacy_level: 'local' | 'cloud' | 'hybrid'
  supports_word_timestamps: boolean
  supports_segment_timestamps: boolean
  supports_confidence: boolean
  supports_diarization: boolean
  supports_speaker_confidence: boolean
  supports_custom_vocabulary: boolean
  supports_prompt_context: boolean
  supports_translation: boolean
  supports_batch_compare: boolean
  max_file_size_hint_mb: number | null
  default_model: string | null
}

export interface VocabularyPack {
  id: string
  name: string
  terms: string[]
  category: string
  description?: string
  created_at: string
}

export interface AudioProviderConfig {
  id: string
  type: AudioProviderType | string
  label: string
  api_key?: string | null
  base_url?: string | null
  region?: string | null
  deployment?: string | null
  concurrency?: number | null
  timeout?: number | null
  max_retries?: number | null
  default_model?: string | null
  models: string[]
  enabled: boolean
  cloud: boolean
}

export interface ActiveAudioProvider {
  provider_id: string
  model_id: string
}

/**
 * Migrate any stored/legacy ocr_engine value to a currently-valid one.
 *
 * The specialist engines (glm_ocr, paddleocr_vl) are no longer user-facing —
 * they live behind hybrid_ocr — and mistral_ocr was removed from the local OCR
 * path entirely (cloud-based, conflicts with local-first). Presets saved under
 * the old four-value union are normalised here on load.
 */
export function normalizeOcrEngine(value: unknown): OcrEngine {
  if (value === 'surya' || value === 'hybrid_ocr') return value
  if (value === 'glm_ocr' || value === 'paddleocr_vl') return 'hybrid_ocr'
  return 'surya'
}

export interface ConversionConfig {
  output_formats: OutputFormat[]
  chunking_strategy?: ChunkingStrategy
  allow_chunking_fallback?: boolean
  converter: ConverterType
  engine_override?: string
  use_llm?: boolean
  llm_provider?: string
  llm_model?: string
  image_handling_mode?: ImageHandlingMode
  allow_cloud_vlm?: boolean
  force_ocr?: boolean
  paginate?: boolean
  disable_image_extraction?: boolean
  page_range?: string
  language?: string
  audio_output_mode?: AudioOutputMode
  audio_model?: string
  audio_vocabulary?: string
  audio_context?: string
  audio_low_confidence_threshold?: number
  audio_word_timestamps?: boolean
  // Advanced audio (plan §5.5)
  audio_provider?: AudioProviderType
  audio_language?: string
  audio_device?: string
  audio_compute_type?: string
  audio_beam_size?: number
  audio_vad_filter?: boolean
  // Speaker / diarization
  audio_diarization?: boolean
  audio_min_speakers?: number
  audio_max_speakers?: number
  audio_speaker_aliases?: Record<string, string>
  // Vocabulary packs
  audio_vocabulary_pack_ids?: string[]
  // Quality / confidence
  audio_confidence_heatmap?: boolean
  audio_quality_diagnostics?: boolean
  audio_review_required_on_low_confidence?: boolean
  // Enhancement layer
  audio_text_enhancement_enabled?: boolean
  audio_text_enhancement_strength?: number
  audio_structural_enhancement_enabled?: boolean
  audio_structural_enhancement_mode?: AudioStructuralMode
  audio_enhancement_allow_cloud?: boolean
  // Fusion / contradiction
  audio_fusion_mode?: string
  audio_contradiction_detection?: boolean
  // Privacy
  audio_allow_cloud_stt?: boolean
  // Benchmark
  audio_benchmark_compare?: boolean
  audio_compare_providers?: string[]
  disable_multiprocessing?: boolean
  debug?: boolean
  conversion_profile?: 'auto' | 'fast' | 'high_accuracy'
  // --- Image-understanding pipeline knobs (mirror ImageUnderstandingConfig) ---
  router_enabled?: boolean
  smart_router_level?: SmartRouterLevel
  dedup_enabled?: boolean
  downscale_vlm_crops?: boolean
  batch_enabled?: boolean
  ocr_engine?: OcrEngine
  hybrid_ocr_profile?: HybridOcrProfile
  hybrid_ocr_require_specialists?: boolean
  decorative_max_text_density?: number
  ocr_min_text_density?: number
  ocr_min_lines?: number
  dedup_max_distance?: number
  vlm_crop_max_px?: number
  vlm_batch_size?: number
  max_batch_retries?: number
  archive_recursive?: boolean
  archive_max_files?: number
  archive_inline_bytes?: number
  archive_max_converted_children?: number
  archive_max_child_bytes?: number
  archive_max_total_uncompressed_bytes?: number
  archive_max_compression_ratio?: number
  archive_max_depth?: number
}

export interface ConversionResponse {
  job_id: string
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled'
  filename: string
}

export interface ImageUnderstandingMeta {
  image_name: string
  image_type: string
  confidence: number
  model: string | null
  omitted: boolean
  cost_usd?: number
}

export interface MixedEngineSegment {
  page_range?: string | null
  requested_engine?: string | null
  actual_engine?: string | null
  fallback_reason?: string | null
  pages?: number[] | null
}

export interface ConversionMetadata {
  engine?: ConverterPlanResponse | null
  probe_result?: Record<string, unknown> | null
  audio?: Record<string, unknown> | null
  audio_batch?: Record<string, unknown> | null
  mixed_engine_segments?: MixedEngineSegment[] | null
  [key: string]: unknown
}

export interface AsOfContract {
  schema_version: string
  state_token: string
  completeness: 'complete' | 'incomplete' | 'failed' | 'cancelled'
  result_digest?: string | null
  source_revision_id?: string | null
  config_digest?: string | null
  artifacts_purged?: boolean
}

export interface JobStatus {
  id: string
  job_id: string
  filename: string
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled'
  progress: number
  output_format: string
  converter: string
  created_at: string
  completed_at: string | null
  error_message: string | null
  result_text: string | null
  formats?: Record<string, string> | null
  available_formats?: string[] | null
  image_understanding?: ImageUnderstandingMeta[] | null
  conversion_metadata?: ConversionMetadata | null
  message?: string | null
  logs?: string[] | null
  elapsed?: number | null
  eta?: number | null
  // Operational as-of envelope, server-derived. Carried verbatim from
  // /status and /history so the UI can detect stale-state rejections.
  as_of?: AsOfContract | null
}

export interface SSEEvent {
  event: string
  data: string
}

export interface SettingsResponse {
  key: string
  value: string
  category: string
  description: string | null
}

export type LLMService =
  | 'none'
  | 'gemini'
  | 'vertex'
  | 'ollama'
  | 'claude'
  | 'openai'
  | 'azure'

export interface LLMConfig {
  service: LLMService
  api_key: string
  base_url: string
  model_name: string
  timeout: number
  max_retries: number
  max_output_tokens: number
}

export interface ModelConfig {
  model_id: string
  context_window?: number
  max_retries?: number
  max_output_tokens?: number
  timeout?: number
  vision_capable?: boolean
}

export interface LLMProvider {
  id: string
  type: string
  label: string
  api_key?: string
  fallback_api_keys: string[]
  base_url?: string
  /** Max simultaneous in-flight API calls to this provider. undefined/0 = unlimited. */
  concurrency?: number | null
  models: ModelConfig[]
}

export interface ActiveLLM {
  provider_id: string
  model_id: string
}

export interface BackendLLMConfig {
  llm_service: string
  timeout?: number
  max_retries?: number
  max_output_tokens?: number
  gemini_api_key?: string | null
  gemini_model_name?: string | null
  openai_api_key?: string | null
  openai_base_url?: string | null
  openai_model?: string | null
  claude_api_key?: string | null
  claude_model_name?: string | null
  vertex_project_id?: string | null
  vertex_location?: string | null
  azure_api_key?: string | null
  azure_endpoint?: string | null
  azure_deployment_name?: string | null
  ollama_base_url?: string | null
  ollama_model?: string | null
  vlm_model?: string | null
}

export interface BackendJobStatus {
  job_id: string
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled'
  progress: number
  output_format: string
  converter: string
  created_at: string
  completed_at: string | null
  error_message: string | null
  result_text: string | null
  image_understanding?: ImageUnderstandingMeta[] | null
  conversion_metadata?: ConversionMetadata | null
  filename: string
  message?: string | null
  logs?: string[] | null
  elapsed?: number | null
  eta?: number | null
  as_of?: AsOfContract | null
}

// ─── Helpers ─────────────────────────────────────────────────────────

// Convert backend LLMConfig to frontend LLMConfig
function mapBackendToFrontendLLM(backend: BackendLLMConfig): LLMConfig {
  const service = backend.llm_service === 'no_llm' ? 'none' : backend.llm_service as LLMService;
  let api_key = '';
  let base_url = '';
  let model_name = '';

  if (service === 'gemini') {
    api_key = backend.gemini_api_key || '';
    model_name = backend.gemini_model_name || '';
  }
  if (service === 'openai') {
    api_key = backend.openai_api_key || '';
    base_url = backend.openai_base_url || '';
    model_name = backend.openai_model || '';
  }
  if (service === 'claude') {
    api_key = backend.claude_api_key || '';
    model_name = backend.claude_model_name || '';
  }
  if (service === 'vertex') {
    api_key = backend.vertex_project_id || '';
    model_name = backend.gemini_model_name || '';
  }
  if (service === 'azure') {
    api_key = backend.azure_api_key || '';
    base_url = backend.azure_endpoint || '';
    model_name = backend.azure_deployment_name || '';
  }
  if (service === 'ollama') {
    base_url = backend.ollama_base_url || '';
    model_name = backend.ollama_model || '';
  }

  return {
    service,
    api_key,
    base_url,
    model_name,
    timeout: backend.timeout ?? 60,
    max_retries: backend.max_retries ?? 3,
    max_output_tokens: backend.max_output_tokens ?? 4096,
  }
}

// Convert frontend LLMConfig to backend LLMConfig
function mapFrontendToBackendLLM(frontend: LLMConfig): BackendLLMConfig {
  const llm_service = frontend.service === 'none' ? 'no_llm' : frontend.service;
  const backend: BackendLLMConfig = {
    llm_service,
    timeout: frontend.timeout,
    max_retries: frontend.max_retries,
    max_output_tokens: frontend.max_output_tokens,
  };

  if (frontend.service === 'gemini') {
    backend.gemini_api_key = frontend.api_key || null;
    backend.gemini_model_name = frontend.model_name || null;
  }
  if (frontend.service === 'openai') {
    backend.openai_api_key = frontend.api_key || null;
    backend.openai_base_url = frontend.base_url || null;
    backend.openai_model = frontend.model_name || null;
  }
  if (frontend.service === 'claude') {
    backend.claude_api_key = frontend.api_key || null;
    backend.claude_model_name = frontend.model_name || null;
  }
  if (frontend.service === 'vertex') {
    backend.vertex_project_id = frontend.api_key || null;
    backend.gemini_model_name = frontend.model_name || null;
  }
  if (frontend.service === 'azure') {
    backend.azure_api_key = frontend.api_key || null;
    backend.azure_endpoint = frontend.base_url || null;
    backend.azure_deployment_name = frontend.model_name || null;
  }
  if (frontend.service === 'ollama') {
    backend.ollama_base_url = frontend.base_url || null;
    backend.ollama_model = frontend.model_name || null;
  }

  return backend;
}

export class ApiError extends Error {
  status: number
  code?: string
  currentAsOf?: AsOfContract | null
  constructor(
    message: string,
    opts: { status: number; code?: string; currentAsOf?: AsOfContract | null },
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = opts.status
    this.code = opts.code
    this.currentAsOf = opts.currentAsOf ?? null
  }
}

// FastAPI HTTPException bodies carry {detail: {code, ...}} (e.g. the 409
// stale_state rejections from the as-of contract). Extract the machine fields
// so callers can branch on them without parsing prose.
function parseErrorDetail(bodyText: string): { code?: string; currentAsOf: AsOfContract | null } {
  try {
    const parsed = JSON.parse(bodyText) as { detail?: { code?: string; current_as_of?: AsOfContract | null } }
    return { code: parsed?.detail?.code, currentAsOf: parsed?.detail?.current_as_of ?? null }
  } catch {
    // Non-JSON body: plain message-only error.
    return { currentAsOf: null }
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  })

  if (!res.ok) {
    const body = await res.text().catch(() => res.statusText)
    const { code, currentAsOf } = parseErrorDetail(body)
    throw new ApiError(`API ${res.status}: ${body}`, { status: res.status, code, currentAsOf })
  }

  if (res.status === 204) return undefined as T

  return res.json() as Promise<T>
}

// ─── API Functions ───────────────────────────────────────────────────

export async function uploadFile(
  file: File | null,
  config: ConversionConfig,
  localFilepath?: string,
  outputDir?: string,
  sourceUrl?: string
): Promise<ConversionResponse> {
  const form = new FormData()
  if (file) {
    form.append('file', file)
  }

  const params = new URLSearchParams()
  const primaryFormat = config.output_formats[0] ?? 'markdown'
  params.append('output_format', primaryFormat)
  if (config.output_formats.length > 1) {
    params.append('output_formats', config.output_formats.join(','))
  }
  if (config.chunking_strategy) params.append('chunking_strategy', config.chunking_strategy)
  if (config.allow_chunking_fallback !== undefined) params.append('allow_chunking_fallback', String(config.allow_chunking_fallback))
  if (config.conversion_profile) params.append('conversion_profile', config.conversion_profile)
  if (config.converter) params.append('converter', config.converter)
  if (config.engine_override) params.append('engine_override', config.engine_override)
  if (config.use_llm !== undefined) params.append('use_llm', String(config.use_llm))
  if (config.llm_provider) params.append('llm_provider', config.llm_provider)
  if (config.llm_model) params.append('llm_model', config.llm_model)
  if (config.image_handling_mode) params.append('image_handling_mode', config.image_handling_mode)
  if (config.allow_cloud_vlm !== undefined) params.append('allow_cloud_vlm', String(config.allow_cloud_vlm))
  if (config.force_ocr !== undefined) params.append('force_ocr', String(config.force_ocr))
  if (config.paginate !== undefined) params.append('paginate_output', String(config.paginate))
  const disableImageExtraction = config.image_handling_mode === 'understanding'
    ? true
    : config.disable_image_extraction
  if (disableImageExtraction !== undefined) params.append('disable_image_extraction', String(disableImageExtraction))
  if (config.page_range) params.append('page_range', config.page_range)
  if (config.language) params.append('lang', config.language)
  if (config.audio_output_mode) params.append('audio_output_mode', config.audio_output_mode)
  if (config.audio_model) params.append('audio_model', config.audio_model)
  if (config.audio_vocabulary) params.append('audio_vocabulary', config.audio_vocabulary)
  if (config.audio_context) params.append('audio_context', config.audio_context)
  if (config.audio_low_confidence_threshold !== undefined) params.append('audio_low_confidence_threshold', String(config.audio_low_confidence_threshold))
  if (config.audio_word_timestamps !== undefined) params.append('audio_word_timestamps', String(config.audio_word_timestamps))
  // Advanced audio controls (sent as audio_config JSON blob, plan §5.5)
  const audioAdvanced: Record<string, unknown> = {}
  if (config.audio_provider) audioAdvanced.audio_provider = config.audio_provider
  if (config.audio_language) audioAdvanced.audio_language = config.audio_language
  if (config.audio_device) audioAdvanced.audio_device = config.audio_device
  if (config.audio_compute_type) audioAdvanced.audio_compute_type = config.audio_compute_type
  if (config.audio_beam_size !== undefined) audioAdvanced.audio_beam_size = config.audio_beam_size
  if (config.audio_vad_filter !== undefined) audioAdvanced.audio_vad_filter = config.audio_vad_filter
  if (config.audio_diarization !== undefined) audioAdvanced.audio_diarization = config.audio_diarization
  if (config.audio_min_speakers !== undefined) audioAdvanced.audio_min_speakers = config.audio_min_speakers
  if (config.audio_max_speakers !== undefined) audioAdvanced.audio_max_speakers = config.audio_max_speakers
  if (config.audio_speaker_aliases && Object.keys(config.audio_speaker_aliases).length > 0) audioAdvanced.audio_speaker_aliases = config.audio_speaker_aliases
  if (config.audio_vocabulary_pack_ids && config.audio_vocabulary_pack_ids.length > 0) audioAdvanced.audio_vocabulary_pack_ids = config.audio_vocabulary_pack_ids
  if (config.audio_confidence_heatmap !== undefined) audioAdvanced.audio_confidence_heatmap = config.audio_confidence_heatmap
  if (config.audio_quality_diagnostics !== undefined) audioAdvanced.audio_quality_diagnostics = config.audio_quality_diagnostics
  if (config.audio_review_required_on_low_confidence !== undefined) audioAdvanced.audio_review_required_on_low_confidence = config.audio_review_required_on_low_confidence
  if (config.audio_text_enhancement_enabled !== undefined) audioAdvanced.audio_text_enhancement_enabled = config.audio_text_enhancement_enabled
  if (config.audio_text_enhancement_strength !== undefined) audioAdvanced.audio_text_enhancement_strength = config.audio_text_enhancement_strength
  if (config.audio_structural_enhancement_enabled !== undefined) audioAdvanced.audio_structural_enhancement_enabled = config.audio_structural_enhancement_enabled
  if (config.audio_structural_enhancement_mode) audioAdvanced.audio_structural_enhancement_mode = config.audio_structural_enhancement_mode
  if (config.audio_enhancement_allow_cloud !== undefined) audioAdvanced.audio_enhancement_allow_cloud = config.audio_enhancement_allow_cloud
  if (config.audio_fusion_mode) audioAdvanced.audio_fusion_mode = config.audio_fusion_mode
  if (config.audio_contradiction_detection !== undefined) audioAdvanced.audio_contradiction_detection = config.audio_contradiction_detection
  if (config.audio_allow_cloud_stt !== undefined) audioAdvanced.audio_allow_cloud_stt = config.audio_allow_cloud_stt
  if (config.audio_benchmark_compare !== undefined) audioAdvanced.audio_benchmark_compare = config.audio_benchmark_compare
  if (config.audio_compare_providers && config.audio_compare_providers.length > 0) audioAdvanced.audio_compare_providers = config.audio_compare_providers
  if (Object.keys(audioAdvanced).length > 0) params.append('audio_config', JSON.stringify(audioAdvanced))
  if (config.disable_multiprocessing !== undefined) params.append('disable_multiprocessing', String(config.disable_multiprocessing))
  if (config.debug !== undefined) params.append('debug', String(config.debug))
  // --- Image-understanding pipeline knobs (1:1 query-param names) ---
  if (config.router_enabled !== undefined) params.append('router_enabled', String(config.router_enabled))
  if (config.smart_router_level) params.append('smart_router_level', config.smart_router_level)
  if (config.dedup_enabled !== undefined) params.append('dedup_enabled', String(config.dedup_enabled))
  if (config.downscale_vlm_crops !== undefined) params.append('downscale_vlm_crops', String(config.downscale_vlm_crops))
  if (config.batch_enabled !== undefined) params.append('batch_enabled', String(config.batch_enabled))
  if (config.ocr_engine !== undefined) params.append('ocr_engine', config.ocr_engine)
  if (config.hybrid_ocr_profile !== undefined) params.append('hybrid_ocr_profile', config.hybrid_ocr_profile)
  if (config.hybrid_ocr_require_specialists !== undefined) params.append('hybrid_ocr_require_specialists', String(config.hybrid_ocr_require_specialists))
  if (config.decorative_max_text_density !== undefined) params.append('decorative_max_text_density', String(config.decorative_max_text_density))
  if (config.ocr_min_text_density !== undefined) params.append('ocr_min_text_density', String(config.ocr_min_text_density))
  if (config.ocr_min_lines !== undefined) params.append('ocr_min_lines', String(config.ocr_min_lines))
  if (config.dedup_max_distance !== undefined) params.append('dedup_max_distance', String(config.dedup_max_distance))
  if (config.vlm_crop_max_px !== undefined) params.append('vlm_crop_max_px', String(config.vlm_crop_max_px))
  if (config.vlm_batch_size !== undefined) params.append('vlm_batch_size', String(config.vlm_batch_size))
  if (config.max_batch_retries !== undefined) params.append('max_batch_retries', String(config.max_batch_retries))
  if (config.archive_recursive !== undefined) params.append('archive_recursive', String(config.archive_recursive))
  if (config.archive_max_files !== undefined) params.append('archive_max_files', String(config.archive_max_files))
  if (config.archive_inline_bytes !== undefined) params.append('archive_inline_bytes', String(config.archive_inline_bytes))
  if (config.archive_max_converted_children !== undefined) params.append('archive_max_converted_children', String(config.archive_max_converted_children))
  if (config.archive_max_child_bytes !== undefined) params.append('archive_max_child_bytes', String(config.archive_max_child_bytes))
  if (config.archive_max_total_uncompressed_bytes !== undefined) params.append('archive_max_total_uncompressed_bytes', String(config.archive_max_total_uncompressed_bytes))
  if (config.archive_max_compression_ratio !== undefined) params.append('archive_max_compression_ratio', String(config.archive_max_compression_ratio))
  if (config.archive_max_depth !== undefined) params.append('archive_max_depth', String(config.archive_max_depth))
  if (localFilepath) params.append('local_filepath', localFilepath)
  if (sourceUrl) params.append('source_url', sourceUrl)
  if (outputDir) params.append('output_dir', outputDir)

  const res = await fetch(`${API_BASE}/convert/upload?${params.toString()}`, {
    method: 'POST',
    body: file ? form : undefined,
  })

  if (!res.ok) {
    const body = await res.text().catch(() => res.statusText)
    throw new Error(`Upload failed (${res.status}): ${body}`)
  }

  return res.json() as Promise<ConversionResponse>
}

export async function getJobStatus(jobId: string): Promise<JobStatus> {
  const res = await request<BackendJobStatus>(`/convert/status/${jobId}`)
  return {
    ...res,
    id: res.job_id,
  }
}

export function getJobEvents(jobId: string): EventSource {
  return new EventSource(`${API_BASE}/convert/events/${jobId}`)
}

/** Export currency reported by the server via the X-Marker-As-Of-Mode header:
 *  `verified` = the presented state token matched the current derivation;
 *  `historical` = no currency claim was made. */
export type AsOfMode = 'verified' | 'historical'

export async function downloadResult(
  jobId: string,
  format?: string,
  asOfToken?: string
): Promise<{ blob: Blob; filename?: string; asOfMode?: AsOfMode }> {
  let url = format
    ? `${API_BASE}/convert/download/${jobId}?format=${format}`
    : `${API_BASE}/convert/download/${jobId}`
  if (asOfToken) url += `&as_of=${encodeURIComponent(asOfToken)}`
  const res = await fetch(url)
  if (!res.ok) {
    const bodyText = await res.text().catch(() => '')
    const { code, currentAsOf } = parseErrorDetail(bodyText)
    if (code === 'stale_state') {
      throw new ApiError(`Download failed (${res.status}): stale_state`, {
        status: res.status,
        code: 'stale_state',
        currentAsOf,
      })
    }
    throw new Error(`Download failed (${res.status})`)
  }

  let filename: string | undefined
  const disposition = res.headers.get('content-disposition')
  if (disposition) {
    const filenameMatch = disposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/)
    if (filenameMatch && filenameMatch[1]) {
      filename = filenameMatch[1].replace(/['"]/g, '')
    }
  }

  const modeHeader = res.headers.get('X-Marker-As-Of-Mode')
  const asOfMode: AsOfMode | undefined =
    modeHeader === 'verified' || modeHeader === 'historical' ? modeHeader : undefined

  const blob = await res.blob()
  return { blob, filename, asOfMode }
}

export interface RegenerateResult {
  status: string
  job_id: string
  format: string
  available_formats: string[]
}

export async function regenerateFormat(jobId: string, format: string, asOfToken?: string): Promise<RegenerateResult> {
  let url = `/convert/${jobId}/regenerate?format=${format}`
  if (asOfToken) url += `&as_of=${encodeURIComponent(asOfToken)}`
  return request<RegenerateResult>(url, {
    method: 'POST',
  })
}

export async function getHistory(
  page = 1,
  limit = 20,
  search?: string,
  status?: string,
  converter?: string
): Promise<{ jobs: JobStatus[]; total: number }> {
  let url = `/convert/history?page=${page}&page_size=${limit}`
  if (search) url += `&search=${encodeURIComponent(search)}`
  if (status && status !== 'all') url += `&status=${encodeURIComponent(status)}`
  if (converter && converter !== 'all') url += `&converter=${encodeURIComponent(converter)}`

  const res = await request<{ jobs: BackendJobStatus[]; total: number }>(url)
  return {
    jobs: res.jobs.map((j) => ({
      ...j,
      id: j.job_id,
    })),
    total: res.total,
  }
}

export async function getSettings(): Promise<SettingsResponse[]> {
  // Backend returns dict[str, list[SettingsResponse]]
  const res = await request<Record<string, SettingsResponse[]>>('/settings')
  return Object.values(res).flat()
}

export async function updateSetting(
  key: string,
  value: string,
  category: string
): Promise<void> {
  return request<void>('/settings', {
    method: 'PUT',
    body: JSON.stringify({ key, value, category }),
  })
}

export async function getLLMConfig(): Promise<LLMConfig> {
  const res = await request<BackendLLMConfig>('/settings/llm/config')
  return mapBackendToFrontendLLM(res)
}

export async function updateLLMConfig(config: LLMConfig): Promise<void> {
  const backendConfig = mapFrontendToBackendLLM(config)
  return request<void>('/settings/llm/config', {
    method: 'PUT',
    body: JSON.stringify(backendConfig),
  })
}

export async function testLLMConnection(config: LLMConfig): Promise<{ success: boolean; message: string }> {
  const backendConfig = mapFrontendToBackendLLM(config)
  return request<{ success: boolean; message: string }>('/settings/llm/test', {
    method: 'POST',
    body: JSON.stringify(backendConfig),
  })
}

export async function getLLMProviders(): Promise<LLMProvider[]> {
  return request<LLMProvider[]>('/settings/llm/providers')
}

export async function saveLLMProviders(providers: LLMProvider[]): Promise<LLMProvider[]> {
  return request<LLMProvider[]>('/settings/llm/providers', {
    method: 'PUT',
    body: JSON.stringify(providers),
  })
}

export async function getActiveLLM(): Promise<ActiveLLM> {
  return request<ActiveLLM>('/settings/llm/active')
}

export async function setActiveLLM(active: ActiveLLM): Promise<ActiveLLM> {
  return request<ActiveLLM>('/settings/llm/active', {
    method: 'PUT',
    body: JSON.stringify(active),
  })
}

export async function fetchAvailableModels(
  providerId: string | undefined,
  type: string,
  baseUrl?: string,
  apiKey?: string
): Promise<string[]> {
  return request<string[]>('/settings/llm/providers/fetch-models', {
    method: 'POST',
    body: JSON.stringify({ provider_id: providerId, type, base_url: baseUrl, api_key: apiKey }),
  })
}

export interface LiveOverrideRequest {
  provider_id: string
  old_model?: string
  new_model?: string
  concurrency?: number | null
  persist?: boolean
}

/** Hot-swap a running job's model and/or concurrency for one provider.
 *  Same-provider model swaps apply to in-flight calls without losing work. */
export async function applyLiveOverride(body: LiveOverrideRequest): Promise<{ status: string }> {
  return request<{ status: string }>('/settings/llm/live-override', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export interface RetryJobRequestBody {
  llm_provider?: string
  llm_model?: string
}

export interface RetryJobResponse {
  new_job_id: string
  source_job_id: string
  status: string
}

/** Re-run a terminal job from its stored source file, optionally with a
 *  different LLM provider/model. Creates a new job; the original stays in
 *  history. LLM responses cached for the same prompt are replayed, so only
 *  the work that did not complete is re-done. */
export async function retryConversionJob(
  jobId: string,
  body: RetryJobRequestBody = {}
): Promise<RetryJobResponse> {
  return request<RetryJobResponse>(`/convert/${jobId}/retry`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function deleteJob(jobId: string): Promise<void> {
  return request<void>(`/convert/${jobId}`, { method: 'DELETE' })
}

// --- LLM call trace viewer -------------------------------------------------

export interface LlmTracePart {
  type: 'text' | 'image'
  text?: string
  data_url?: string
  mime?: string
  size_bytes?: number
  truncated?: boolean
  note?: string
}

export interface LlmTrace {
  index: number
  ts: number
  job_id: string
  host: string
  model: string
  parts: LlmTracePart[]
  image_count: number
  prompt_chars: number
  status: number
  response: string
  response_chars: number
  cache_hit: boolean
  elapsed_ms: number
}

export async function getLlmTraces(jobId: string): Promise<{ job_id: string; traces: LlmTrace[] }> {
  return request<{ job_id: string; traces: LlmTrace[] }>(`/convert/${jobId}/llm-traces`)
}

export async function cancelJob(jobId: string): Promise<{ status: string; job_id: string; cancelled: boolean }> {
  return request<{ status: string; job_id: string; cancelled: boolean }>(`/convert/${jobId}/cancel`, { method: 'POST' })
}

export async function healthCheck(): Promise<{ status: string }> {
  return request<{ status: string }>('/health')
}

// ─── Model Download Onboarding ───────────────────────────────────────

export interface FileDownloadInfo {
  status: 'downloading' | 'completed' | 'failed'
  downloaded_bytes: number
  total_bytes: number
}

export interface ModelDownloadInfo {
  name: string
  status: 'pending' | 'downloading' | 'completed' | 'failed'
  downloaded_bytes: number
  total_bytes: number
  progress: number
  files: Record<string, FileDownloadInfo>
}

export interface ModelTrackerStatus {
  initialized: boolean
  loading: boolean
  cancel_requested: boolean
  error: string | null
  models: Record<string, ModelDownloadInfo>
  overall: {
    status: 'pending' | 'downloading' | 'loading' | 'completed' | 'failed'
    progress: number
    downloaded_bytes: number
    total_bytes: number
    speed: number // MB/s
    eta: number // seconds
  }
}

export async function getModelsStatus(): Promise<ModelTrackerStatus> {
  return request<ModelTrackerStatus>('/models/status')
}

export async function cancelModelsDownload(): Promise<{ status: string }> {
  return request<{ status: string }>('/models/cancel', { method: 'POST' })
}

export async function retryModelsDownload(): Promise<{ status: string }> {
  return request<{ status: string }>('/models/retry', { method: 'POST' })
}

export interface HybridOcrEngineStatus {
  model_id: string
  model_dir: string
  model_present: boolean
}

export interface HybridOcrStatus {
  schema_version: string
  model_root: string
  engines: Record<'glm_ocr' | 'paddleocr_vl', HybridOcrEngineStatus>
  engines_available: string[]
  warnings: string[]
}

export async function getHybridOcrStatus(): Promise<HybridOcrStatus> {
  return request<HybridOcrStatus>('/models/hybrid-ocr/status')
}

export async function setupHybridOcrModels(engine = 'all', force = false): Promise<{ status: HybridOcrStatus }> {
  const params = new URLSearchParams({ engine, force: String(force) })
  return request<{ status: HybridOcrStatus }>(`/models/hybrid-ocr/setup?${params.toString()}`, {
    method: 'POST',
  })
}

// ─── GPU Acceleration ──────────────────────────────────────────────────

export interface GPUStatus {
  status: 'not_installed' | 'installing' | 'ready' | 'failed'
  progress: number
  logs: string[]
  error_message: string | null
  cuda_available: boolean
}

export async function getGPUStatus(): Promise<GPUStatus> {
  return request<GPUStatus>('/settings/gpu/status')
}

export async function installGPU(): Promise<GPUStatus> {
  return request<GPUStatus>('/settings/gpu/install', { method: 'POST' })
}

export async function toggleGPU(enabled: boolean): Promise<{ status: string; enabled: boolean }> {
  return request<{ status: string; enabled: boolean }>('/settings/gpu/toggle', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })
}

// ─── Multi-GPU Worker Scaling ─────────────────────────────────────────

export type GPUWorkerMode = 'auto' | 'manual'

export interface GPUWorkersResolved {
  mode: GPUWorkerMode
  detected: number
  effective: number
  active: string
  restart_required: boolean
}

export async function getGPUWorkersResolved(): Promise<GPUWorkersResolved> {
  return request<GPUWorkersResolved>('/settings/gpu-workers/resolved')
}

export async function setGPUWorkers(
  mode: GPUWorkerMode,
  manualCount?: number,
): Promise<GPUWorkersResolved> {
  return request<GPUWorkersResolved>('/settings/gpu-workers', {
    method: 'PUT',
    body: JSON.stringify({ mode, manual_count: manualCount }),
  })
}

export async function selfHealModels(): Promise<{ success: boolean; healed_count: number; issues: string[]; message: string }> {
  return request<{ success: boolean; healed_count: number; issues: string[]; message: string }>('/models/self-heal', { method: 'POST' })
}

export async function resetModels(deleteUserData: boolean): Promise<{ success: boolean; deleted_models: string[]; user_data_reset: boolean; message: string }> {
  return request<{ success: boolean; deleted_models: string[]; user_data_reset: boolean; message: string }>(`/models/reset?delete_user_data=${deleteUserData}`, { method: 'POST' })
}

// ─── Capabilities & Conversion Planning ───────────────────────────────

export interface InputFormatCapability {
  extensions: string[]
  engine: string
  label: string
  category: string
  needs_marker_models: boolean
  needs_gpu: boolean
  upload_allowed: boolean
  url_allowed: boolean
  output_formats?: OutputFormat[]
}

export interface CapabilitiesResponse {
  engines: Record<string, string>
  output_formats?: OutputFormat[]
  marker_multi_format_extensions?: string[]
  input_formats?: InputFormatCapability[]
}

export interface ConverterPlanResponse {
  engine: string
  label: string
  confidence: number
  reasons: string[]
  needs_marker_models: boolean
  needs_gpu: boolean
  execution_backend: string
  needs_cloud: boolean
  optional_dependencies: string[]
  fallback_chain: string[]
  warnings: string[]
  output_formats?: OutputFormat[]
  preliminary: boolean
  probe_result?: Record<string, unknown> | null
  mixed_engine_segments?: MixedEngineSegment[] | null
}

export async function getCapabilities(): Promise<CapabilitiesResponse> {
  return request<CapabilitiesResponse>('/capabilities')
}

export async function planConversion(
  filename: string,
  size: number,
  local_filepath?: string,
  engine_override?: string,
  conversion_profile?: 'auto' | 'fast' | 'high_accuracy',
  image_handling_mode?: string,
  converter?: string,
  force_ocr?: boolean
): Promise<ConverterPlanResponse> {
  return request<ConverterPlanResponse>('/convert/plan', {
    method: 'POST',
    body: JSON.stringify({
      filename,
      size,
      local_filepath,
      engine_override,
      conversion_profile,
      image_handling_mode,
      converter_cls: converter,
      force_ocr,
    }),
  })
}


// ─── Conversion Presets ───────────────────────────────────────────────

export interface ConversionPreset {
  id: string
  name: string
  description?: string
  config: Partial<ConversionConfig>
  created_at: string
}

export async function getPresets(): Promise<ConversionPreset[]> {
  return request<ConversionPreset[]>('/settings/presets')
}

export async function savePreset(
  name: string,
  config: Partial<ConversionConfig>,
  description?: string
): Promise<ConversionPreset> {
  return request<ConversionPreset>('/settings/presets', {
    method: 'POST',
    body: JSON.stringify({ name, description, config }),
  })
}

export async function deletePreset(presetId: string): Promise<{ success: boolean; message: string }> {
  return request<{ success: boolean; message: string }>(`/settings/presets/${presetId}`, {
    method: 'DELETE',
  })
}


// ─── Audio Provider & Vocabulary Pack APIs ──────────────────────────

export async function getAudioCapabilities(): Promise<AudioProviderCapability[]> {
  const res = await request<{ providers: AudioProviderCapability[] }>('/settings/audio/capabilities')
  return res.providers
}

export async function getAudioProviders(): Promise<AudioProviderConfig[]> {
  return request<AudioProviderConfig[]>('/settings/audio/providers')
}

export async function saveAudioProviders(providers: AudioProviderConfig[]): Promise<AudioProviderConfig[]> {
  return request<AudioProviderConfig[]>('/settings/audio/providers', {
    method: 'PUT',
    body: JSON.stringify(providers),
  })
}

export async function getActiveAudioProvider(): Promise<ActiveAudioProvider> {
  return request<ActiveAudioProvider>('/settings/audio/active')
}

export async function setActiveAudioProvider(active: ActiveAudioProvider): Promise<ActiveAudioProvider> {
  return request<ActiveAudioProvider>('/settings/audio/active', {
    method: 'PUT',
    body: JSON.stringify(active),
  })
}

export async function getVocabularyPacks(): Promise<VocabularyPack[]> {
  return request<VocabularyPack[]>('/settings/audio/vocabulary')
}

export async function saveVocabularyPack(
  pack: { name: string; terms: string[]; category?: string; description?: string }
): Promise<VocabularyPack> {
  return request<VocabularyPack>('/settings/audio/vocabulary', {
    method: 'POST',
    body: JSON.stringify(pack),
  })
}

export async function deleteVocabularyPack(packId: string): Promise<void> {
  return request<void>(`/settings/audio/vocabulary/${packId}`, {
    method: 'DELETE',
  })
}
