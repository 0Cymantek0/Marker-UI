import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { SettingsPage } from '@/pages/SettingsPage'
import * as api from '@/lib/api'
import { toast } from 'sonner'
import '@testing-library/jest-dom'

vi.mock('@/lib/api', () => ({
  getSettings: vi.fn(),
  getGPUStatus: vi.fn(),
  installGPU: vi.fn(),
  toggleGPU: vi.fn(),
  getLLMProviders: vi.fn(),
  saveLLMProviders: vi.fn(),
  getActiveLLM: vi.fn(),
  setActiveLLM: vi.fn(),
  fetchAvailableModels: vi.fn(),
  selfHealModels: vi.fn(),
  resetModels: vi.fn(),
  updateSetting: vi.fn(),
  getGPUWorkersResolved: vi.fn().mockResolvedValue({
    mode: 'auto',
    detected: 1,
    effective: 1,
    active: 'true',
    restart_required: false,
  }),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  }
}))

describe('SettingsPage component', () => {
  const mockProviders: api.LLMProvider[] = [
    {
      id: 'gemini',
      type: 'gemini',
      label: 'Gemini',
      api_key: 'gemini-key',
      fallback_api_keys: [],
      base_url: '',
      models: [
        { model_id: 'gemini-2.0-flash' }
      ]
    },
    {
      id: 'claude',
      type: 'claude',
      label: 'Claude',
      api_key: '',
      fallback_api_keys: [],
      base_url: '',
      models: [
        { model_id: 'claude-3-7-sonnet' }
      ]
    }
  ]

  const mockActive: api.ActiveLLM = {
    provider_id: 'gemini',
    model_id: 'gemini-2.0-flash'
  }

  const mockSettings: api.SettingsResponse[] = [
    { key: 'gpu_acceleration_enabled', value: 'false', category: 'gpu', description: null },
    { key: 'vlm_model', value: '', category: 'image', description: null },
    { key: 'max_images_per_doc', value: '50', category: 'image', description: null }
  ]

  const mockGPUStatus: api.GPUStatus = {
    status: 'ready',
    progress: 100,
    logs: [],
    error_message: null,
    cuda_available: true
  }

  beforeEach(() => {
    vi.restoreAllMocks()
    vi.mocked(api.getLLMProviders).mockResolvedValue(mockProviders)
    vi.mocked(api.getActiveLLM).mockResolvedValue(mockActive)
    vi.mocked(api.getSettings).mockResolvedValue(mockSettings)
    vi.mocked(api.getGPUStatus).mockResolvedValue(mockGPUStatus)
  })

  it('renders configured providers and handles draft state correctly on cancel', async () => {
    render(<SettingsPage />)

    // Wait for initial load
    expect(await screen.findByText('Configured Service Providers')).toBeInTheDocument()
    expect(screen.getByText('Gemini')).toBeInTheDocument()

    // Find and click Models button for Gemini
    const modelsButtons = screen.getAllByRole('button', { name: /Models \(\d+\)/ })
    // The first one is for Gemini (models length 1)
    await act(async () => {
      fireEvent.click(modelsButtons[0]!)
    })

    // Drawer should show
    expect(await screen.findByText('Gemini Models')).toBeInTheDocument()

    // Click cancel in drawer
    const cancelBtn = screen.getByRole('button', { name: 'Cancel' })
    await act(async () => {
      fireEvent.click(cancelBtn)
    })

    // Drawer should close, save function should not be called
    expect(api.saveLLMProviders).not.toHaveBeenCalled()
  })

  it('correctly persists changes only on save', async () => {
    vi.mocked(api.fetchAvailableModels).mockResolvedValue(['gemini-3-flash-preview', 'gemini-2.0-flash'])
    vi.mocked(api.saveLLMProviders).mockResolvedValue(mockProviders)

    render(<SettingsPage />)

    // Wait for load
    await screen.findByText('Configured Service Providers')

    // Open Models drawer for Gemini
    const modelsButtons = screen.getAllByRole('button', { name: /Models \(\d+\)/ })
    await act(async () => {
      fireEvent.click(modelsButtons[0]!)
    })

    // Fetch models list
    const fetchModelsBtn = await screen.findByRole('button', { name: 'Fetch Models' })
    await act(async () => {
      fireEvent.click(fetchModelsBtn)
    })

    // Find the 'Add' action for gemini-3-flash-preview
    const addBtns = screen.getAllByRole('button', { name: 'Add' })
    // The first one is typically the fetched model list's "Add" button
    await act(async () => {
      fireEvent.click(addBtns[0]!)
    })

    expect(toast.success).toHaveBeenCalledWith('Model "gemini-3-flash-preview" added')

    // Click Cancel first to verify it doesn't save
    const cancelBtn = screen.getByRole('button', { name: 'Cancel' })
    await act(async () => {
      fireEvent.click(cancelBtn)
    })

    expect(api.saveLLMProviders).not.toHaveBeenCalled()

    // Open models drawer again
    await act(async () => {
      fireEvent.click(modelsButtons[0]!)
    })

    // Fetch models again
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Fetch Models' }))
    })

    // Click add again
    const addBtns2 = screen.getAllByRole('button', { name: 'Add' })
    await act(async () => {
      fireEvent.click(addBtns2[0]!)
    })

    // Now click Save Models
    const saveBtn = screen.getByRole('button', { name: 'Save Models' })
    await act(async () => {
      fireEvent.click(saveBtn)
    })

    // Verify it saved the updated list containing the new model
    expect(api.saveLLMProviders).toHaveBeenCalled()
    const savedArg = vi.mocked(api.saveLLMProviders).mock.calls[0]![0]
    const geminiSaved = savedArg.find(p => p.id === 'gemini')
    expect(geminiSaved?.models).toContainEqual({ model_id: 'gemini-3-flash-preview' })
  })

  it('opens and closes the reset confirmation overlay', async () => {
    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    const resetBtn = screen.getByRole('button', { name: 'Reset Environment' })
    await act(async () => {
      fireEvent.click(resetBtn)
    })

    expect(screen.getByText('Confirm System Reset')).toBeInTheDocument()

    const goBackBtn = screen.getByRole('button', { name: 'Go Back' })
    await act(async () => {
      fireEvent.click(goBackBtn)
    })

    expect(api.resetModels).not.toHaveBeenCalled()
  })

  it('renders a vision toggle per model in the models drawer', async () => {
    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    const modelsButtons = screen.getAllByRole('button', { name: /Models \(\d+\)/ })
    await act(async () => {
      fireEvent.click(modelsButtons[0]!)
    })

    expect(await screen.findByText('Gemini Models')).toBeInTheDocument()

    const visionToggles = screen.getAllByRole('switch', { name: /vision capability/i })
    expect(visionToggles.length).toBeGreaterThanOrEqual(1)
  })

  it('toggling vision flag updates model and persists on save', async () => {
    vi.mocked(api.saveLLMProviders).mockClear()
    vi.mocked(api.saveLLMProviders).mockResolvedValue(mockProviders)

    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    const modelsButtons = screen.getAllByRole('button', { name: /Models \(\d+\)/ })
    await act(async () => {
      fireEvent.click(modelsButtons[0]!)
    })

    expect(await screen.findByText('Gemini Models')).toBeInTheDocument()

    const visionToggle = screen.getByRole('switch', { name: /vision capability for gemini-2\.0-flash/i })
    expect(visionToggle).toHaveAttribute('aria-checked', 'false')

    await act(async () => {
      fireEvent.click(visionToggle)
    })

    expect(visionToggle).toHaveAttribute('aria-checked', 'true')

    const saveBtn = screen.getByRole('button', { name: 'Save Models' })
    await act(async () => {
      fireEvent.click(saveBtn)
    })

    expect(api.saveLLMProviders).toHaveBeenCalledTimes(1)
    const savedArg = vi.mocked(api.saveLLMProviders).mock.calls[0]![0]
    const geminiSaved = savedArg.find((p) => p.id === 'gemini')
    const target = geminiSaved?.models.find((m) => m.model_id === 'gemini-2.0-flash')
    expect(target?.vision_capable).toBe(true)
  })

  it('renders the Image Understanding Defaults section with seeded values', async () => {
    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    expect(screen.getByText('Image Understanding Defaults')).toBeInTheDocument()
    expect(screen.getByText('Default Vision Model')).toBeInTheDocument()
    expect(screen.getByText('Per-document Image Cap')).toBeInTheDocument()
    // Seeded max_images_per_doc=50 renders in the number input.
    const capInput = screen.getByRole('spinbutton', { name: /per-document image cap/i }) as HTMLInputElement
    expect(capInput.value).toBe('50')
  })

  it('lists every configured model in the vision dropdown even when none are vision-capable (ISSUE-3)', async () => {
    // Regression: the dropdown used to filter to vision_capable models only, so
    // with the default seed (all vision_capable=false) the user saw nothing but
    // "Auto" and could never pick a model. It must now show all configured models.
    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    // The Default Vision Model select is the trigger button under that label.
    const label = screen.getByText('Default Vision Model')
    const wrapper = label.parentElement as HTMLElement
    const trigger = wrapper.querySelector('button') as HTMLButtonElement
    await act(async () => {
      fireEvent.click(trigger)
    })

    // Both providers' models appear despite vision_capable being unset.
    expect(screen.getByText(/Gemini: gemini-2\.0-flash/)).toBeInTheDocument()
    expect(screen.getByText(/Claude: claude-3-7-sonnet/)).toBeInTheDocument()
  })

  it('saves the selected vision model via updateSetting (ISSUE-3)', async () => {
    vi.mocked(api.updateSetting).mockResolvedValue(undefined)
    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    const label = screen.getByText('Default Vision Model')
    const wrapper = label.parentElement as HTMLElement
    const trigger = wrapper.querySelector('button') as HTMLButtonElement
    await act(async () => {
      fireEvent.click(trigger)
    })
    await act(async () => {
      fireEvent.click(screen.getByText(/Claude: claude-3-7-sonnet/))
    })

    expect(api.updateSetting).toHaveBeenCalledWith('vlm_model', 'claude-3-7-sonnet', 'image')
  })

  it('changing the image cap saves via updateSetting', async () => {
    vi.mocked(api.updateSetting).mockResolvedValue(undefined)
    render(<SettingsPage />)
    await screen.findByText('Configured Service Providers')

    const capInput = screen.getByRole('spinbutton', { name: /per-document image cap/i }) as HTMLInputElement
    await act(async () => {
      fireEvent.change(capInput, { target: { value: '100' } })
    })
    await act(async () => {
      fireEvent.blur(capInput)
    })

    expect(api.updateSetting).toHaveBeenCalledWith('max_images_per_doc', '100', 'image')
  })
})
