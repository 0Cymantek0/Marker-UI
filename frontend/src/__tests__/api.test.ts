import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  getJobEvents,
  downloadResult,
} from '@/lib/api'

const eventSourceUrls: string[] = []

beforeEach(() => {
  vi.restoreAllMocks()
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
