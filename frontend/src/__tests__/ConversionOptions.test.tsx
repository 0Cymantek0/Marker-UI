import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConversionOptions } from '@/components/features/ConversionOptions'
import type { ConversionConfig, LLMProvider, ActiveLLM } from '@/lib/api'
import '@testing-library/jest-dom'

const mockGetLLMProviders = vi.fn()
const mockGetActiveLLM = vi.fn()

vi.mock('@/lib/api', () => ({
  getLLMProviders: (...args: any[]) => mockGetLLMProviders(...args),
  getActiveLLM: (...args: any[]) => mockGetActiveLLM(...args),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
  },
}))

const baseConfig: ConversionConfig = {
  output_format: 'markdown',
  converter: 'PdfConverter',
  use_llm: false,
  image_handling_mode: 'extraction',
  allow_cloud_vlm: false,
}

const active: ActiveLLM = {
  provider_id: 'openai',
  model_id: 'gpt-4o',
}

describe('ConversionOptions image understanding controls', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('requires explicit cloud image analysis opt-in', async () => {
    const providers: LLMProvider[] = [
      {
        id: 'openai',
        type: 'openai',
        label: 'OpenAI',
        fallback_api_keys: [],
        models: [{ model_id: 'gpt-4o', vision_capable: true }],
      },
    ]
    mockGetLLMProviders.mockResolvedValue(providers)
    mockGetActiveLLM.mockResolvedValue(active)
    const onChange = vi.fn()

    render(<ConversionOptions config={baseConfig} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: /configure advanced settings/i }))

    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /both/i })).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('radio', { name: /both/i }))
    fireEvent.click(screen.getByRole('button', { name: /allow cloud image analysis/i }))
    fireEvent.click(screen.getByRole('button', { name: /apply settings/i }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        image_handling_mode: 'both',
        allow_cloud_vlm: true,
      })
    )
  })

  it('saves the 3-way image handling radio selection', async () => {
    const providers: LLMProvider[] = [
      {
        id: 'openai',
        type: 'openai',
        label: 'OpenAI',
        fallback_api_keys: [],
        models: [{ model_id: 'gpt-4o', vision_capable: true }],
      },
    ]
    mockGetLLMProviders.mockResolvedValue(providers)
    mockGetActiveLLM.mockResolvedValue(active)
    const onChange = vi.fn()

    render(<ConversionOptions config={baseConfig} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: /configure advanced settings/i }))

    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /both/i })).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('radio', { name: /both/i }))
    fireEvent.click(screen.getByRole('button', { name: /apply settings/i }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ image_handling_mode: 'both' })
    )
  })

  it('exposes pipeline knobs and applies router + tuning overrides', async () => {
    const providers: LLMProvider[] = [
      {
        id: 'openai',
        type: 'openai',
        label: 'OpenAI',
        fallback_api_keys: [],
        models: [{ model_id: 'gpt-4o', vision_capable: true }],
      },
    ]
    mockGetLLMProviders.mockResolvedValue(providers)
    mockGetActiveLLM.mockResolvedValue(active)
    const onChange = vi.fn()

    render(<ConversionOptions config={baseConfig} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: /configure advanced settings/i }))

    // Knobs only show once an understanding mode is active.
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /both/i })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('radio', { name: /both/i }))

    // Everyday router toggle is present and flips.
    const router = await screen.findByRole('button', { name: /smart image router/i })
    expect(router).toBeInTheDocument()
    fireEvent.click(router)

    // Tuning section is collapsed until disclosed.
    expect(screen.queryByLabelText(/vlm batch size/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /experimental \/ tuning/i }))

    const batchSize = await screen.findByLabelText(/vlm batch size/i)
    fireEvent.change(batchSize, { target: { value: '16' } })

    fireEvent.click(screen.getByRole('button', { name: /apply settings/i }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        image_handling_mode: 'both',
        router_enabled: false,
        vlm_batch_size: 16,
      })
    )
  })

  it('exposes the smart router level dropdown and applies it', async () => {
    const providers: LLMProvider[] = [
      {
        id: 'openai',
        type: 'openai',
        label: 'OpenAI',
        fallback_api_keys: [],
        models: [{ model_id: 'gpt-4o', vision_capable: true }],
      },
    ]
    mockGetLLMProviders.mockResolvedValue(providers)
    mockGetActiveLLM.mockResolvedValue(active)
    const onChange = vi.fn()

    render(<ConversionOptions config={baseConfig} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: /configure advanced settings/i }))

    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /both/i })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('radio', { name: /both/i }))

    // The level dropdown is visible (router is on by default) and starts on smart.
    const levelTrigger = await screen.findByRole('button', { name: /smart \(layout-aware\)/i })
    const desc = screen.getByTestId('smart-router-desc')
    expect(desc.textContent).toMatch(/local Surya layout model/i)

    // Open the custom select and pick beeg_brain; the pros/cons line updates live.
    fireEvent.click(levelTrigger)
    fireEvent.click(await screen.findByRole('button', { name: /beeg brain/i }))
    expect(screen.getByTestId('smart-router-desc').textContent).toMatch(/highest accuracy/i)

    fireEvent.click(screen.getByRole('button', { name: /apply settings/i }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        image_handling_mode: 'both',
        smart_router_level: 'beeg_brain',
      })
    )
  })

  it('disables understanding modes when no vision-capable model is configured', async () => {
    const providers: LLMProvider[] = [
      {
        id: 'openai',
        type: 'openai',
        label: 'OpenAI',
        fallback_api_keys: [],
        models: [{ model_id: 'gpt-3.5-turbo', vision_capable: false }],
      },
    ]
    mockGetLLMProviders.mockResolvedValue(providers)
    mockGetActiveLLM.mockResolvedValue(active)

    render(<ConversionOptions config={baseConfig} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /configure advanced settings/i }))

    const understanding = await screen.findByRole('radio', { name: /understanding only/i })
    const extraction = screen.getByRole('radio', { name: /extraction only/i })
    const both = screen.getByRole('radio', { name: /both/i })

    expect(understanding).toBeDisabled()
    expect(both).toBeDisabled()
    expect(extraction).not.toBeDisabled()
    expect(screen.getByText(/enable vision capability/i)).toBeInTheDocument()
  })
})
