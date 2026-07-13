import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import '@testing-library/jest-dom'

// Mock the API client.
const mockGetLlmTraces = vi.fn()
vi.mock('@/lib/api', () => ({
  getLlmTraces: (...args: unknown[]) => mockGetLlmTraces(...args),
}))

import { LlmTraceViewer } from '@/components/features/conversion/LlmTraceViewer'

const SAMPLE_TRACES = [
  {
    index: 0,
    ts: 1700000000,
    job_id: 'job-1',
    host: 'generativelanguage.googleapis.com',
    model: 'gemini-flash',
    parts: [
      { type: 'image', data_url: 'data:image/webp;base64,UklGRkBAA==', mime: 'image/webp', size_bytes: 10 },
      { type: 'text', text: 'Rewrite this table:\n<table><tr><td>Name</td><td>Age</td></tr><tr><td>Ada</td><td>36</td></tr></table>' },
    ],
    image_count: 1,
    prompt_chars: 80,
    status: 200,
    response: '{"corrected_html": "<table><tr><td>Name</td><td>Age</td></tr><tr><td>Ada</td><td>36</td></tr></table>"}',
    response_chars: 90,
    cache_hit: false,
    elapsed_ms: 1200,
  },
  {
    index: 1,
    ts: 1700000005,
    job_id: 'job-1',
    host: 'generativelanguage.googleapis.com',
    model: 'gemini-flash',
    parts: [{ type: 'text', text: 'Rewrite this table:\n<table>...</table>' }],
    image_count: 0,
    prompt_chars: 40,
    status: 429,
    response: '{"error": "rate limited"}',
    response_chars: 25,
    cache_hit: false,
    elapsed_ms: 600,
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  mockGetLlmTraces.mockResolvedValue({ job_id: 'job-1', traces: SAMPLE_TRACES })
})

describe('LlmTraceViewer', () => {
  it('renders the inspector with the job filename and call count', async () => {
    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning={false}
        onClose={vi.fn()}
      />
    )
    await waitFor(() => {
      expect(screen.getByText(/LLM Call Inspector/i)).toBeInTheDocument()
    })
    expect(screen.getByText('report.pdf')).toBeInTheDocument()
    // Footer summary: 2 calls.
    expect(screen.getByText(/2 calls/i)).toBeInTheDocument()
  })

  it('shows an empty state when there are no traces', async () => {
    mockGetLlmTraces.mockResolvedValue({ job_id: 'job-x', traces: [] })
    render(
      <LlmTraceViewer
        open
        jobId="job-x"
        filename="empty.pdf"
        isRunning={false}
        onClose={vi.fn()}
      />
    )
    await waitFor(() => {
      expect(screen.getByText(/No LLM calls captured yet/i)).toBeInTheDocument()
    })
  })

  it('renders one collapsible card per trace with model + status badge', async () => {
    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning={false}
        onClose={vi.fn()}
      />
    )
    await waitFor(() => {
      expect(screen.getByText(/2 calls/i)).toBeInTheDocument()
    })
    // Both cards show the model name.
    expect(screen.getAllByText('gemini-flash').length).toBe(2)
    // HTTP 200 and HTTP 429 badges present.
    expect(screen.getByText('HTTP 200')).toBeInTheDocument()
    expect(screen.getByText('HTTP 429')).toBeInTheDocument()
  })

  it('expands a card on click to reveal the prompt image and response', async () => {
    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning={false}
        onClose={vi.fn()}
      />
    )
    await waitFor(() => expect(screen.getByText('HTTP 200')).toBeInTheDocument())

    // Click the first card header to expand.
    await act(async () => {
      fireEvent.click(screen.getByText('HTTP 200'))
    })

    // Expanded: "Sent to LLM" + "Received" sections appear.
    expect(screen.getByText(/Sent to LLM/i)).toBeInTheDocument()
    expect(screen.getByText(/Received/i)).toBeInTheDocument()
    // The image preview is rendered.
    expect(screen.getByAltText('LLM input')).toBeInTheDocument()
    // The response text is present.
    expect(screen.getByText(/corrected_html/i)).toBeInTheDocument()
  })

  it('toggles between Raw and Rendered view modes', async () => {
    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning={false}
        onClose={vi.fn()}
      />
    )
    await waitFor(() => expect(screen.getByText('HTTP 200')).toBeInTheDocument())
    await act(async () => {
      fireEvent.click(screen.getByText('HTTP 200'))
    })

    // Default is Raw. Switch to Rendered.
    await act(async () => {
      fireEvent.click(screen.getByText('Rendered'))
    })
    // In rendered mode the table HTML is rendered inside <table> elements
    // (one for the sent prompt, one for the received corrected_html).
    expect(screen.getAllByRole('table').length).toBeGreaterThan(0)
  })

  it('renders LLM table HTML as inert text-only cells', async () => {
    mockGetLlmTraces.mockResolvedValue({
      job_id: 'job-1',
      traces: [{
        ...SAMPLE_TRACES[0],
        response: JSON.stringify({
          corrected_html: '<table><tr><td onclick="alert(1)"><img src="https://evil.test/pixel.png" onerror="alert(2)">Ada<script>alert(3)</script><a href="javascript:alert(4)">link</a></td></tr></table>',
        }),
      }],
    })

    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning={false}
        onClose={vi.fn()}
      />
    )
    await waitFor(() => expect(screen.getByText('HTTP 200')).toBeInTheDocument())
    await act(async () => {
      fireEvent.click(screen.getByText('HTTP 200'))
      fireEvent.click(screen.getByText('Rendered'))
    })

    expect(screen.getAllByRole('cell').some((cell) => cell.textContent === 'Adalink')).toBe(true)
    expect(document.querySelector('td img')).toBeNull()
    expect(document.querySelector('td a')).toBeNull()
    expect(document.querySelector('[onclick]')).toBeNull()
    expect(document.querySelector('[onerror]')).toBeNull()
  })

  it('polls for traces while the job is running', async () => {
    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning
        onClose={vi.fn()}
        pollIntervalMs={50}
      />
    )
    // Initial fetch + at least one poll within 200ms.
    await waitFor(() => expect(mockGetLlmTraces).toHaveBeenCalledTimes(2), { timeout: 500 })
  })

  it('stops polling when the job is not running', async () => {
    render(
      <LlmTraceViewer
        open
        jobId="job-uuid-1"
        filename="report.pdf"
        isRunning={false}
        onClose={vi.fn()}
        pollIntervalMs={50}
      />
    )
    await waitFor(() => expect(mockGetLlmTraces).toHaveBeenCalledTimes(1))
    // Wait long enough that a poll WOULD have fired if it were scheduled.
    await new Promise((r) => setTimeout(r, 200))
    expect(mockGetLlmTraces).toHaveBeenCalledTimes(1)
  })
})
