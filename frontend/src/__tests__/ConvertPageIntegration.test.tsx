import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConvertPage } from '@/pages/ConvertPage'
import { ConversionProvider } from '@/hooks/useConversionQueue'
import { BrowserRouter } from 'react-router-dom'
import '@testing-library/jest-dom'

// Mock the API module
const mockUploadFile = vi.fn()
const mockGetJobEvents = vi.fn()
const mockDownloadResult = vi.fn()
const mockDeleteJob = vi.fn()
const mockGetJobStatus = vi.fn()
const mockGetHistory = vi.fn()

vi.mock('@/lib/api', () => ({
  uploadFile: (...args: any[]) => mockUploadFile(...args),
  getJobEvents: (...args: any[]) => mockGetJobEvents(...args),
  downloadResult: (...args: any[]) => mockDownloadResult(...args),
  deleteJob: (...args: any[]) => mockDeleteJob(...args),
  getJobStatus: (...args: any[]) => mockGetJobStatus(...args),
  getHistory: (...args: any[]) => mockGetHistory(...args),
  browseFiles: vi.fn(),
  browseFolder: vi.fn(),
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
    mockGetJobEvents.mockReturnValue(createMockEventSource())
    mockGetHistory.mockResolvedValue({ jobs: [], total: 0 })
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
    
    const convertBtn = screen.getByRole('button', { name: /convert/i })
    expect(convertBtn).toBeInTheDocument()
    expect(convertBtn).not.toBeDisabled()

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
})
