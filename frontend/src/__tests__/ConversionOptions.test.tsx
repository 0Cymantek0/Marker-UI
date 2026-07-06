import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConversionOptions } from '@/components/features/ConversionOptions'
import type { ConversionConfig, LLMProvider, ActiveLLM } from '@/lib/api'
import '@testing-library/jest-dom'

const mockGetLLMProviders = vi.fn()
const mockGetActiveLLM = vi.fn()
const mockGetPresets = vi.fn().mockResolvedValue([])
const mockSavePreset = vi.fn().mockResolvedValue({ id: 'preset_123', name: 'Saved', config: {}, created_at: '' })
const mockDeletePreset = vi.fn().mockResolvedValue({ success: true, message: '' })
const mockGetAudioCapabilities = vi.fn().mockResolvedValue([])
const mockGetVocabularyPacks = vi.fn().mockResolvedValue([])
const mockSaveVocabularyPack = vi.fn().mockResolvedValue({ id: 'v1', name: 'Test', terms: [], category: '', created_at: '' })
const mockDeleteVocabularyPack = vi.fn().mockResolvedValue(undefined)

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    getLLMProviders: (...args: unknown[]) => mockGetLLMProviders(...args),
    getActiveLLM: (...args: unknown[]) => mockGetActiveLLM(...args),
    getPresets: (...args: unknown[]) => mockGetPresets(...args),
    savePreset: (...args: unknown[]) => mockSavePreset(...args),
    deletePreset: (...args: unknown[]) => mockDeletePreset(...args),
    getAudioCapabilities: (...args: unknown[]) => mockGetAudioCapabilities(...args),
    getVocabularyPacks: (...args: unknown[]) => mockGetVocabularyPacks(...args),
    saveVocabularyPack: (...args: unknown[]) => mockSaveVocabularyPack(...args),
    deleteVocabularyPack: (...args: unknown[]) => mockDeleteVocabularyPack(...args),
  }
})

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
  },
}))

const baseConfig: ConversionConfig = {
  output_formats: ['markdown'],
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
    mockGetPresets.mockResolvedValue([])
    mockSavePreset.mockResolvedValue({ id: 'preset_123', name: 'SavedPreset', config: {}, created_at: '' })
    mockDeletePreset.mockResolvedValue({ success: true, message: '' })
    mockGetAudioCapabilities.mockResolvedValue([])
    mockGetVocabularyPacks.mockResolvedValue([])
    mockSaveVocabularyPack.mockResolvedValue({ id: 'v1', name: 'Test', terms: [], category: '', created_at: '' })
    mockDeleteVocabularyPack.mockResolvedValue(undefined)
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

  it('allows selecting different conversion profiles', async () => {
    mockGetLLMProviders.mockResolvedValue([])
    mockGetActiveLLM.mockResolvedValue(null)
    const onChange = vi.fn()

    const { rerender } = render(<ConversionOptions config={baseConfig} onChange={onChange} />)

    expect(screen.getByRole('button', { name: /AutoProbes/i })).toBeInTheDocument()

    const fastButton = screen.getByRole('button', { name: /FastForce/i })
    expect(fastButton).toBeInTheDocument()
    fireEvent.click(fastButton)

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ conversion_profile: 'fast' })
    )

    // Re-render with the new config to assert warning visibility
    rerender(<ConversionOptions config={{ ...baseConfig, conversion_profile: 'fast' }} onChange={onChange} />)
    expect(screen.getByText(/may be less accurate/i)).toBeInTheDocument()

    const highAccuracyButton = screen.getByRole('button', { name: /High AccuracyForces/i })
    expect(highAccuracyButton).toBeInTheDocument()
    fireEvent.click(highAccuracyButton)

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ conversion_profile: 'high_accuracy' })
    )
  })

  it('keeps chunks selectable when marker multi-format renderers are unavailable', async () => {
    mockGetLLMProviders.mockResolvedValue([])
    mockGetActiveLLM.mockResolvedValue(null)
    const onChange = vi.fn()

    render(<ConversionOptions config={baseConfig} onChange={onChange} supportsMultiFormat={false} />)

    expect(screen.getByRole('button', { name: /markdown/i })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: /chunks/i })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: /json/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /html/i })).toBeDisabled()
  })

  it('shows and applies chunking strategy when chunks output is selected', async () => {
    mockGetLLMProviders.mockResolvedValue([])
    mockGetActiveLLM.mockResolvedValue(null)
    const onChange = vi.fn()

    const { rerender } = render(
      <ConversionOptions
        config={{ ...baseConfig, output_formats: ['chunks'] }}
        onChange={onChange}
        supportsMultiFormat={false}
      />
    )

    expect(screen.getByText('Chunking Strategy')).toBeInTheDocument()
    const trigger = screen.getByRole('button', { name: /markdown headings/i })
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('button', { name: /unstructured by title/i }))

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ chunking_strategy: 'unstructured_by_title' })
    )

    rerender(
      <ConversionOptions
        config={{ ...baseConfig, output_formats: ['markdown'] }}
        onChange={onChange}
        supportsMultiFormat={false}
      />
    )
    expect(screen.queryByText('Chunking Strategy')).not.toBeInTheDocument()
  })

  describe('Conversion Presets UI flow', () => {
    it('loads and lists presets on mount', async () => {
      const mockPresets = [
        {
          id: 'preset_1',
          name: 'Fast OCR',
          description: 'Fast mode with OCR enabled',
          config: { conversion_profile: 'fast', force_ocr: true },
          created_at: new Date().toISOString()
        }
      ]
      mockGetPresets.mockResolvedValueOnce(mockPresets)
      mockGetLLMProviders.mockResolvedValue([])
      mockGetActiveLLM.mockResolvedValue(null)

      render(<ConversionOptions config={baseConfig} onChange={vi.fn()} />)

      // The select element should display custom by default since baseConfig doesn't match preset_1
      expect(await screen.findByRole('button', { name: /custom configuration/i })).toBeInTheDocument()
    })

    it('allows opening the save preset inline form and saving the configuration', async () => {
      mockGetPresets.mockResolvedValue([])
      mockGetLLMProviders.mockResolvedValue([])
      mockGetActiveLLM.mockResolvedValue(null)
      mockSavePreset.mockResolvedValueOnce({
        id: 'preset_2',
        name: 'Super High Quality',
        config: baseConfig,
        created_at: new Date().toISOString()
      })

      render(<ConversionOptions config={baseConfig} onChange={vi.fn()} />)

      const saveCurrentBtn = screen.getByRole('button', { name: /save current/i })
      fireEvent.click(saveCurrentBtn)

      // Form should be visible
      const nameInput = screen.getByPlaceholderText(/e.g. OCR High Accuracy/i)
      const descInput = screen.getByPlaceholderText(/e.g. Max layout\/VLM settings/i)
      expect(nameInput).toBeInTheDocument()

      fireEvent.change(nameInput, { target: { value: 'Super High Quality' } })
      fireEvent.change(descInput, { target: { value: 'Awesome config' } })

      const savePresetSubmitBtn = screen.getByRole('button', { name: /save preset/i })
      fireEvent.click(savePresetSubmitBtn)

      await waitFor(() => {
        expect(mockSavePreset).toHaveBeenCalledWith('Super High Quality', expect.any(Object), 'Awesome config')
      })
    })
  })
})
