import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import '@testing-library/jest-dom'

// Mock the API client BEFORE importing the component.
const mockGetLLMProviders = vi.fn()
const mockApplyLiveOverride = vi.fn()
vi.mock('@/lib/api', () => ({
  getLLMProviders: (...args: unknown[]) => mockGetLLMProviders(...args),
  applyLiveOverride: (...args: unknown[]) => mockApplyLiveOverride(...args),
}))

import { ModelSwapDialog } from '@/components/features/conversion/ModelSwapDialog'

const TWO_PROVIDERS = [
  {
    id: 'gemini',
    name: 'Google Gemini',
    type: 'gemini',
    base_url: 'https://generativelanguage.googleapis.com',
    api_key: '',
    concurrency: 4,
    models: [
      { model_id: 'gemini-flash', name: 'Flash' },
      { model_id: 'gemini-pro', name: 'Pro' },
    ],
  },
  {
    id: 'openai',
    name: 'OpenAI',
    type: 'openai',
    base_url: 'https://api.openai.com',
    api_key: '',
    models: [{ model_id: 'gpt-4', name: 'GPT-4' }],
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  mockGetLLMProviders.mockResolvedValue(TWO_PROVIDERS)
  mockApplyLiveOverride.mockResolvedValue({ status: 'applied' })
})

describe('ModelSwapDialog', () => {
  it('renders both Swap Model and Retry New provider tabs when onRetry is provided', async () => {
    render(
      <ModelSwapDialog
        open
        auto
        filename="doc.pdf"
        providerId="gemini"
        currentModel="gemini-flash"
        onClose={vi.fn()}
        onApplied={vi.fn()}
        onRetry={vi.fn()}
      />
    )
    await waitFor(() => expect(screen.getByText(/Swap Model/i)).toBeInTheDocument())
    expect(screen.getByText(/Retry New Provider/i)).toBeInTheDocument()
  })

  it('hides the retry tab when onRetry is omitted (manual same-provider swap)', async () => {
    render(
      <ModelSwapDialog
        open
        auto={false}
        filename="doc.pdf"
        providerId="gemini"
        currentModel="gemini-flash"
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />
    )
    await waitFor(() => expect(screen.getByText(/Apply Live/i)).toBeInTheDocument())
    expect(screen.queryByText(/Retry New Provider/i)).not.toBeInTheDocument()
  })

  it('calls applyLiveOverride when the Swap Model tab is applied', async () => {
    const onApplied = vi.fn()
    const onClose = vi.fn()
    render(
      <ModelSwapDialog
        open
        auto={false}
        filename="doc.pdf"
        providerId="gemini"
        currentModel="gemini-flash"
        onClose={onClose}
        onApplied={onApplied}
        onRetry={vi.fn()}
      />
    )
    await waitFor(() => expect(screen.getByText(/Apply Live/i)).toBeInTheDocument())
    // The Apply button is disabled until something changes. Open the model
    // dropdown and pick a different model (gemini-pro) to enable it.
    await act(async () => {
      fireEvent.click(screen.getByText('gemini-flash'))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('gemini-pro'))
    })
    await act(async () => {
      fireEvent.click(screen.getByText(/Apply Live/i))
    })
    await waitFor(() => expect(mockApplyLiveOverride).toHaveBeenCalled())
    expect(onApplied).toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
  })

  it('calls onRetry with the selected provider+model when the Retry tab is applied', async () => {
    const onRetry = vi.fn().mockResolvedValue(undefined)
    render(
      <ModelSwapDialog
        open
        auto
        filename="doc.pdf"
        providerId="gemini"
        currentModel="gemini-flash"
        onClose={vi.fn()}
        onApplied={vi.fn()}
        onRetry={onRetry}
      />
    )
    // Switch to the Retry tab.
    await waitFor(() => expect(screen.getByText(/Retry New Provider/i)).toBeInTheDocument())
    await act(async () => {
      fireEvent.click(screen.getByText(/Retry New Provider/i))
    })
    // Retry Now button visible.
    const retryBtn = await screen.findByText(/Retry Now/i)
    await act(async () => {
      fireEvent.click(retryBtn)
    })
    await waitFor(() => expect(onRetry).toHaveBeenCalled())
    // The default alt provider is openai (first non-gemini with models).
    const firstCall = onRetry.mock.calls[0]
    expect(firstCall).toBeDefined()
    const [provider, model] = firstCall!
    expect(provider).toBe('openai')
    expect(model).toBe('gpt-4')
  })
})
