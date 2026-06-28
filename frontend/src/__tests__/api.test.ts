import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getJobEvents,
  downloadResult,
  uploadFile,
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
})
