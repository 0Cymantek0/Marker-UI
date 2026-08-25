import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import '@testing-library/jest-dom'
import { IntegrityPage } from '@/pages/IntegrityPage'
import { ApiError, type AsOfContract, type JobStatus } from '@/lib/api'
import * as api from '@/lib/api'
import { toast } from 'sonner'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    getJobStatus: vi.fn(),
    getHistory: vi.fn(),
    downloadResult: vi.fn(),
  }
})

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const SERVER_TOKEN = 'tok-currentserver456'
const BOOKMARK_TOKEN = 'tok-oldbookmark123'

const envelope: AsOfContract = {
  schema_version: 'marker.operational.as_of.v1',
  state_token: SERVER_TOKEN,
  completeness: 'complete',
  result_digest: 'sha256:result-digest-abcdef',
  source_revision_id: 'rev-src-123456',
  config_digest: 'sha256:config-digest-abcdef',
  artifacts_purged: false,
}

function makeJob(overrides: Partial<JobStatus> = {}): JobStatus {
  return {
    id: 'job-1',
    job_id: 'job-1',
    filename: 'report.pdf',
    status: 'completed',
    progress: 100,
    output_format: 'markdown',
    converter: 'PdfConverter',
    created_at: '2026-06-11T09:00:00Z',
    completed_at: '2026-06-11T09:01:00Z',
    error_message: null,
    result_text: 'sample output',
    available_formats: ['markdown', 'json'],
    as_of: envelope,
    ...overrides,
  }
}

function renderPage(route = '/integrity') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <IntegrityPage />
    </MemoryRouter>
  )
}

function mockDownloadAnchor() {
  const anchor = document.createElement('a')
  const click = vi.fn()
  anchor.click = click
  const createElement = vi.spyOn(document, 'createElement').mockImplementation((tagName, options) => {
    if (tagName.toLowerCase() === 'a') return anchor
    return Document.prototype.createElement.call(document, tagName, options)
  })
  const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:integrity-download')
  const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  return {
    anchor,
    click,
    restore() {
      createElement.mockRestore()
      createObjectURL.mockRestore()
      revokeObjectURL.mockRestore()
    },
  }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.getHistory).mockResolvedValue({ jobs: [], total: 0 })
  vi.mocked(api.getJobStatus).mockResolvedValue(makeJob())
})

describe('IntegrityPage', () => {
  it('renders the current revision context from the server status envelope', async () => {
    renderPage('/integrity?job=job-1')

    expect(await screen.findByText('report.pdf')).toBeInTheDocument()
    // Envelope fields visible with human labels; full values exposed via title.
    expect(screen.getByText('State token (acting)')).toBeInTheDocument()
    expect(screen.getByTitle(SERVER_TOKEN)).toBeInTheDocument()
    expect(screen.getByText('Result digest')).toBeInTheDocument()
    expect(screen.getByTitle('sha256:result-digest-abcdef')).toBeInTheDocument()
    expect(screen.getByText('Source revision')).toBeInTheDocument()
    expect(screen.getByTitle('rev-src-123456')).toBeInTheDocument()
    expect(screen.getByText('Config digest')).toBeInTheDocument()
    expect(screen.getByTitle('sha256:config-digest-abcdef')).toBeInTheDocument()
    expect(screen.getByText('Artifacts purged')).toBeInTheDocument()
    expect(screen.getByText('No')).toBeInTheDocument()
    expect(screen.getByText('Schema version')).toBeInTheDocument()
    expect(screen.getByText('marker.operational.as_of.v1')).toBeInTheDocument()
    // Live region announces the current state.
    expect(screen.getByRole('status')).toHaveTextContent('Ready for verified export')
    // Download is enabled while current.
    expect(screen.getByRole('button', { name: /Download \(verified\)/i })).toBeEnabled()
  })

  it('deep link ?job= fetches that job from the server', async () => {
    vi.mocked(api.getJobStatus).mockResolvedValue(makeJob({ id: 'job-deep-9', job_id: 'job-deep-9' }))

    renderPage('/integrity?job=job-deep-9')

    expect(await screen.findByText('report.pdf')).toBeInTheDocument()
    expect(api.getJobStatus).toHaveBeenCalledWith('job-deep-9')
  })

  it('shows recent completed jobs in the picker and loads one on selection', async () => {
    vi.mocked(api.getHistory).mockResolvedValue({ jobs: [makeJob({ id: 'job-pick-1', job_id: 'job-pick-1', filename: 'picked.pdf' })], total: 1 })

    renderPage('/integrity')

    expect(api.getHistory).toHaveBeenCalledWith(1, 10, undefined, 'completed')
    const pick = await screen.findByRole('button', { name: /picked\.pdf/ })
    fireEvent.click(pick)

    expect(await screen.findByText('Revision you are acting on')).toBeInTheDocument()
    expect(api.getJobStatus).toHaveBeenCalledWith('job-pick-1')
  })

  it('flags a bookmarked as_of token as stale on load and blocks the download', async () => {
    renderPage(`/integrity?job=job-1&as_of=${BOOKMARK_TOKEN}`)

    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('revision moved on the server')
    // Observed vs current token ids are both visible (full values via title).
    expect(within(banner).getByTitle(BOOKMARK_TOKEN)).toBeInTheDocument()
    expect(within(banner).getByTitle(SERVER_TOKEN)).toBeInTheDocument()
    // Download disabled with a visible reason; no success surface anywhere.
    const download = screen.getByRole('button', { name: /Download \(verified\)/i })
    expect(download).toBeDisabled()
    expect(screen.getByText(/pinned state token is stale/i)).toBeInTheDocument()
    expect(screen.queryByText('Verified export downloaded')).not.toBeInTheDocument()
    expect(toast.success).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('Download disabled')
  })

  it('reconciles a 409 stale rejection visibly without any false success', async () => {
    vi.mocked(api.downloadResult).mockRejectedValueOnce(
      new ApiError('Download failed (409): stale_state', {
        status: 409,
        code: 'stale_state',
        currentAsOf: { ...envelope, state_token: 'tok-refreshed789' },
      })
    )

    renderPage('/integrity?job=job-1')
    await screen.findByText('report.pdf')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Download \(verified\)/i }))
    })

    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('revision moved on the server')
    // Observed (pinned) and current (from the 409 envelope) tokens displayed.
    expect(within(banner).getByTitle(SERVER_TOKEN)).toBeInTheDocument()
    expect(within(banner).getByTitle('tok-refreshed789')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Download \(verified\)/i })).toBeDisabled()
    expect(screen.queryByText('Verified export downloaded')).not.toBeInTheDocument()
    expect(toast.success).not.toHaveBeenCalled()
    expect(api.downloadResult).toHaveBeenCalledTimes(1)
  })

  it('treats a stale rejection without the server envelope as a conservative failure', async () => {
    vi.mocked(api.downloadResult).mockRejectedValueOnce(
      new ApiError('Download failed (409): stale_state', {
        status: 409,
        code: 'stale_state',
      })
    )

    renderPage('/integrity?job=job-1')
    await screen.findByText('report.pdf')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Download \(verified\)/i }))
    })

    // Ambiguous rejection: conservative error with retry, never a dead stale
    // UI and never a success surface.
    expect(await screen.findByRole('alert')).toHaveTextContent('did not include its current state')
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    expect(screen.queryByText('Verified export downloaded')).not.toBeInTheDocument()
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('returns to the job picker via Change job', async () => {
    renderPage('/integrity?job=job-1')
    await screen.findByText('report.pdf')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Change job/i }))
    })

    expect(await screen.findByText('Load by Job ID')).toBeInTheDocument()
    expect(screen.queryByText('State token (acting)')).not.toBeInTheDocument()

    // Re-picking the same job reloads authoritative state.
    fireEvent.change(screen.getByLabelText('Job ID'), { target: { value: 'job-1' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Load job state/i }))
    })
    expect(api.getJobStatus).toHaveBeenCalledTimes(2)
  })

  it('recovers: refresh adopts the new server token and the retry download verifies', async () => {
    const anchorSpy = mockDownloadAnchor()
    vi.mocked(api.downloadResult)
      .mockRejectedValueOnce(
        new ApiError('Download failed (409): stale_state', {
          status: 409,
          code: 'stale_state',
          currentAsOf: { ...envelope, state_token: 'tok-refreshed789' },
        })
      )
      .mockResolvedValueOnce({
        blob: new Blob(['zip'], { type: 'application/zip' }),
        filename: 'report.md',
        asOfMode: 'verified',
      })

    renderPage('/integrity?job=job-1')
    await screen.findByText('report.pdf')

    // First attempt rejected as stale.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Download \(verified\)/i }))
    })
    await screen.findByRole('alert')

    // Recovery: refresh re-fetches server truth and adopts the new token.
    vi.mocked(api.getJobStatus).mockResolvedValue(
      makeJob({ as_of: { ...envelope, state_token: 'tok-refreshed789' } })
    )
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Refresh current state/i }))
    })

    await screen.findByText('State current. Ready for verified export.')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Download \(verified\)/i })).toBeEnabled()

    // Retry uses the adopted token and succeeds against the server's verdict.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Download \(verified\)/i }))
    })

    expect(api.downloadResult).toHaveBeenLastCalledWith('job-1', 'markdown', 'tok-refreshed789')
    expect(await screen.findByText('Verified export downloaded')).toBeInTheDocument()
    expect(screen.getByText('report.zip')).toBeInTheDocument()
    expect(anchorSpy.anchor.download).toBe('report.zip')
    expect(anchorSpy.click).toHaveBeenCalledTimes(1)
    expect(toast.success).toHaveBeenCalledWith('Verified export downloaded')
    anchorSpy.restore()
  })

  it('treats a server response without a verified mode as a failure (no false success)', async () => {
    vi.mocked(api.downloadResult).mockResolvedValueOnce({
      blob: new Blob(['zip'], { type: 'application/zip' }),
      filename: 'report.md',
      asOfMode: undefined,
    })

    renderPage('/integrity?job=job-1')
    await screen.findByText('report.pdf')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Download \(verified\)/i }))
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('did not confirm this export as verified')
    expect(screen.queryByText('Verified export downloaded')).not.toBeInTheDocument()
    expect(toast.success).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith('Export could not be verified by the server')
  })

  it('renders a verified success state after a confirmed export', async () => {
    const anchorSpy = mockDownloadAnchor()
    vi.mocked(api.downloadResult).mockResolvedValueOnce({
      blob: new Blob(['zip'], { type: 'application/zip' }),
      filename: 'report.md',
      asOfMode: 'verified',
    })

    renderPage('/integrity?job=job-1')
    await screen.findByText('report.pdf')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Download \(verified\)/i }))
    })

    expect(api.downloadResult).toHaveBeenCalledWith('job-1', 'markdown', SERVER_TOKEN)
    expect(await screen.findByText('Verified export downloaded')).toBeInTheDocument()
    expect(screen.getByText('report.zip')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Verified export downloaded')
    expect(toast.success).toHaveBeenCalledTimes(1)
    anchorSpy.restore()
  })

  it('renders a conservative error state on network failure with retry', async () => {
    vi.mocked(api.getJobStatus).mockRejectedValue(new TypeError('fetch failed'))

    renderPage('/integrity?job=job-1')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Job state could not be loaded from the server'
    )
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    // No optimistic current/final rendering: no context card, no export panel.
    expect(screen.queryByText('State token (acting)')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Download \(verified\)/i })).not.toBeInTheDocument()
    expect(screen.queryByText('Verified export downloaded')).not.toBeInTheDocument()
    expect(toast.success).not.toHaveBeenCalled()

    // Retry recovers when the server comes back.
    vi.mocked(api.getJobStatus).mockResolvedValue(makeJob())
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    })
    expect(await screen.findByText('State current. Ready for verified export.')).toBeInTheDocument()
  })
})
