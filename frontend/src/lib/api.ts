const API_BASE = '/api'

// ─── Types ───────────────────────────────────────────────────────────

export type OutputFormat = 'markdown' | 'json' | 'html' | 'chunks'
export type ConverterType =
  | 'PdfConverter'
  | 'TableConverter'
  | 'OCRConverter'
  | 'ExtractionConverter'
export type ImageHandlingMode = 'understanding' | 'extraction' | 'both'
export type OcrEngine = 'surya' | 'glm_ocr' | 'paddleocr_vl' | 'mistral_ocr'
export type SmartRouterLevel = 'disabled' | 'smart' | 'beeg_brain'
export type AudioOutputMode = 'transcript' | 'enhanced' | 'notes' | 'meeting_notes' | 'lecture_notes'

export interface ConversionConfig {
  output_format: OutputFormat
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
  decorative_max_text_density?: number
  ocr_min_text_density?: number
  ocr_min_lines?: number
  dedup_max_distance?: number
  vlm_crop_max_px?: number
  vlm_batch_size?: number
  max_batch_retries?: number
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
  image_understanding?: ImageUnderstandingMeta[] | null
  conversion_metadata?: Record<string, any> | null
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
  conversion_metadata?: Record<string, any> | null
  filename: string
  message?: string | null
  logs?: string | null
  elapsed?: number | null
  eta?: number | null
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
    throw new Error(`API ${res.status}: ${body}`)
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
  params.append('output_format', config.output_format)
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
  if (config.disable_image_extraction !== undefined) params.append('disable_image_extraction', String(config.disable_image_extraction))
  if (config.page_range) params.append('page_range', config.page_range)
  if (config.language) params.append('lang', config.language)
  if (config.audio_output_mode) params.append('audio_output_mode', config.audio_output_mode)
  if (config.audio_model) params.append('audio_model', config.audio_model)
  if (config.audio_vocabulary) params.append('audio_vocabulary', config.audio_vocabulary)
  if (config.audio_context) params.append('audio_context', config.audio_context)
  if (config.audio_low_confidence_threshold !== undefined) params.append('audio_low_confidence_threshold', String(config.audio_low_confidence_threshold))
  if (config.audio_word_timestamps !== undefined) params.append('audio_word_timestamps', String(config.audio_word_timestamps))
  if (config.disable_multiprocessing !== undefined) params.append('disable_multiprocessing', String(config.disable_multiprocessing))
  if (config.debug !== undefined) params.append('debug', String(config.debug))
  // --- Image-understanding pipeline knobs (1:1 query-param names) ---
  if (config.router_enabled !== undefined) params.append('router_enabled', String(config.router_enabled))
  if (config.smart_router_level) params.append('smart_router_level', config.smart_router_level)
  if (config.dedup_enabled !== undefined) params.append('dedup_enabled', String(config.dedup_enabled))
  if (config.downscale_vlm_crops !== undefined) params.append('downscale_vlm_crops', String(config.downscale_vlm_crops))
  if (config.batch_enabled !== undefined) params.append('batch_enabled', String(config.batch_enabled))
  if (config.ocr_engine) params.append('ocr_engine', config.ocr_engine)
  if (config.decorative_max_text_density !== undefined) params.append('decorative_max_text_density', String(config.decorative_max_text_density))
  if (config.ocr_min_text_density !== undefined) params.append('ocr_min_text_density', String(config.ocr_min_text_density))
  if (config.ocr_min_lines !== undefined) params.append('ocr_min_lines', String(config.ocr_min_lines))
  if (config.dedup_max_distance !== undefined) params.append('dedup_max_distance', String(config.dedup_max_distance))
  if (config.vlm_crop_max_px !== undefined) params.append('vlm_crop_max_px', String(config.vlm_crop_max_px))
  if (config.vlm_batch_size !== undefined) params.append('vlm_batch_size', String(config.vlm_batch_size))
  if (config.max_batch_retries !== undefined) params.append('max_batch_retries', String(config.max_batch_retries))
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

export async function downloadResult(jobId: string): Promise<{ blob: Blob; filename?: string }> {
  const res = await fetch(`${API_BASE}/convert/download/${jobId}`)
  if (!res.ok) throw new Error(`Download failed (${res.status})`)

  let filename: string | undefined
  const disposition = res.headers.get('content-disposition')
  if (disposition) {
    const filenameMatch = disposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/)
    if (filenameMatch && filenameMatch[1]) {
      filename = filenameMatch[1].replace(/['"]/g, '')
    }
  }

  const blob = await res.blob()
  return { blob, filename }
}

export async function getHistory(page = 1, limit = 20): Promise<{ jobs: JobStatus[]; total: number }> {
  // Backend returns HistoryResponse: { jobs: JobStatus[], total: number }
  const res = await request<{ jobs: BackendJobStatus[]; total: number }>(
    `/convert/history?page=${page}&page_size=${limit}`
  )
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

export async function deleteJob(jobId: string): Promise<void> {
  return request<void>(`/convert/${jobId}`, { method: 'DELETE' })
}

export async function browseFolder(): Promise<{ path: string }> {
  return request<{ path: string }>('/convert/browse-folder')
}

export async function browseFiles(): Promise<{ paths: string[] }> {
  return request<{ paths: string[] }>('/convert/browse-files')
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

export interface CapabilitiesResponse {
  engines: Record<string, string>
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
  preliminary: boolean
  probe_result?: Record<string, any> | null
  mixed_engine_segments?: Array<Record<string, any>> | null
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

