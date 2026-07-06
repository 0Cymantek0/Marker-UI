import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getJobEvents,
  downloadResult,
  uploadFile,
  normalizeOcrEngine,
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

  it('throws on error', async () => {
    mockFetchOnce(500, 'Internal Server Error')

    await expect(downloadResult('job-dl-err')).rejects.toThrow('Download failed')
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
    })

    const call = vi.mocked(global.fetch).mock.calls[0]
    const url = new URL(String(call?.[0]), 'http://localhost')
    expect(url.searchParams.get('output_format')).toBe('chunks')
    expect(url.searchParams.get('chunking_strategy')).toBe('unstructured_by_title')
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
