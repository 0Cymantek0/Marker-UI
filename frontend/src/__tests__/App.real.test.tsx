import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import '@testing-library/jest-dom'
import App from '@/App'

const mockGetCapabilities = vi.fn()
const mockGetHistory = vi.fn()
const mockGetPresets = vi.fn()
const mockPlanConversion = vi.fn()

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    getCapabilities: () => mockGetCapabilities(),
    getHistory: (...args: unknown[]) => mockGetHistory(...args),
    getPresets: (...args: unknown[]) => mockGetPresets(...args),
    planConversion: (...args: unknown[]) => mockPlanConversion(...args),
    getJobEvents: vi.fn(),
    getLLMProviders: vi.fn().mockResolvedValue([]),
    getActiveLLM: vi.fn().mockResolvedValue(null),
  }
})

describe('App real smoke', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mockGetCapabilities.mockResolvedValue({
      engines: {
        marker_pdf: 'ready',
        office_docx: 'ready',
      },
      output_formats: ['markdown', 'json', 'html', 'chunks'],
      marker_multi_format_extensions: ['.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tiff', '.bmp', '.gif', '.epub'],
      input_formats: [],
    })
    mockGetHistory.mockResolvedValue({ jobs: [], total: 0 })
    mockGetPresets.mockResolvedValue([])
    mockPlanConversion.mockResolvedValue({
      engine: 'marker_pdf',
      label: 'Marker PDF',
      output_formats: ['markdown', 'json', 'html', 'chunks'],
      confidence: 1,
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
  })

  it('renders the real convert route inside the real app layout', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>
    )

    expect(await screen.findByRole('heading', { name: 'Convert Document' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /upload files/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /local paths/i })).toBeInTheDocument()

    await waitFor(() => expect(mockGetCapabilities).toHaveBeenCalled())
    await waitFor(() => expect(mockGetHistory).toHaveBeenCalled())
  })
})
