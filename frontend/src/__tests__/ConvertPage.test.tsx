import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { ConvertPage } from '@/pages/ConvertPage'
import type { ConversionConfig } from '@/lib/api'
import '@testing-library/jest-dom'

// Mock react-router-dom
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return {
    ...actual,
    useNavigate: () => mockNavigate
  }
})

// Mock useConversionQueue hook
const mockUseConversionQueue = vi.fn()
vi.mock('@/hooks/useConversionQueue', () => ({
  useConversionQueue: () => mockUseConversionQueue()
}))

// Mock components that we don't want to render in full depth or that have external deps
vi.mock('@/components/features/FileUpload', () => ({
  FileUpload: ({
    onFilesSelect,
    fileEngineControls = [],
  }: {
    onFilesSelect: (files: File[]) => void
    fileEngineControls?: Array<{
      value: string
      status: string
      options: Array<{ value: string; label: string }>
      onChange: (value: string) => void
    }>
  }) => (
    <div data-testid="file-upload">
      FileUpload
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['pdf'], 'sample.pdf', { type: 'application/pdf' })])}
      >
        Mock select PDF
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([
          new File(['pdf'], 'sample.pdf', { type: 'application/pdf' }),
          new File(['name\tscore\nAda\t10\n'], 'sample.tsv', { type: 'text/tab-separated-values' }),
        ])}
      >
        Mock select PDF and TSV
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['name\tscore\nAda\t10\n'], 'sample.tsv', { type: 'text/tab-separated-values' })])}
      >
        Mock select TSV
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['docx'], 'report.docx', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })])}
      >
        Mock select DOCX
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['xls'], 'legacy.xls', { type: 'application/vnd.ms-excel' })])}
      >
        Mock select XLS
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['msg'], 'mail.msg', { type: 'application/vnd.ms-outlook' })])}
      >
        Mock select MSG
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['wav'], 'voice.wav', { type: 'audio/wav' })])}
      >
        Mock select WAV
      </button>
      <button
        type="button"
        onClick={() => onFilesSelect([new File(['mp4'], 'clip.mp4', { type: 'video/mp4' })])}
      >
        Mock select MP4
      </button>
      {fileEngineControls.map((control, index) => (
        <div data-testid={`file-engine-${index}`} key={index}>
          <span>{control.status}</span>
          <select
            aria-label={`Engine for file ${index + 1}`}
            value={control.value}
            onChange={(event) => control.onChange(event.target.value)}
          >
            {control.options.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
      ))}
    </div>
  )
}))

vi.mock('@/components/features/ConversionOptions', () => ({
  ConversionOptions: ({
    config,
    onChange,
    supportsMultiFormat,
  }: {
    config: ConversionConfig
    onChange: (cfg: ConversionConfig) => void
    supportsMultiFormat?: boolean
  }) => (
    <div data-testid="conversion-options">
      <span data-testid="config-format">{config.output_formats?.join(',')}</span>
      <span data-testid="config-ocr">{config.force_ocr ? 'ocr-enabled' : 'ocr-disabled'}</span>
      <span data-testid="supports-multi">{supportsMultiFormat ? 'multi' : 'markdown-only'}</span>
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

// The model-swap dialog pulls provider models + applies the override via the
// API client; stub those so the dialog renders without network.
const mockApplyLiveOverride = vi.fn().mockResolvedValue({ status: 'applied' })
const mockGetLLMProviders = vi.fn().mockResolvedValue([
  {
    id: 'gemini',
    type: 'gemini',
    label: 'Gemini',
    fallback_api_keys: [],
    concurrency: 4,
    models: [
      { model_id: 'gemini-3-flash-preview' },
      { model_id: 'gemini-2.0-flash' },
    ],
  },
])
const mockGetCapabilities = vi.fn().mockResolvedValue({
  engines: {
    marker_pdf: 'ready',
    office_docx: 'ready',
    office_pptx: 'ready',
    spreadsheet: 'ready',
    text_data: 'ready',
    html: 'ready',
  },
  output_formats: ['markdown', 'json', 'html', 'chunks'],
  marker_multi_format_extensions: ['.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tiff', '.bmp', '.gif', '.epub'],
  input_formats: [],
})
const mockPlanConversion = vi.fn().mockResolvedValue({
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
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    getLLMProviders: () => mockGetLLMProviders(),
    applyLiveOverride: (body: unknown) => mockApplyLiveOverride(body),
    getCapabilities: () => mockGetCapabilities(),
    planConversion: (filename: string, size: number, local_filepath?: string) => mockPlanConversion(filename, size, local_filepath),
    getPresets: () => Promise.resolve([]),
    savePreset: () => Promise.resolve({ id: 'preset_123', name: 'SavedPreset', config: {}, created_at: '' }),
    deletePreset: () => Promise.resolve({ success: true, message: '' }),
  }
})

describe('ConvertPage component', () => {
  it('renders initial state with empty queue and console closed by default', () => {
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
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

  it('shows metadata-only engine plan on the selected file as auto preview', async () => {
    mockPlanConversion.mockResolvedValueOnce({
      engine: 'marker_pdf',
      label: 'Marker PDF',
      confidence: 0.75,
      reasons: ['PDF complexity was not probed; using conservative Marker route'],
      needs_marker_models: true,
      needs_gpu: true,
      execution_backend: 'marker_worker',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: ['Preliminary filename-only plan; upload/local probing may change selected engine'],
      preliminary: true,
    })
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select PDF'))

    expect(await screen.findByText('Auto: backend will probe on upload')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Engine for file 1/i })).toHaveValue('auto')
    expect(screen.queryByText(/PDF complexity was not probed/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Preliminary filename-only plan/i)).not.toBeInTheDocument()
  })

  it('passes selected file engine override per source when converting', async () => {
    const start = vi.fn().mockResolvedValue(undefined)
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start,
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select PDF'))

    const select = await screen.findByRole('combobox', { name: /Engine for file 1/i })
    fireEvent.change(select, { target: { value: 'marker_pdf' } })
    const convertButton = await screen.findByRole('button', { name: /Convert 1 Document/i })
    await waitFor(() => expect(convertButton).not.toBeDisabled())
    fireEvent.click(convertButton)

    expect(start).toHaveBeenCalledTimes(1)
    const call = start.mock.calls[0]!
    expect(call[0]).toHaveLength(1)
    expect(call[4].fileKeys).toHaveLength(1)
    expect(call[4].fileEngineOverrides[call[4].fileKeys[0]]).toBe('marker_pdf')
  })

  it('offers the text data engine for TSV uploads', async () => {
    mockPlanConversion.mockResolvedValueOnce({
      engine: 'text_data',
      label: 'Text / Data',
      confidence: 0.95,
      reasons: ["Matched extension '.tsv'"],
      needs_marker_models: false,
      needs_gpu: false,
      execution_backend: 'cpu_thread',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: [],
      preliminary: true,
    })
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select TSV'))

    const select = await screen.findByRole('combobox', { name: /Engine for file 1/i })
    expect(within(select).getByRole('option', { name: 'Text / Data' })).toHaveValue('text_data')
  })

  it('offers the spreadsheet engine for legacy XLS uploads', async () => {
    mockPlanConversion.mockResolvedValueOnce({
      engine: 'spreadsheet',
      label: 'Fast Spreadsheet',
      confidence: 0.95,
      reasons: ["Matched extension '.xls'"],
      needs_marker_models: false,
      needs_gpu: false,
      execution_backend: 'cpu_thread',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: [],
      preliminary: true,
    })
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select XLS'))

    const select = await screen.findByRole('combobox', { name: /Engine for file 1/i })
    expect(within(select).getByRole('option', { name: 'Fast Spreadsheet' })).toHaveValue('spreadsheet')
  })

  it('offers the Outlook MSG engine for MSG uploads', async () => {
    mockPlanConversion.mockResolvedValueOnce({
      engine: 'outlook_msg',
      label: 'Outlook MSG',
      confidence: 0.95,
      reasons: ["Matched extension '.msg'"],
      needs_marker_models: false,
      needs_gpu: false,
      execution_backend: 'cpu_thread',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: [],
      preliminary: true,
    })
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select MSG'))

    const select = await screen.findByRole('combobox', { name: /Engine for file 1/i })
    expect(within(select).getByRole('option', { name: 'Outlook MSG' })).toHaveValue('outlook_msg')
  })

  it('offers the local audio transcript engine for audio uploads', async () => {
    mockPlanConversion.mockResolvedValueOnce({
      engine: 'audio',
      label: 'Local Audio Transcript',
      confidence: 0.95,
      reasons: ["Matched extension '.wav'"],
      needs_marker_models: false,
      needs_gpu: false,
      execution_backend: 'cpu_thread',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: [],
      preliminary: true,
    })
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select WAV'))

    const select = await screen.findByRole('combobox', { name: /Engine for file 1/i })
    expect(within(select).getByRole('option', { name: 'Local Audio Transcript' })).toHaveValue('audio')
  })

  it('offers the local video timeline engine for video uploads', async () => {
    mockPlanConversion.mockResolvedValueOnce({
      engine: 'video',
      label: 'Local Video Timeline',
      confidence: 0.9,
      reasons: ["Matched extension '.mp4'"],
      needs_marker_models: false,
      needs_gpu: false,
      execution_backend: 'cpu_thread',
      needs_cloud: false,
      optional_dependencies: [],
      fallback_chain: [],
      warnings: [],
      preliminary: true,
    })
    mockUseConversionQueue.mockReturnValue({
      jobs: [],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)
    fireEvent.click(screen.getByText('Mock select MP4'))

    const select = await screen.findByRole('combobox', { name: /Engine for file 1/i })
    expect(within(select).getByRole('option', { name: 'Local Video Timeline' })).toHaveValue('video')
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
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
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

  it('previews clean result_text, never the ZIP download blob (regression)', async () => {
    // Regression for the "PK..." binary garbage bug: when images are extracted
    // the download blob is a ZIP. The preview must use the clean result_text
    // from /status (same source as the history page), NOT decode the blob.
    const zipBytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00]) // "PK\x03\x04..."
    const zipBlob = new Blob([zipBytes], { type: 'application/zip' })
    const cleanMarkdown = '# OPENSKILL: Open-World Self-Evolution\n\nThis is the clean document text.'

    mockUseConversionQueue.mockReturnValue({
      jobs: [
        {
          id: 'job-zip',
          filename: 'openskill.pdf',
          file: null,
          localPath: '',
          phase: 'completed',
          progress: 100,
          statusText: 'Conversion complete',
          jobId: 'job-uuid-zip',
          error: null,
          resultBlob: zipBlob,
          resultText: cleanMarkdown,
          logs: [],
          outputFormat: 'markdown',
          imageUnderstanding: null,
        }
      ],
      start: vi.fn(),
      cancel: vi.fn(),
      download: vi.fn(),
      clearLogs: vi.fn(),
      removeJob: vi.fn(),
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
    })

    render(<ConvertPage />)

    // The clean heading renders (ReactMarkdown turns it into an <h1>).
    expect(
      await screen.findByRole('heading', { name: /OPENSKILL: Open-World Self-Evolution/i })
    ).toBeInTheDocument()
    expect(screen.getByText(/This is the clean document text\./)).toBeInTheDocument()

    // The ZIP magic bytes must never appear in the preview.
    expect(screen.queryByText(/PK/)).not.toBeInTheDocument()
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
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
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
      regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
      clearRateLimited: vi.fn(),
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
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      const customConfig = {
        output_formats: ['json'],
        converter: 'TableConverter',
        use_llm: true,
        force_ocr: true,
      }
      localStorage.setItem('marker-conversion-config', JSON.stringify(customConfig))

      render(<ConvertPage />)

      expect(screen.getByTestId('config-format')).toHaveTextContent('json')
      expect(screen.getByTestId('config-ocr')).toHaveTextContent('ocr-enabled')
    })

    it('collapses saved structured output formats when selected source cannot render them', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      localStorage.setItem('marker-conversion-config', JSON.stringify({ output_formats: ['json'] }))

      render(<ConvertPage />)
      fireEvent.click(screen.getByText('Mock select TSV'))

      await waitFor(() => {
        expect(screen.getByTestId('supports-multi')).toHaveTextContent('markdown-only')
        expect(screen.getByTestId('config-format')).toHaveTextContent('markdown')
      })
    })

    it('keeps saved chunks output format for native sources', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      localStorage.setItem('marker-conversion-config', JSON.stringify({ output_formats: ['chunks'] }))

      render(<ConvertPage />)
      fireEvent.click(screen.getByText('Mock select TSV'))

      await waitFor(() => {
        expect(screen.getByTestId('supports-multi')).toHaveTextContent('markdown-only')
        expect(screen.getByTestId('config-format')).toHaveTextContent('chunks')
      })
    })

    it('does not offer incompatible Marker override for DOCX sources', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })
      mockPlanConversion.mockResolvedValueOnce({
        engine: 'office_docx',
        label: 'Fast Office (Word)',
        confidence: 0.95,
        reasons: [],
        needs_marker_models: false,
        needs_gpu: false,
        execution_backend: 'cpu_thread',
        needs_cloud: false,
        optional_dependencies: [],
        fallback_chain: [],
        warnings: [],
        preliminary: true,
      })

      render(<ConvertPage />)
      fireEvent.click(screen.getByText('Mock select DOCX'))

      const control = await screen.findByLabelText('Engine for file 1')
      expect(within(control).queryByRole('option', { name: 'Marker PDF' })).not.toBeInTheDocument()
      expect(within(control).getByRole('option', { name: 'Fast Office (Word)' })).toBeInTheDocument()
    })

    it('requires every selected source to support multi-format rendering', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      render(<ConvertPage />)
      fireEvent.click(screen.getByText('Mock select PDF and TSV'))

      await waitFor(() => {
        expect(screen.getByTestId('supports-multi')).toHaveTextContent('markdown-only')
      })
    })

    it('uses backend capability extensions for multi-format support', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })
      mockGetCapabilities.mockResolvedValueOnce({
        engines: { office_docx: 'ready' },
        output_formats: ['markdown', 'json', 'html', 'chunks'],
        marker_multi_format_extensions: ['.docx'],
        input_formats: [],
      })
      localStorage.setItem('marker-conversion-config', JSON.stringify({ output_formats: ['json'] }))

      render(<ConvertPage />)
      fireEvent.click(screen.getByText('Mock select DOCX'))

      await waitFor(() => {
        expect(screen.getByTestId('supports-multi')).toHaveTextContent('multi')
        expect(screen.getByTestId('config-format')).toHaveTextContent('json')
      })
    })

    it('saves config to localStorage when changes are applied', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
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

  describe('model-swap on rate limit', () => {
    const runningLLMJob = (overrides: Record<string, unknown> = {}) => ({
      id: 'job-run',
      filename: 'openskill.pdf',
      file: null,
      localPath: '',
      phase: 'processing',
      progress: 92,
      statusText: 'Extracting tables...',
      jobId: 'job-uuid-run',
      error: null,
      resultBlob: null,
      resultText: null,
      logs: [],
      outputFormat: 'markdown',
      llmProvider: 'gemini',
      llmModel: 'gemini-3-flash-preview',
      ...overrides,
    })

    it('shows a Switch Model button on a running LLM job', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [runningLLMJob()],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      render(<ConvertPage />)
      expect(screen.getByRole('button', { name: /Switch Model/i })).toBeInTheDocument()
    })

    it('opens the swap dialog manually with the current model shown', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [runningLLMJob()],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      render(<ConvertPage />)
      fireEvent.click(screen.getByRole('button', { name: /Switch Model/i }))

      const dialog = await screen.findByRole('dialog')
      expect(dialog).toBeInTheDocument()
      expect(within(dialog).getByRole('heading', { name: /Switch Model/i })).toBeInTheDocument()
      expect(within(dialog).getByText(/Current: gemini-3-flash-preview/i)).toBeInTheDocument()
    })

    it('auto-surfaces the dialog with the rate-limit header when stuck', async () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [runningLLMJob({ rateLimited: true })],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      render(<ConvertPage />)
      expect(await screen.findByRole('dialog')).toBeInTheDocument()
      expect(screen.getByText(/Hitting Rate Limits/i)).toBeInTheDocument()
    })

    it('does not auto-surface once the prompt was dismissed', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [runningLLMJob({ rateLimited: true, swapPromptDismissed: true })],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
      dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
      })

      render(<ConvertPage />)
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })

    it('shows a rate-limited banner on a running job that is rateLimited', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [runningLLMJob({ rateLimited: true, swapPromptDismissed: true })],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
        retryJob: vi.fn(),
      })

      render(<ConvertPage />)
      expect(screen.getByText(/Rate-limited — swap model or retry/i)).toBeInTheDocument()
    })

    it('shows a partial-failure banner on a completed job with partialFailure', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [
          runningLLMJob({
            phase: 'completed',
            progress: 100,
            statusText: 'Done',
            partialFailure: true,
            swapPromptDismissed: true,
          }),
        ],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
        retryJob: vi.fn(),
      })

      render(<ConvertPage />)
      expect(screen.getByText(/Some LLM steps skipped/i)).toBeInTheDocument()
      // Both the banner and the action button carry "Retry" — assert at least one.
      expect(screen.getAllByRole('button', { name: /Retry/i }).length).toBeGreaterThan(0)
    })

    it('does not show the rate-limited banner when rateLimited is false', () => {
      mockUseConversionQueue.mockReturnValue({
        jobs: [runningLLMJob({ rateLimited: false })],
        start: vi.fn(),
        cancel: vi.fn(),
        download: vi.fn(),
        clearLogs: vi.fn(),
        removeJob: vi.fn(),
        regenerateJobFormat: vi.fn(),
        dismissSwapPrompt: vi.fn(),
        clearRateLimited: vi.fn(),
        retryJob: vi.fn(),
      })

      render(<ConvertPage />)
      expect(screen.queryByText(/Rate-limited — swap model or retry/i)).not.toBeInTheDocument()
    })
  })
})
