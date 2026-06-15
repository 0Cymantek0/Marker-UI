import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ConvertPage } from '@/pages/ConvertPage'
import '@testing-library/jest-dom'

// Mock useConversionQueue hook
const mockUseConversionQueue = vi.fn()
vi.mock('@/hooks/useConversionQueue', () => ({
  useConversionQueue: () => mockUseConversionQueue()
}))

// Mock components that we don't want to render in full depth or that have external deps
vi.mock('@/components/features/FileUpload', () => ({
  FileUpload: () => <div data-testid="file-upload">FileUpload</div>
}))

vi.mock('@/components/features/ConversionOptions', () => ({
  ConversionOptions: ({ config, onChange }: { config: any; onChange: (cfg: any) => void }) => (
    <div data-testid="conversion-options">
      <span data-testid="config-format">{config.output_format}</span>
      <span data-testid="config-ocr">{config.force_ocr ? 'ocr-enabled' : 'ocr-disabled'}</span>
      <button data-testid="trigger-config-change" onClick={() => onChange({ ...config, force_ocr: true })}>
        Trigger Change
      </button>
    </div>
  )
}))

vi.mock('@/components/features/TerminalLog', () => ({
  TerminalLog: ({ logs, onClose }: { logs: string[]; onClose?: () => void }) => (
    <div data-testid="terminal-log">
      TerminalLog Logs Count: {logs.length}
      {onClose && (
        <button onClick={onClose}>Close console</button>
      )}
    </div>
  )
}))

describe('ConvertPage component', () => {
  it('renders initial state with empty queue and console closed by default', () => {
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
    })

    render(<ConvertPage />)

    // Check headers and uploads
    expect(screen.getByRole('heading', { name: 'Convert Document' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Convert Document' })).toBeInTheDocument()
    expect(screen.getByTestId('file-upload')).toBeInTheDocument()
    expect(screen.getByTestId('conversion-options')).toBeInTheDocument()

    // Console logs is closed by default
    expect(screen.queryByTestId('terminal-log')).not.toBeInTheDocument()
    expect(screen.getByText('Open Console')).toBeInTheDocument()
  })

  it('renders queue items and overall progress without crash', () => {
    mockUseConversionQueue.mockReturnValue({
      jobs: [
        {
          id: 'job-1',
          filename: 'test.pdf',
          file: null,
          localPath: 'C:\\test.pdf',
          phase: 'completed',
          progress: 100,
          statusText: 'Conversion complete',
          jobId: 'job-uuid-1',
          error: null,
          resultBlob: new Blob(),
          logs: ['log line 1'],
          outputFormat: 'markdown'
        }
      ],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
    })

    render(<ConvertPage />)

    // Queue list should show 1 job
    expect(screen.getByText('Conversion Queue (1)')).toBeInTheDocument()
    // Filename appears in the queue card and (for completed jobs) the Output Preview header.
    expect(screen.getAllByText('test.pdf').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Overall:')).toBeInTheDocument()
    expect(screen.getByText('1 of 1 completed')).toBeInTheDocument()
    // 100% appears in the queue progress; OutputViewer may add another.
    expect(screen.getAllByText('100%').length).toBeGreaterThanOrEqual(1)

    // Download button is present (queue card + possibly OutputViewer) for completed job
    expect(screen.getAllByRole('button', { name: /download/i }).length).toBeGreaterThanOrEqual(1)
  })

  it('renders failed job and displays failure status without download button', () => {
    mockUseConversionQueue.mockReturnValue({
      jobs: [
        {
          id: 'job-failed',
          filename: 'failed_doc.pdf',
          file: null,
          localPath: '',
          phase: 'failed',
          progress: 50,
          statusText: 'Conversion failed',
          jobId: 'job-uuid-failed',
          error: 'Error: Extraction failed',
          logs: ['Failed step'],
          outputFormat: 'markdown'
        }
      ],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
    })

    render(<ConvertPage />)

    expect(screen.getByText('failed_doc.pdf')).toBeInTheDocument()
    expect(screen.getByText('Conversion failed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /download/i })).not.toBeInTheDocument()
  })

  it('toggles console visibility when clicking the console buttons', () => {
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
    })

    render(<ConvertPage />)

    // Initially console is closed
    expect(screen.queryByTestId('terminal-log')).not.toBeInTheDocument()
    expect(screen.getByText('Open Console')).toBeInTheDocument()

    // Click Open Console button
    fireEvent.click(screen.getByText('Open Console'))

    // Now console is visible
    expect(screen.getByTestId('terminal-log')).toBeInTheDocument()
    expect(screen.getByText('TerminalLog Logs Count: 0')).toBeInTheDocument()

    // Click close button
    fireEvent.click(screen.getByText('Close console'))

    // Console is closed again
    expect(screen.queryByTestId('terminal-log')).not.toBeInTheDocument()
  })

  describe('configuration persistence', () => {
    beforeEach(() => {
      localStorage.clear()
    })

    it('loads config from localStorage if present', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
      })

      const customConfig = {
        output_format: 'json',
        converter: 'TableConverter',
        use_llm: true,
        force_ocr: true,
      }
      localStorage.setItem('marker-conversion-config', JSON.stringify(customConfig))

      render(<ConvertPage />)

      expect(screen.getByTestId('config-format')).toHaveTextContent('json')
      expect(screen.getByTestId('config-ocr')).toHaveTextContent('ocr-enabled')
    })

    it('saves config to localStorage when changes are applied', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
      })

      render(<ConvertPage />)

      // Verify initial load saves the default config
      expect(localStorage.getItem('marker-conversion-config')).not.toBeNull()

      // Trigger a change
      fireEvent.click(screen.getByTestId('trigger-config-change'))

      const saved = localStorage.getItem('marker-conversion-config')
      expect(saved).not.toBeNull()
      const parsed = JSON.parse(saved!)
      expect(parsed.force_ocr).toBe(true)
    })
  })
})
