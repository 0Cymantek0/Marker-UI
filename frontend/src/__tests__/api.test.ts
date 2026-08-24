import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getJobEvents,
  downloadResult,
  uploadFile,
  normalizeOcrEngine,
  getAudioProviders,
  saveAudioProviders,
  getActiveAudioProvider,
  setActiveAudioProvider,
  ApiError,
} from '@/lib/api'

const eventSourceUrls: string[] = []

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  eventSourceUrls.length = 0

  vi.spyOn(global, 'fetch')

  vi.stubGlobal(
    'EventSource',
    class MockEventSource {
      addEventListener = vi.fn()
      close = vi.fn()
      onerror: (() => void) | null = null
      constructor(public readonly url: string) {
        eventSourceUrls.push(url)
      }
    } as unknown as typeof EventSource
  )
})
function mockFetchOnce(status: number, body: unknown, ok?: boolean) {
  return vi.mocked(global.fetch).mockResolvedValueOnce({
    status,
    ok: ok ?? (status >= 200 && status < 300),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
    blob: () => Promise.resolve(new Blob()),
    headers: new Headers(),
  } as Response)
}

describe('getJobEvents EventSource URL', () => {
  it('creates EventSource with correct URL', () => {
    getJobEvents('job-events-1')

    expect(eventSourceUrls[0]).toBe('/api/convert/events/job-events-1')
  })
})

describe('downloadResult', () => {
  it('returns blob on success', async () => {
    mockFetchOnce(200, new Blob(), true)

    const result = await downloadResult('job-dl-1')

    expect(result.blob).toBeInstanceOf(Blob)
  })

  it('appends the as_of query param when a token is supplied', async () => {
    mockFetchOnce(200, new Blob(), true)

    await downloadResult('job-dl-token', 'markdown', 'sha256:abc/def')

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('as_of')).toBe('sha256:abc/def')
    expect(url.pathname).toBe('/api/convert/download/job-dl-token')
    expect(url.searchParams.get('format')).toBe('markdown')
  })

  it('omits the as_of query param when no token is supplied', async () => {
    mockFetchOnce(200, new Blob(), true)

    await downloadResult('job-dl-notoken')

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.has('as_of')).toBe(false)
  })

  it('throws on error', async () => {
    mockFetchOnce(500, 'Internal Server Error')

    await expect(downloadResult('job-dl-err')).rejects.toThrow('Download failed')
  })

  it('rejects with ApiError on a 409 stale_state body', async () => {
    const staleBody = {
      detail: {
        code: 'stale_state',
        message: 'state token mismatch',
        observed_state_token: 'sha256:old',
        current_state_token: 'sha256:cur',
        current_as_of: {
          schema_version: 'marker.operational.as_of.v1',
          state_token: 'sha256:cur',
          completeness: 'complete',
        },
      },
    }
    mockFetchOnce(409, staleBody)

    let caught: unknown
    try {
      await downloadResult('job-dl-stale', 'markdown', 'sha256:old')
    } catch (err) {
      caught = err
    }

    expect(caught).toBeInstanceOf(ApiError)
    const apiErr = caught as ApiError
    expect(apiErr.code).toBe('stale_state')
    expect(apiErr.status).toBe(409)
    expect(apiErr.message).toContain('stale_state')
    expect(apiErr.currentAsOf?.state_token).toBe('sha256:cur')
  })

  it('throws a plain Error on a 500 (backward compat)', async () => {
    mockFetchOnce(500, 'boom')

    await expect(downloadResult('job-dl-500')).rejects.toThrow(/Download failed \(500\)/)
  })
})

describe('uploadFile', () => {
  it('sends audio enhancement controls as upload query params', async () => {
    mockFetchOnce(200, { job_id: 'job-1', status: 'pending', filename: 'voice.wav' }, true)

    await uploadFile(new File(['wav'], 'voice.wav', { type: 'audio/wav' }), {
      output_formats: ['markdown'],
      converter: 'PdfConverter',
      audio_output_mode: 'meeting_notes',
      audio_model: 'base.en',
      audio_vocabulary: 'Marker, LiteParse',
      audio_context: 'project call',
      audio_low_confidence_threshold: 0.7,
      audio_word_timestamps: true,
    })

    const call = vi.mocked(global.fetch).mock.calls[0]
    expect(call).toBeDefined()
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('audio_output_mode')).toBe('meeting_notes')
    expect(url.searchParams.get('audio_model')).toBe('base.en')
    expect(url.searchParams.get('audio_vocabulary')).toBe('Marker, LiteParse')
    expect(url.searchParams.get('audio_context')).toBe('project call')
    expect(url.searchParams.get('audio_low_confidence_threshold')).toBe('0.7')
    expect(url.searchParams.get('audio_word_timestamps')).toBe('true')
  })

  it('sends hybrid OCR controls even when image understanding is off', async () => {
    mockFetchOnce(200, { job_id: 'job-ocr', status: 'pending', filename: 'scan.pdf' }, true)

    await uploadFile(new File(['pdf'], 'scan.pdf', { type: 'application/pdf' }), {
      output_formats: ['markdown'],
      converter: 'PdfConverter',
      image_handling_mode: 'extraction',
      ocr_engine: 'hybrid_ocr',
      hybrid_ocr_profile: 'low_vram',
      hybrid_ocr_require_specialists: true,
    })

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('image_handling_mode')).toBe('extraction')
    expect(url.searchParams.get('ocr_engine')).toBe('hybrid_ocr')
    expect(url.searchParams.get('hybrid_ocr_profile')).toBe('low_vram')
    expect(url.searchParams.get('hybrid_ocr_require_specialists')).toBe('true')
  })

  it('forces image extraction off for understanding-only mode', async () => {
    mockFetchOnce(200, { job_id: 'job-description', status: 'pending', filename: 'scan.png' }, true)

    await uploadFile(new File(['png'], 'scan.png', { type: 'image/png' }), {
      output_formats: ['markdown'],
      converter: 'OCRConverter',
      image_handling_mode: 'understanding',
      disable_image_extraction: false,
    })

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('image_handling_mode')).toBe('understanding')
    expect(url.searchParams.get('disable_image_extraction')).toBe('true')
  })

  it('sends chunking strategy when chunks output is requested', async () => {
    mockFetchOnce(200, { job_id: 'job-chunks', status: 'pending', filename: 'notes.md' }, true)

    await uploadFile(new File(['# Notes'], 'notes.md', { type: 'text/markdown' }), {
      output_formats: ['chunks'],
      converter: 'PdfConverter',
      chunking_strategy: 'unstructured_by_title',
      allow_chunking_fallback: true,
    })

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('output_format')).toBe('chunks')
    expect(url.searchParams.get('chunking_strategy')).toBe('unstructured_by_title')
    expect(url.searchParams.get('allow_chunking_fallback')).toBe('true')
  })

  it('sends archive budget controls as upload query params', async () => {
    mockFetchOnce(200, { job_id: 'job-zip', status: 'pending', filename: 'bundle.zip' }, true)

    await uploadFile(new File(['zip'], 'bundle.zip', { type: 'application/zip' }), {
      output_formats: ['markdown'],
      converter: 'PdfConverter',
      archive_recursive: false,
      archive_max_files: 12,
      archive_inline_bytes: 4096,
      archive_max_converted_children: 3,
      archive_max_child_bytes: 8192,
      archive_max_total_uncompressed_bytes: 16384,
      archive_max_compression_ratio: 25,
      archive_max_depth: 1,
    })

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('archive_recursive')).toBe('false')
    expect(url.searchParams.get('archive_max_files')).toBe('12')
    expect(url.searchParams.get('archive_inline_bytes')).toBe('4096')
    expect(url.searchParams.get('archive_max_converted_children')).toBe('3')
    expect(url.searchParams.get('archive_max_child_bytes')).toBe('8192')
    expect(url.searchParams.get('archive_max_total_uncompressed_bytes')).toBe('16384')
    expect(url.searchParams.get('archive_max_compression_ratio')).toBe('25')
    expect(url.searchParams.get('archive_max_depth')).toBe('1')
  })
})

describe('normalizeOcrEngine', () => {
  it('maps legacy specialist values without preserving cloud Mistral', () => {
    expect(normalizeOcrEngine('surya')).toBe('surya')
    expect(normalizeOcrEngine('hybrid_ocr')).toBe('hybrid_ocr')
    expect(normalizeOcrEngine('glm_ocr')).toBe('hybrid_ocr')
    expect(normalizeOcrEngine('paddleocr_vl')).toBe('hybrid_ocr')
    expect(normalizeOcrEngine('mistral_ocr')).toBe('surya')
    expect(normalizeOcrEngine(undefined)).toBe('surya')
  })
})

describe('audio provider settings API', () => {
  it('lists configured audio providers', async () => {
    mockFetchOnce(200, [
      {
        id: 'openai',
        type: 'openai',
        label: 'OpenAI',
        api_key: '********',
        models: ['gpt-4o-transcribe'],
        enabled: true,
        cloud: true,
      },
    ], true)

    const providers = await getAudioProviders()

    expect(providers[0]?.id).toBe('openai')
    expect(global.fetch).toHaveBeenCalledWith('/api/settings/audio/providers', expect.any(Object))
  })

  it('saves configured audio providers', async () => {
    const body = [
      {
        id: 'deepgram',
        type: 'deepgram',
        label: 'Deepgram',
        api_key: 'secret',
        base_url: '',
        region: '',
        deployment: '',
        concurrency: 2,
        timeout: 30,
        max_retries: 1,
        default_model: 'nova-2',
        models: ['nova-2'],
        enabled: true,
        cloud: true,
      },
    ]
    mockFetchOnce(200, body, true)

    await saveAudioProviders(body)

    expect(global.fetch).toHaveBeenCalledWith('/api/settings/audio/providers', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify(body),
    }))
  })

  it('gets and sets active audio provider', async () => {
    mockFetchOnce(200, { provider_id: 'local_faster_whisper', model_id: '' }, true)
    await expect(getActiveAudioProvider()).resolves.toEqual({ provider_id: 'local_faster_whisper', model_id: '' })

    mockFetchOnce(200, { provider_id: 'openai', model_id: 'gpt-4o-transcribe' }, true)
    await setActiveAudioProvider({ provider_id: 'openai', model_id: 'gpt-4o-transcribe' })

    expect(global.fetch).toHaveBeenLastCalledWith('/api/settings/audio/active', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ provider_id: 'openai', model_id: 'gpt-4o-transcribe' }),
    }))
  })
})
