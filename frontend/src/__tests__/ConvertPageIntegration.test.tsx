import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { ConvertPage } from '@/pages/ConvertPage'
import { ConversionProvider } from '@/hooks/useConversionQueue'
import { BrowserRouter } from 'react-router-dom'
import '@testing-library/jest-dom'

// Mock the API module
const mockUploadFile = vi.fn()
const mockGetJobEvents = vi.fn()
const mockDownloadResult = vi.fn()
const mockCancelJob = vi.fn()
const mockDeleteJob = vi.fn()
const mockGetJobStatus = vi.fn()
const mockGetHistory = vi.fn()
const mockGetCapabilities = vi.fn()
const mockPlanConversion = vi.fn()
const mockGetPresets = vi.fn()
const mockSavePreset = vi.fn()
const mockDeletePreset = vi.fn()

vi.mock('@/lib/api', () => ({
  uploadFile: (...args: unknown[]) => mockUploadFile(...args),
  getJobEvents: (...args: unknown[]) => mockGetJobEvents(...args),
  downloadResult: (...args: unknown[]) => mockDownloadResult(...args),
  cancelJob: (...args: unknown[]) => mockCancelJob(...args),
  deleteJob: (...args: unknown[]) => mockDeleteJob(...args),
  getJobStatus: (...args: unknown[]) => mockGetJobStatus(...args),
  getHistory: (...args: unknown[]) => mockGetHistory(...args),
  browseFiles: vi.fn(),
  browseFolder: vi.fn(),
  getCapabilities: () => mockGetCapabilities(),
  planConversion: (...args: unknown[]) => mockPlanConversion(...args),
  getLLMProviders: vi.fn().mockResolvedValue([]),
  getActiveLLM: vi.fn().mockResolvedValue(null),
  getPresets: (...args: unknown[]) => mockGetPresets(...args),
  savePreset: (...args: unknown[]) => mockSavePreset(...args),
  deletePreset: (...args: unknown[]) => mockDeletePreset(...args),
}))

// Mock EventSource helper
interface MockEventSource {
  addEventListener: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
  onerror: (() => void) | null
}

function createMockEventSource(): MockEventSource {
  return {
    addEventListener: vi.fn(),
    close: vi.fn(),
    onerror: null,
  }
}

describe('ConvertPage Integration with real hook', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mockUploadFile.mockResolvedValue({
      job_id: 'job-uuid-123',
      status: 'pending',
      filename: 'test.pdf'
    })
    mockCancelJob.mockResolvedValue({ status: 'cancelled', job_id: 'job-uuid-123', cancelled: true })
    mockGetJobEvents.mockReturnValue(createMockEventSource())
    mockGetHistory.mockResolvedValue({ jobs: [], total: 0 })
    mockGetCapabilities.mockResolvedValue({
      engines: {
        marker_pdf: 'ready',
        office_docx: 'ready',
      },
      output_formats: ['markdown', 'json', 'html', 'chunks'],
      marker_multi_format_extensions: ['.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tiff', '.bmp', '.gif', '.epub'],
      input_formats: [],
    })
    mockPlanConversion.mockResolvedValue({
      engine: 'marker_pdf',
      label: 'Marker PDF',
      confidence: 1.0,
      reasons: [],
      needs_marker_models: true,
      needs_gpu: true,
      execution_backend: 'marker_worker',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: [],
      preliminary: true,
    })
    mockGetPresets.mockResolvedValue([])
    mockSavePreset.mockResolvedValue({ id: 'preset_123', name: 'Saved', config: {}, created_at: '' })
    mockDeletePreset.mockResolvedValue({ success: true, message: '' })
  })

  it('submits conversion and renders queue item without crashing', async () => {
    const { container } = render(
      <BrowserRouter>
        <ConversionProvider>
          <ConvertPage />
        </ConversionProvider>
      </BrowserRouter>
    )

    // Verify it renders initial empty state
    expect(screen.getByRole('heading', { name: 'Convert Document' })).toBeInTheDocument()
    expect(screen.getByText('Queue is empty')).toBeInTheDocument()

    // Click the Local Paths tab
    const localPathsTab = screen.getByRole('button', { name: /local paths/i })
    fireEvent.click(localPathsTab)

    // Select local path text area and add a path to enable the button
    const textarea = container.querySelector('textarea')!
    fireEvent.change(textarea, { target: { value: 'C:\\test_document.pdf' } })
    
    const convertBtn = await screen.findByRole('button', { name: /Convert 1 Document/i })
    await waitFor(() => expect(convertBtn).not.toBeDisabled())

    // Click convert button
    fireEvent.click(convertBtn)

    // Wait for the job card to appear in the queue list
    await waitFor(() => {
      expect(screen.getByText('test_document.pdf')).toBeInTheDocument()
    })

    // Check overall progress and console logs rendering
    expect(screen.getByText('Conversion Queue (1)')).toBeInTheDocument()
    expect(screen.getByText('Processing document...')).toBeInTheDocument()
  })

  it('recovers active backend jobs from history when queue state is empty', async () => {
    mockGetHistory.mockResolvedValue({
      total: 1,
      jobs: [
        {
          id: 'backend-job-1',
          job_id: 'backend-job-1',
          filename: 'openskills.pdf',
          status: 'pending',
          progress: 10,
          output_format: 'markdown',
          converter: 'PdfConverter',
          created_at: '2026-06-14T03:29:54Z',
          completed_at: null,
          error_message: null,
          result_text: null,
        },
      ],
    })

    render(
      <BrowserRouter>
        <ConversionProvider>
          <ConvertPage />
        </ConversionProvider>
      </BrowserRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('openskills.pdf')).toBeInTheDocument()
    })

    expect(screen.getByText('Conversion Queue (1)')).toBeInTheDocument()
    expect(screen.getByText('Queued on backend...')).toBeInTheDocument()
    expect(mockGetJobEvents).toHaveBeenCalledWith('backend-job-1')
  })

  it('removes a queue item locally without deleting backend metadata', async () => {
    // Remove is a local queue action. Backend cancellation is the explicit
    // Cancel button so history/output metadata is not silently destroyed.
    mockGetHistory.mockResolvedValue({
      total: 1,
      jobs: [
        {
          id: 'backend-job-9',
          job_id: 'backend-job-9',
          filename: 'running.pdf',
          status: 'processing',
          progress: 42,
          output_format: 'markdown',
          converter: 'PdfConverter',
          created_at: '2026-06-14T03:29:54Z',
          completed_at: null,
          error_message: null,
          result_text: null,
        },
      ],
    })
    render(
      <BrowserRouter>
        <ConversionProvider>
          <ConvertPage />
        </ConversionProvider>
      </BrowserRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('running.pdf')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /remove from list/i }))

    expect(screen.queryByText('running.pdf')).not.toBeInTheDocument()
    expect(mockCancelJob).not.toHaveBeenCalled()
    expect(mockDeleteJob).not.toHaveBeenCalled()
  })

  it('uses the non-destructive cancel endpoint when cancel is clicked', async () => {
    mockGetHistory.mockResolvedValue({
      total: 1,
      jobs: [
        {
          id: 'backend-job-10',
          job_id: 'backend-job-10',
          filename: 'cancel-only.pdf',
          status: 'processing',
          progress: 42,
          output_format: 'markdown',
          converter: 'PdfConverter',
          created_at: '2026-06-14T03:29:54Z',
          completed_at: null,
          error_message: null,
          result_text: null,
        },
      ],
    })

    render(
      <BrowserRouter>
        <ConversionProvider>
          <ConvertPage />
        </ConversionProvider>
      </BrowserRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('cancel-only.pdf')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))

    await waitFor(() => {
      expect(mockCancelJob).toHaveBeenCalledWith('backend-job-10')
    })
    expect(mockDeleteJob).not.toHaveBeenCalled()
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
  })

  it('renders backend cancelled SSE status without marking the job failed', async () => {
    const eventSource = createMockEventSource()
    mockGetJobEvents.mockReturnValue(eventSource)

    const { container } = render(
      <BrowserRouter>
        <ConversionProvider>
          <ConvertPage />
        </ConversionProvider>
      </BrowserRouter>
    )

    fireEvent.click(screen.getByRole('button', { name: /local paths/i }))
    fireEvent.change(container.querySelector('textarea')!, { target: { value: 'C:\\cancelled.pdf' } })
    fireEvent.click(await screen.findByRole('button', { name: /Convert 1 Document/i }))

    await waitFor(() => {
      expect(screen.getByText('cancelled.pdf')).toBeInTheDocument()
    })

    const statusHandler = eventSource.addEventListener.mock.calls.find(([event]) => event === 'status')?.[1]
    expect(statusHandler).toBeDefined()

    act(() => {
      statusHandler?.({ data: JSON.stringify({ status: 'cancelled' }) })
    })

    await waitFor(() => {
      expect(screen.getByText('Cancelled')).toBeInTheDocument()
    })
    expect(screen.queryByText('Conversion failed')).not.toBeInTheDocument()
  })

  it('clears fallback polling when a disconnected job is removed locally', async () => {
    const eventSource = createMockEventSource()
    mockGetJobEvents.mockReturnValue(eventSource)
    mockGetHistory.mockResolvedValue({
      total: 1,
      jobs: [
        {
          id: 'backend-job-11',
          job_id: 'backend-job-11',
          filename: 'disconnecting.pdf',
          status: 'processing',
          progress: 42,
          output_format: 'markdown',
          converter: 'PdfConverter',
          created_at: '2026-06-14T03:29:54Z',
          completed_at: null,
          error_message: null,
          result_text: null,
        },
      ],
    })

    render(
      <BrowserRouter>
        <ConversionProvider>
          <ConvertPage />
        </ConversionProvider>
      </BrowserRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('disconnecting.pdf')).toBeInTheDocument()
    })

    vi.useFakeTimers()
    try {
      act(() => {
        eventSource.onerror?.()
      })

      fireEvent.click(screen.getByRole('button', { name: /remove from list/i }))

      act(() => {
        vi.advanceTimersByTime(9000)
      })

      expect(screen.queryByText('disconnecting.pdf')).not.toBeInTheDocument()
      expect(mockGetJobStatus).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})
