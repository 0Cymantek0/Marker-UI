import { useState, useEffect, useRef } from 'react'
import {
  Save,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  Key,
  Globe,
  Sliders,
  Plus,
  Trash2,
  ChevronDown,
  ChevronUp,
  ChevronRight,
  X,
  Settings,
  ListPlus,
  Activity,
  Trash,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select } from '@/components/ui/select'
import {
  getSettings,
  getGPUStatus,
  installGPU,
  toggleGPU,
  getGPUWorkersResolved,
  setGPUWorkers,
  getLLMProviders,
  saveLLMProviders,
  getActiveLLM,
  setActiveLLM,
  fetchAvailableModels,
  selfHealModels,
  resetModels,
  updateSetting,
  type BackendLLMConfig,
  type LLMProvider,
  type ModelConfig,
  type ActiveLLM,
  type GPUStatus,
  type GPUWorkerMode,
  type GPUWorkersResolved,
} from '@/lib/api'
import { cn } from '@/lib/utils'
import { messageFromUnknownError } from '@/lib/errors'
import { PageHeader } from '@/components/layout/PageHeader'
import { TestConnectionButton } from '@/components/features/settings/TestConnectionButton'
import { AudioProviderSettings } from '@/components/features/settings/AudioProviderSettings'
import { GpuSettings } from '@/components/features/settings/GpuSettings'
import { ImageUnderstandingSettings } from '@/components/features/settings/ImageUnderstandingSettings'
import { SystemMaintenanceSettings } from '@/components/features/settings/SystemMaintenanceSettings'

export function SettingsPage() {
  const [providers, setProviders] = useState<LLMProvider[]>([])
  const [activeLLM, setActiveLLMState] = useState<ActiveLLM>({ provider_id: 'none', model_id: '' })
  const [isLoading, setIsLoading] = useState(true)

  // GPU state
  const [gpuEnabled, setGpuEnabled] = useState(false)
  const [gpuStatus, setGpuStatus] = useState<GPUStatus | null>(null)
  const [isPollingGpu, setIsPollingGpu] = useState(false)

  // Multi-GPU worker scaling state
  const [gpuWorkers, setGpuWorkers] = useState<GPUWorkersResolved | null>(null)
  const [workerMode, setWorkerMode] = useState<GPUWorkerMode>('auto')
  const [workerCount, setWorkerCount] = useState<number>(1)
  const [isSavingWorkers, setIsSavingWorkers] = useState(false)

  // Image understanding defaults state
  const [vlmModel, setVlmModel] = useState<string>('')
  const [maxImagesPerDoc, setMaxImagesPerDoc] = useState<number>(50)
  const [isSavingImageSetting, setIsSavingImageSetting] = useState(false)
  // Last value persisted to the backend; used to skip no-op blur saves.
  const committedImageCapRef = useRef<number>(50)

  // Drawer & Modal state
  const [activeDrawer, setActiveDrawer] = useState<{
    type: 'keys' | 'models'
    providerId: string
  } | null>(null)
  const [showAddCustomModal, setShowAddCustomModal] = useState(false)

  // Add Custom Provider Form state
  const [customName, setCustomName] = useState('')
  const [customBaseUrl, setCustomBaseUrl] = useState('')
  const [customType, setCustomType] = useState('custom_openai')

  // Connection testing state (drawer specific)
  const [isTesting, setIsTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)

  // Model management state (drawer specific)
  const [isFetchingModels, setIsFetchingModels] = useState(false)
  const [fetchedModels, setFetchedModels] = useState<string[]>([])
  const [modelSearchQuery, setModelSearchQuery] = useState('')
  const [customModelId, setCustomModelId] = useState('')
  const [expandedModelSettings, setExpandedModelSettings] = useState<string | null>(null)

  // System maintenance states
  const [isSelfHealing, setIsSelfHealing] = useState(false)
  const [isResetting, setIsResetting] = useState(false)
  const [deleteUserDataCheck, setDeleteUserDataCheck] = useState(false)
  const [isConfirmingReset, setIsConfirmingReset] = useState(false)
  const [clickCoords, setClickCoords] = useState({ x: 0, y: 0 })
  const [transitionEnabled, setTransitionEnabled] = useState(false)
  const resetCardRef = useRef<HTMLDivElement>(null)

  const handleSelfHeal = async () => {
    setIsSelfHealing(true)
    toast.info('Verifying engine component files...')
    try {
      const res = await selfHealModels()
      if (res.success) {
        if (res.healed_count > 0) {
          toast.success(`Self-healing completed. Re-downloading ${res.healed_count} missing component(s).`)
          // Redirect the user to Onboarding Page by reloading the page
          setTimeout(() => {
            window.location.reload()
          }, 1500)
        } else {
          toast.success('Integrity check passed! All components are healthy.')
        }
      } else {
        toast.error(res.message || 'Self-healing failed')
      }
    } catch (err) {
      toast.error('Failed to run self-healing')
      console.error(err)
    } finally {
      setIsSelfHealing(false)
    }
  }

  const triggerResetConfirm = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (!resetCardRef.current) return
    const rect = resetCardRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    setTransitionEnabled(false)
    setClickCoords({ x, y })
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        setTransitionEnabled(true)
        setIsConfirmingReset(true)
      })
    })
  }

  const handleGoBack = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (!resetCardRef.current) return
    const rect = resetCardRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    setTransitionEnabled(true)
    setClickCoords({ x, y })
    setIsConfirmingReset(false)
  }

  const handleReset = async () => {
    setIsResetting(true)
    toast.info('Cleaning environment...')
    try {
      const res = await resetModels(deleteUserDataCheck)
      if (res.success) {
        toast.success(res.message || 'System reset completed.')
        setTimeout(() => {
          window.location.reload()
        }, 1500)
      } else {
        toast.error(res.message || 'System reset failed.')
      }
    } catch (err) {
      toast.error('Failed to reset system')
      console.error(err)
    } finally {
      setIsResetting(false)
    }
  }

  // Fetch initial data
  useEffect(() => {
    async function loadInitialData() {
      try {
        const provs = await getLLMProviders()
        const active = await getActiveLLM()
        setProviders(provs)
        setActiveLLMState(active)

        const settingsList = await getSettings()
        const gpuSetting = settingsList.find((s) => s.key === 'gpu_acceleration_enabled')
        setGpuEnabled(gpuSetting?.value === 'true')

        // Image understanding defaults
        const vlmSetting = settingsList.find((s) => s.key === 'vlm_model')
        setVlmModel(vlmSetting?.value ?? '')
        const capSetting = settingsList.find((s) => s.key === 'max_images_per_doc')
        const loadedCap = capSetting ? Number(capSetting.value) || 50 : 50
        setMaxImagesPerDoc(loadedCap)
        committedImageCapRef.current = loadedCap

        const status = await getGPUStatus()
        setGpuStatus(status)
        if (status.status === 'installing') {
          setIsPollingGpu(true)
        } else if (gpuSetting?.value === 'true' && (status.status === 'not_installed' || status.status === 'failed')) {
          try {
            const startStatus = await installGPU()
            setGpuStatus(startStatus)
            setIsPollingGpu(true)
          } catch (e) {
            console.error('Failed to auto-start GPU installation', e)
          }
        }

        // Multi-GPU worker scaling (best effort; degrades silently on CPU-only).
        try {
          const resolved = await getGPUWorkersResolved()
          setGpuWorkers(resolved)
          setWorkerMode(resolved.mode)
          setWorkerCount(Math.max(1, resolved.effective))
        } catch (e) {
          console.error('Failed to load GPU worker scaling state', e)
        }
      } catch (err) {
        console.error('Failed to load settings data', err)
        toast.error('Failed to load settings configuration')
      } finally {
        setIsLoading(false)
      }
    }
    loadInitialData()
  }, [])

  // GPU polling
  useEffect(() => {
    let timer: number
    if (isPollingGpu) {
      timer = window.setInterval(async () => {
        try {
          const status = await getGPUStatus()
          setGpuStatus(status)
          if (status.status !== 'installing') {
            setIsPollingGpu(false)
            if (status.status === 'ready') {
              toast.success('GPU Acceleration is ready!')
            } else if (status.status === 'failed') {
              toast.error('GPU Acceleration installation failed.')
            }
          }
        } catch (err) {
          console.error('Error polling GPU status', err)
        }
      }, 2000)
    }
    return () => clearInterval(timer)
  }, [isPollingGpu])

  const handleToggleGpu = async (checked: boolean) => {
    setGpuEnabled(checked)
    try {
      await toggleGPU(checked)
      toast.success(`GPU Acceleration ${checked ? 'enabled' : 'disabled'}`)
      if (checked && (!gpuStatus || gpuStatus.status === 'not_installed' || gpuStatus.status === 'failed')) {
        toast.info('Installation starting...')
        const newStatus = await installGPU()
        setGpuStatus(newStatus)
        setIsPollingGpu(true)
      }
    } catch (err) {
      console.error('Failed to toggle GPU:', err)
      toast.error('Failed to toggle GPU acceleration')
      setGpuEnabled(!checked)
    }
  }

  const handleSaveWorkerScaling = async () => {
    setIsSavingWorkers(true)
    try {
      const resolved = await setGPUWorkers(workerMode, workerMode === 'manual' ? workerCount : undefined)
      setGpuWorkers(resolved)
      setWorkerCount(Math.max(1, resolved.effective))
      toast.success('Worker scaling saved — restart the server to apply.')
    } catch (err) {
      console.error('Failed to save worker scaling:', err)
      toast.error('Failed to save worker scaling')
    } finally {
      setIsSavingWorkers(false)
    }
  }

  const handleSaveImageSetting = async (key: 'vlm_model' | 'max_images_per_doc', value: string) => {
    setIsSavingImageSetting(true)
    try {
      await updateSetting(key, value, 'image')
      if (key === 'max_images_per_doc') {
        committedImageCapRef.current = Number(value) || 50
      }
      toast.success('Image understanding default saved')
    } catch (err) {
      console.error('Failed to save image setting:', err)
      toast.error('Failed to save image understanding default')
    } finally {
      setIsSavingImageSetting(false)
    }
  }

  // Draft state for drawers to avoid mutating the main providers list directly
  const [draftProvider, setDraftProvider] = useState<LLMProvider | null>(null)

  const openDrawer = (type: 'keys' | 'models', providerId: string) => {
    const prov = providers.find((p) => p.id === providerId)
    if (prov) {
      setDraftProvider(JSON.parse(JSON.stringify(prov)))
      setActiveDrawer({ type, providerId })
    }
  }

  const closeDrawer = () => {
    setActiveDrawer(null)
    setDraftProvider(null)
  }

  const updateDraft = (updater: (draft: LLMProvider) => void) => {
    setDraftProvider((prev) => {
      if (!prev) return null
      const nextDraft = JSON.parse(JSON.stringify(prev))
      updater(nextDraft)
      return nextDraft
    })
  }

  // Save full providers configuration
  const handleSaveProviders = async (updatedProviders: LLMProvider[]) => {
    try {
      const saved = await saveLLMProviders(updatedProviders)
      setProviders(saved)
      toast.success('Configuration saved successfully')
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to save configuration'
      toast.error(msg)
    }
  }

  const handleSaveDrawer = async () => {
    if (!draftProvider) return
    const updated = providers.map((p) =>
      p.id === draftProvider.id ? draftProvider : p
    )
    await handleSaveProviders(updated)
    closeDrawer()
  }

  // Handle active LLM selection change
  const handleActiveChange = async (providerId: string, modelId: string) => {
    const active = { provider_id: providerId, model_id: modelId }
    try {
      await setActiveLLM(active)
      setActiveLLMState(active)
      toast.success('Global active LLM updated')
    } catch (err) {
      console.error('Failed to update active LLM:', err)
      toast.error('Failed to update active LLM')
    }
  }

  // Create custom OpenAI provider
  const handleAddCustomProvider = () => {
    if (!customName.trim()) {
      toast.error('Provider name is required')
      return
    }
    const cleanUrl = customBaseUrl.trim()
    if (!cleanUrl) {
      toast.error('Base URL is required')
      return
    }

    const providerId = 'custom-' + customName.toLowerCase().replace(/[^a-z0-9]/g, '-')
    if (providers.some((p) => p.id === providerId)) {
      toast.error('A provider with this name already exists')
      return
    }

    const newProvider: LLMProvider = {
      id: providerId,
      type: customType,
      label: customName.trim(),
      api_key: '',
      fallback_api_keys: [],
      base_url: cleanUrl,
      models: [],
    }

    const updated = [...providers, newProvider]
    setProviders(updated)
    void handleSaveProviders(updated)

    setCustomName('')
    setCustomBaseUrl('')
    setCustomType('custom_openai')
    setShowAddCustomModal(false)
    toast.success(`Custom provider "${newProvider.label}" added`)
  }

  // Delete custom provider
  const handleDeleteProvider = (providerId: string) => {
    const updated = providers.filter((p) => p.id !== providerId)
    setProviders(updated)
    void handleSaveProviders(updated)

    if (activeLLM.provider_id === providerId) {
      void handleActiveChange('none', '')
    }
    toast.success('Provider deleted')
  }

  // Test Connection helper inside credentials drawer
  const handleTestApiKey = async (key: string): Promise<{ success: boolean; message: string }> => {
    if (!draftProvider) return { success: false, message: 'No provider configured' }
    if (!key) {
      toast.error('API key is empty')
      return { success: false, message: 'API key is empty' }
    }
    setIsTesting(true)
    setTestResult(null)
    toast.info('Testing connection to provider...')
    
    try {
      // Map to legacy structure for the backend connection tester
      const backendConfig: BackendLLMConfig = {
        llm_service: 
          draftProvider.type === 'custom_openai' ? 'openai' : 
          draftProvider.type === 'custom_anthropic' ? 'claude' : 
          draftProvider.type,
        timeout: 15,
        max_retries: 2,
        max_output_tokens: 4096,
      }

      const baseUrl = draftProvider.base_url
      const model = draftProvider.models[0]?.model_id || 'test'

      if (draftProvider.type === 'gemini' || draftProvider.type === 'vertex') {
        backendConfig.gemini_api_key = key
        backendConfig.gemini_model_name = model
        if (draftProvider.type === 'vertex') {
          backendConfig.vertex_project_id = key
          backendConfig.vertex_location = baseUrl
        }
      } else if (draftProvider.type === 'claude' || draftProvider.type === 'custom_anthropic') {
        backendConfig.claude_api_key = key
        backendConfig.claude_model_name = model
        if (draftProvider.type === 'custom_anthropic') {
          backendConfig.openai_base_url = baseUrl
        }
      } else if (draftProvider.type === 'ollama') {
        backendConfig.ollama_base_url = baseUrl
        backendConfig.ollama_model = model
      } else if (draftProvider.type === 'azure') {
        backendConfig.azure_api_key = key
        backendConfig.azure_endpoint = baseUrl
        backendConfig.azure_deployment_name = model
      } else {
        // OpenAI / Custom OpenAI
        backendConfig.openai_api_key = key
        backendConfig.openai_base_url = baseUrl
        backendConfig.openai_model = model
      }

      // We call connection tester
      const res = await fetch('/api/settings/llm/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(backendConfig),
      })
      const data = await res.json() as { success?: boolean; message?: string; detail?: string }
      if (res.ok && data.success) {
        const result = { success: true, message: data.message || 'Connected successfully!' }
        setTestResult(result)
        toast.success('Connection test passed!')
        return result
      } else {
        const result = { success: false, message: data.detail || data.message || 'Connection failed.' }
        setTestResult(result)
        toast.error('Connection test failed.')
        return result
      }
    } catch (err) {
      const msg = messageFromUnknownError(err, 'Network error.')
      const result = { success: false, message: msg }
      setTestResult(result)
      toast.error('Connection test failed.')
      return result
    } finally {
      setIsTesting(false)
    }
  }

  // Fetch models from API inside drawer
  const handleFetchModels = async () => {
    if (!draftProvider) return
    setIsFetchingModels(true)
    setFetchedModels([])
    toast.info('Querying provider model list...')

    try {
      const list = await fetchAvailableModels(
        draftProvider.id,
        draftProvider.type,
        draftProvider.base_url,
        draftProvider.api_key
      )
      setFetchedModels(list)
      if (list.length === 0) {
        toast.info('No models returned by provider')
      } else {
        toast.success(`Successfully fetched ${list.length} models`)
      }
    } catch (err) {
      const msg = messageFromUnknownError(err, 'Failed to query models')
      toast.error(msg)
    } finally {
      setIsFetchingModels(false)
    }
  }

  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center py-32 space-y-4">
        <Loader2 className="w-8 h-8 text-primary animate-spin" />
        <p className="text-sm text-muted-foreground animate-pulse">Loading settings...</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col min-h-full">
      <PageHeader 
        title="Settings"
        description="Configure language model backends, API credentials, and parameter thresholds."
      >
        <Button
          onClick={() => setShowAddCustomModal(true)}
          className="shadow-sm rounded-lg hover:scale-[1.005] active:scale-[0.99] transition-all text-xs font-bold uppercase tracking-wider h-10 w-fit shrink-0 gap-1.5"
        >
          <ListPlus className="w-4 h-4" />
          Add Custom OpenAI Provider
        </Button>
      </PageHeader>

      <div className="max-w-[1400px] mx-auto space-y-8 pb-12 px-4 md:px-6 w-full relative">
        {/* Global LLM active banner */}
      <div className="glass-card border border-border/30 rounded-2xl p-5 shadow-sm space-y-4 bg-card/15 relative z-10">
        <div className="flex items-center gap-2 border-b border-border/20 pb-3">
          <Activity className="w-4.5 h-4.5 text-primary" />
          <h3 className="font-extrabold text-sm text-foreground uppercase tracking-wider">Global LLM Configuration</h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-5 items-end">
          <div className="space-y-1.5">
            <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Active Provider</label>
            <Select
              value={activeLLM.provider_id}
              onChange={(val) => {
                const pObj = providers.find((p) => p.id === val)
                const firstModel = pObj?.models[0]?.model_id || ''
                void handleActiveChange(val, firstModel)
              }}
              options={[
                { value: 'none', label: 'None (Disable LLMs)' },
                ...providers.map((p) => ({
                  value: p.id,
                  label: `${p.label} (${p.type.replace('_', ' ')})`
                }))
              ]}
              className="w-full md:w-full"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Active Model</label>
            <Select
              value={activeLLM.model_id}
              onChange={(val) => void handleActiveChange(activeLLM.provider_id, val)}
              disabled={activeLLM.provider_id === 'none'}
              options={[
                ...(activeLLM.model_id ? [] : [{ value: '', label: 'Select model...' }]),
                ...(providers
                  .find((p) => p.id === activeLLM.provider_id)
                  ?.models.map((m) => ({
                    value: m.model_id,
                    label: m.model_id
                  })) || [])
              ]}
              className="w-full md:w-full"
            />
          </div>
        </div>
      </div>

      {/* Provider List Cards Grid */}
      <div className="space-y-4">
        <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase pb-2 border-b border-border/20 flex items-center gap-2">
          <Sliders className="w-4 h-4 text-primary" />
          Configured Service Providers
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {providers.map((p) => {
            const isActive = activeLLM.provider_id === p.id
            const isConfigured = !!p.api_key || p.type === 'ollama'

            const handleCardClick = () => {
              setFetchedModels([])
              setModelSearchQuery('')
              setCustomModelId('')
              setExpandedModelSettings(null)
              openDrawer('models', p.id)
            }

            return (
              <div
                key={p.id}
                onClick={handleCardClick}
                className="border border-border/60 rounded-2xl p-4 flex items-center justify-between transition-all bg-card/45 hover:bg-card/75 cursor-pointer shadow-sm"
              >
                {/* Left Side: Checkmark, Icon, and Title */}
                <div className="flex items-center gap-3">
                  {isActive && (
                    <div className="w-5 h-5 rounded-full bg-emerald-500 flex items-center justify-center shrink-0 shadow-sm shadow-emerald-500/20">
                      <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                      </svg>
                    </div>
                  )}
                  <div className="flex flex-col">
                    <span className="font-bold text-[15px] text-foreground leading-tight">
                      {p.label === 'Claude' ? 'Anthropic' : p.label}
                    </span>
                    <span className="text-[12px] text-muted-foreground font-medium mt-0.5 leading-none">
                      {p.models.length} {p.models.length === 1 ? 'model' : 'models'}
                    </span>
                  </div>
                </div>

                {/* Right Side: Key, Delete, Chevron */}
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      setTestResult(null)
                      openDrawer('keys', p.id)
                    }}
                    className={cn(
                      'p-2 rounded-lg transition-colors',
                      isConfigured
                        ? 'text-emerald-500 hover:bg-emerald-500/10'
                        : 'text-muted-foreground hover:bg-white/5'
                    )}
                    title="Credentials"
                  >
                    <Key className="w-4.5 h-4.5" />
                  </button>

                  {(p.type === 'custom_openai' || p.type === 'custom_anthropic') && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDeleteProvider(p.id)
                      }}
                      className="p-2 rounded-lg hover:bg-rose-500/10 text-muted-foreground hover:text-rose-500 transition-colors"
                      title="Delete provider"
                    >
                      <Trash2 className="w-4.5 h-4.5" />
                    </button>
                  )}

                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      handleCardClick()
                    }}
                    aria-label={`Models (${p.models.length})`}
                    className="p-2 rounded-lg text-muted-foreground hover:bg-white/5 hover:text-foreground transition-colors"
                  >
                    <ChevronRight className="w-4.5 h-4.5" />
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <AudioProviderSettings />

      <GpuSettings
        gpuEnabled={gpuEnabled}
        gpuStatus={gpuStatus}
        isPollingGpu={isPollingGpu}
        onToggleGpu={handleToggleGpu}
        gpuWorkers={gpuWorkers}
        workerMode={workerMode}
        workerCount={workerCount}
        isSavingWorkers={isSavingWorkers}
        onWorkerModeChange={setWorkerMode}
        onWorkerCountChange={setWorkerCount}
        onSaveWorkerScaling={handleSaveWorkerScaling}
      />

      <ImageUnderstandingSettings
        providers={providers}
        vlmModel={vlmModel}
        maxImagesPerDoc={maxImagesPerDoc}
        isSavingImageSetting={isSavingImageSetting}
        onVlmModelChange={(val) => {
          setVlmModel(val)
          handleSaveImageSetting('vlm_model', val)
        }}
        onImageCapChange={setMaxImagesPerDoc}
        onImageCapCommit={(val) => {
          if (val !== committedImageCapRef.current) {
            handleSaveImageSetting('max_images_per_doc', String(val))
          }
        }}
      />

      <SystemMaintenanceSettings
        isSelfHealing={isSelfHealing}
        isResetting={isResetting}
        deleteUserDataCheck={deleteUserDataCheck}
        isConfirmingReset={isConfirmingReset}
        clickCoords={clickCoords}
        transitionEnabled={transitionEnabled}
        resetCardRef={resetCardRef}
        onSelfHeal={handleSelfHeal}
        onDeleteUserDataChange={setDeleteUserDataCheck}
        onConfirmResetStart={triggerResetConfirm}
        onResetBack={handleGoBack}
        onReset={handleReset}
      />

      {/* Slide-over Drawer for API Keys & Credentials */}
      {activeDrawer && activeDrawer.type === 'keys' && draftProvider && (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm transition-opacity duration-300">
          {/* Backdrop Dismiss Click */}
          <div className="absolute inset-0" onClick={closeDrawer} />

          {/* Panel */}
          <div className="relative w-full max-w-md bg-background border-l border-border/60 shadow-2xl h-full flex flex-col text-left z-10 animate-slide-in">
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-5 border-b border-border/20">
              <div className="flex items-center gap-2.5">
                <Key className="w-5 h-5 text-primary" />
                <div>
                  <h3 className="font-extrabold text-sm text-foreground uppercase tracking-wider">
                    {draftProvider.label} Credentials
                  </h3>
                  <p className="text-xs text-muted-foreground mt-0.5">Manage API endpoints, keys, and fallbacks.</p>
                </div>
              </div>
              <button
                type="button"
                onClick={closeDrawer}
                className="p-1 rounded-md text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-6 space-y-5">
              {/* Base URL (if applicable) */}
              {draftProvider.type !== 'gemini' && draftProvider.type !== 'claude' && (
                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-1.5">
                    <Globe className="w-3.5 h-3.5 text-muted-foreground" />
                    {draftProvider.type === 'azure' ? 'Azure Endpoint URL' : 'API Base URL'}
                  </label>
                  <Input
                    value={draftProvider.base_url || ''}
                    onChange={(e) => {
                      updateDraft((draft) => {
                        draft.base_url = e.target.value
                      })
                    }}
                    placeholder={
                      draftProvider.type === 'azure'
                        ? 'https://your-resource.openai.azure.com'
                        : draftProvider.type === 'ollama'
                        ? 'http://localhost:11434'
                        : 'https://api.example.com/v1'
                    }
                    className="bg-background/50 text-xs"
                  />
                </div>
              )}              {/* Primary API Key */}
              {draftProvider.type !== 'ollama' && (
                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-1.5">
                    <Key className="w-3.5 h-3.5 text-muted-foreground" />
                    {draftProvider.type === 'vertex' ? 'Google Cloud Project ID' : 'Primary API Key'}
                  </label>
                  <div className="flex gap-2 items-center">
                    <Input
                      type="password"
                      value={draftProvider.api_key || ''}
                      onChange={(e) => {
                        updateDraft((draft) => {
                          draft.api_key = e.target.value
                        })
                      }}
                      placeholder={
                        draftProvider.type === 'vertex'
                          ? 'e.g., my-gcp-project-123'
                          : 'Enter credentials token'
                      }
                      className="bg-background/50 text-xs flex-1"
                    />
                    <TestConnectionButton
                      apiKey={draftProvider.api_key || ''}
                      onTest={handleTestApiKey}
                      disabled={isTesting || draftProvider.type === 'vertex'}
                    />
                  </div>
                </div>
              )}

              {/* Fallback API Keys (up to 5) */}
              {draftProvider.type !== 'ollama' && draftProvider.type !== 'vertex' && (
                <div className="space-y-3 pt-2 border-t border-border/10">
                  <div className="flex items-center justify-between">
                    <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-1.5">
                      <ListPlus className="w-3.5 h-3.5 text-muted-foreground" />
                      Fallback API Keys ({draftProvider.fallback_api_keys.length}/5)
                    </label>
                    {draftProvider.fallback_api_keys.length < 5 && (
                      <button
                        type="button"
                        onClick={() => {
                          updateDraft((draft) => {
                            draft.fallback_api_keys.push('')
                          })
                        }}
                        className="text-xs font-bold text-primary hover:underline uppercase flex items-center gap-1"
                      >
                        <Plus className="w-3 h-3" /> Add
                      </button>
                    )}
                  </div>

                  <div className="space-y-2">
                    {draftProvider.fallback_api_keys.map((keyVal, idx) => (
                      <div key={idx} className="flex gap-2 items-center">
                        <span className="text-xs font-bold font-mono text-muted-foreground shrink-0 w-4">#{idx + 1}</span>
                        <Input
                          type="password"
                          value={keyVal}
                          onChange={(e) => {
                            updateDraft((draft) => {
                              draft.fallback_api_keys[idx] = e.target.value
                            })
                          }}
                          placeholder={`Fallback key ${idx + 1}`}
                          className="bg-background/50 text-xs flex-1"
                        />
                        <TestConnectionButton
                          apiKey={keyVal}
                          onTest={handleTestApiKey}
                          disabled={isTesting}
                        />
                        <button
                          type="button"
                          onClick={() => {
                            updateDraft((draft) => {
                              draft.fallback_api_keys = draft.fallback_api_keys.filter((_, i) => i !== idx)
                            })
                          }}
                          className="p-2 text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 rounded-lg transition-colors shrink-0"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ))}
                    {draftProvider.fallback_api_keys.length === 0 && (
                      <div className="text-xs text-muted-foreground/60 italic py-2 text-center">
                        No fallback API keys configured.
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Concurrency cap */}
              <div className="space-y-2 pt-2 border-t border-border/10">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-1.5">
                  <Activity className="w-3.5 h-3.5 text-muted-foreground" />
                  Max Concurrent API Calls
                </label>
                <Input
                  type="number"
                  min={1}
                  value={draftProvider.concurrency ?? ''}
                  onChange={(e) => {
                    const raw = e.target.value.trim()
                    const n = parseInt(raw, 10)
                    updateDraft((draft) => {
                      draft.concurrency = raw === '' || isNaN(n) || n < 1 ? null : n
                    })
                  }}
                  placeholder="Unlimited"
                  className="bg-background/50 text-xs"
                />
                <p className="text-xs text-muted-foreground/60 leading-normal">
                  Caps simultaneous in-flight requests to this provider across all jobs. Lower it if the provider returns 504 / DEADLINE_EXCEEDED under parallel load. Leave blank for unlimited.
                </p>
              </div>

              {/* Test connection result */}
              {testResult && (
                <div
                  className={cn(
                    'p-4 rounded-xl border flex items-start gap-3 mt-4 text-left',
                    testResult.success
                      ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-800 dark:text-emerald-300'
                      : 'bg-destructive/10 border-destructive/30 text-destructive'
                  )}
                >
                  {testResult.success ? (
                    <CheckCircle2 className="w-5 h-5 shrink-0 text-emerald-500 mt-0.5" />
                  ) : (
                    <AlertTriangle className="w-5 h-5 shrink-0 text-destructive mt-0.5" />
                  )}
                  <div>
                    <div className="font-bold text-xs">{testResult.success ? 'Connection Successful' : 'Connection Failed'}</div>
                    <div className="text-xs mt-1 leading-normal opacity-90 font-mono whitespace-pre-wrap">{testResult.message}</div>
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end px-6 py-4 border-t border-border/20 bg-muted/10">
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  onClick={closeDrawer}
                  className="text-xs font-bold uppercase tracking-wider px-4 rounded-lg h-10"
                >
                  Cancel
                </Button>
                <Button
                  onClick={handleSaveDrawer}
                  className="text-xs font-bold uppercase tracking-wider px-5 rounded-lg shadow-sm h-10 gap-1.5"
                >
                  <Save className="w-4 h-4" />
                  Save Settings
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Slide-over Drawer for Model Management */}
      {activeDrawer && activeDrawer.type === 'models' && draftProvider && (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm transition-opacity duration-300">
          <div className="absolute inset-0" onClick={closeDrawer} />

          <div className="relative w-full max-w-md bg-background border-l border-border/60 shadow-2xl h-full flex flex-col text-left z-10 animate-slide-in">
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-5 border-b border-border/20">
              <div className="flex items-center gap-2.5">
                <Settings className="w-5 h-5 text-primary" />
                <div>
                  <h3 className="font-extrabold text-sm text-foreground uppercase tracking-wider">
                    {draftProvider.label} Models
                  </h3>
                  <p className="text-xs text-muted-foreground mt-0.5">Add, query, and edit model threshold parameters.</p>
                </div>
              </div>
              <button
                type="button"
                onClick={closeDrawer}
                className="p-1 rounded-md text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-6 space-y-6">
              {/* Fetch models action */}
              {draftProvider.type !== 'vertex' && (
                <div className="bg-card/40 border border-border/40 p-4 rounded-xl space-y-3.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-extrabold tracking-widest text-muted-foreground uppercase">Query API Models List</span>
                    <Button
                      size="sm"
                      onClick={handleFetchModels}
                      disabled={isFetchingModels}
                      className="h-8 text-xs font-bold uppercase tracking-wider rounded-lg px-3 gap-1.5"
                    >
                      {isFetchingModels && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                      Fetch Models
                    </Button>
                  </div>

                  {fetchedModels.length > 0 && (
                    <div className="space-y-2 animate-fade-in">
                      <Input
                        value={modelSearchQuery}
                        onChange={(e) => setModelSearchQuery(e.target.value)}
                        placeholder="Search returned models..."
                        className="bg-background text-xs"
                      />
                      <div className="max-h-40 overflow-y-auto border border-border/20 bg-background/50 rounded-lg divide-y divide-border/10 scrollbar-thin">
                        {fetchedModels
                          .filter((m) => m.toLowerCase().includes(modelSearchQuery.toLowerCase()))
                          .map((modelId) => {
                            const alreadyAdded = draftProvider.models.some((m) => m.model_id === modelId)
                            return (
                              <div key={modelId} className="flex items-center justify-between p-2 text-xs">
                                <span className="font-mono font-semibold truncate max-w-[75%]" title={modelId}>
                                  {modelId}
                                </span>
                                {alreadyAdded ? (
                                  <Badge variant="secondary" className="text-xs uppercase font-bold tracking-wider opacity-60">Added</Badge>
                                ) : (
                                  <button
                                    type="button"
                                    onClick={() => {
                                      const newModel: ModelConfig = { model_id: modelId }
                                      updateDraft((draft) => {
                                        draft.models.push(newModel)
                                      })
                                      toast.success(`Model "${modelId}" added`)
                                    }}
                                    className="text-xs font-bold text-primary hover:underline uppercase"
                                  >
                                    Add
                                  </button>
                                )}
                              </div>
                            )
                          })}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Add Custom Model ID */}
              <div className="space-y-2">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase block">Add Custom Model ID</label>
                <div className="flex gap-2">
                  <Input
                    value={customModelId}
                    onChange={(e) => setCustomModelId(e.target.value)}
                    placeholder="e.g. gpt-4o-2024-05-13"
                    className="bg-background/50 text-xs flex-1"
                  />
                  <Button
                    type="button"
                    onClick={() => {
                      const cleanId = customModelId.trim()
                      if (!cleanId) return
                      if (draftProvider.models.some((m) => m.model_id === cleanId)) {
                        toast.error('Model ID already exists')
                        return
                      }
                      const newModel: ModelConfig = { model_id: cleanId }
                      updateDraft((draft) => {
                        draft.models.push(newModel)
                      })
                      setCustomModelId('')
                      toast.success(`Model "${cleanId}" added`)
                    }}
                    className="text-xs font-bold uppercase tracking-wider h-9.5 rounded-lg px-4 gap-1"
                  >
                    <Plus className="w-3.5 h-3.5" /> Add
                  </Button>
                </div>
              </div>

              {/* Configured Models List */}
              <div className="space-y-3 pt-4 border-t border-border/15">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase block">
                  Configured Models ({draftProvider.models.length})
                </label>

                <div className="space-y-2">
                  {draftProvider.models.map((model, mIdx) => {
                    const isExpanded = expandedModelSettings === model.model_id
                    return (
                      <div
                        key={model.model_id}
                        className="border border-border/40 rounded-xl overflow-hidden bg-card/15 shadow-sm"
                      >
                        {/* Header bar */}
                        <div className="flex items-center justify-between p-3.5 bg-muted/20 select-none">
                          <button
                            type="button"
                            onClick={() => setExpandedModelSettings(isExpanded ? null : model.model_id)}
                            className="flex items-center gap-2 truncate text-left flex-1"
                          >
                            <span className="font-semibold text-xs font-mono truncate text-foreground flex-1">
                              {model.model_id}
                            </span>
                            {isExpanded ? (
                              <ChevronUp className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                            ) : (
                              <ChevronDown className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                            )}
                          </button>

                          <div className="flex items-center gap-1.5 ml-2 shrink-0">
                            <span className="text-xs font-bold uppercase tracking-wider text-muted-foreground/80">Vision</span>
                            <button
                              type="button"
                              role="switch"
                              aria-checked={model.vision_capable ?? false}
                              aria-label={`Vision capability for ${model.model_id}`}
                              title="Mark this model as vision-capable (multimodal)"
                              onClick={() => {
                                updateDraft((draft) => {
                                  const m = draft.models[mIdx]
                                  if (m) m.vision_capable = !(m.vision_capable ?? false)
                                })
                              }}
                              className={cn(
                                'relative inline-flex h-4 w-7 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                                model.vision_capable ? 'bg-primary' : 'bg-muted border-border/20'
                              )}
                            >
                              <span
                                className={cn(
                                  'pointer-events-none inline-block h-3 w-3 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
                                  model.vision_capable ? 'translate-x-3.5' : 'translate-x-0'
                                )}
                              />
                            </button>
                          </div>

                          <button
                            type="button"
                            onClick={() => {
                              updateDraft((draft) => {
                                draft.models = draft.models.filter((_, i) => i !== mIdx)
                              })
                              toast.info(`Model "${model.model_id}" removed`)
                              if (isExpanded) setExpandedModelSettings(null)
                            }}
                            className="p-1.5 ml-2 text-muted-foreground hover:text-rose-500 rounded-lg hover:bg-rose-500/10 transition-colors"
                          >
                            <Trash className="w-3.5 h-3.5" />
                          </button>
                        </div>

                        {/* Expandable settings panel */}
                        {isExpanded && (
                          <div className="p-4 border-t border-border/10 bg-background/30 space-y-4 text-xs animate-fade-in text-left">
                            <h4 className="text-xs font-bold tracking-wider text-muted-foreground uppercase pb-1 border-b border-border/10">
                              Threshold Parameters Override
                            </h4>

                            <div className="grid grid-cols-2 gap-3.5">
                              {/* Timeout */}
                              <div className="space-y-1">
                                <label className="text-xs font-semibold text-muted-foreground uppercase">Timeout (s)</label>
                                <Input
                                  type="number"
                                  value={model.timeout ?? ''}
                                  onChange={(e) => {
                                    const val = e.target.value ? Number(e.target.value) : undefined
                                    updateDraft((draft) => {
                                      const m = draft.models[mIdx]; if (m) m.timeout = val
                                    })
                                  }}
                                  placeholder="Default (60s)"
                                  className="text-xs bg-background/50"
                                />
                              </div>

                              {/* Max Retries */}
                              <div className="space-y-1">
                                <label className="text-xs font-semibold text-muted-foreground uppercase">Max Retries</label>
                                <Input
                                  type="number"
                                  value={model.max_retries ?? ''}
                                  onChange={(e) => {
                                    const val = e.target.value ? Number(e.target.value) : undefined
                                    updateDraft((draft) => {
                                      const m = draft.models[mIdx]; if (m) m.max_retries = val
                                    })
                                  }}
                                  placeholder="Default (3)"
                                  className="text-xs bg-background/50"
                                />
                              </div>

                              {/* Max Output Tokens */}
                              <div className="space-y-1">
                                <label className="text-xs font-semibold text-muted-foreground uppercase">Max Output Tokens</label>
                                <Input
                                  type="number"
                                  value={model.max_output_tokens ?? ''}
                                  onChange={(e) => {
                                    const val = e.target.value ? Number(e.target.value) : undefined
                                    updateDraft((draft) => {
                                      const m = draft.models[mIdx]; if (m) m.max_output_tokens = val
                                    })
                                  }}
                                  placeholder="Default (4096)"
                                  className="text-xs bg-background/50"
                                />
                              </div>

                              {/* Context Window */}
                              <div className="space-y-1">
                                <label className="text-xs font-semibold text-muted-foreground uppercase">Context Window</label>
                                <Input
                                  type="number"
                                  value={model.context_window ?? ''}
                                  onChange={(e) => {
                                    const val = e.target.value ? Number(e.target.value) : undefined
                                    updateDraft((draft) => {
                                      const m = draft.models[mIdx]; if (m) m.context_window = val
                                    })
                                  }}
                                  placeholder="Intelligent auto"
                                  className="text-xs bg-background/50"
                                />
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                  {draftProvider.models.length === 0 && (
                    <div className="text-xs text-muted-foreground/50 italic py-6 text-center border border-dashed border-border/30 rounded-xl bg-card/5">
                      No models configured. Add one above.
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-border/20 bg-muted/10">
              <Button
                variant="ghost"
                onClick={closeDrawer}
                className="text-xs font-bold uppercase tracking-wider px-4 rounded-lg h-10"
              >
                Cancel
              </Button>
              <Button
                onClick={handleSaveDrawer}
                className="text-xs font-bold uppercase tracking-wider px-5 rounded-lg shadow-sm h-10 gap-1.5"
              >
                <Save className="w-4 h-4" />
                Save Models
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Add Custom Provider Modal Popup */}
      {showAddCustomModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm transition-opacity duration-300">
          <div className="absolute inset-0" onClick={() => setShowAddCustomModal(false)} />

          <div className="relative glass-card max-w-md w-full bg-background border border-border/50 rounded-2xl shadow-xl overflow-hidden animate-modal-zoom-in text-left z-10 flex flex-col">
            <div className="flex items-center justify-between px-6 py-4 border-b border-border/20">
              <div className="flex items-center gap-2">
                <ListPlus className="w-4.5 h-4.5 text-primary" />
                <h3 className="font-extrabold text-sm text-foreground uppercase tracking-wider">Add Custom Provider</h3>
              </div>
              <button
                type="button"
                onClick={() => setShowAddCustomModal(false)}
                className="p-1 rounded-md text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-6 space-y-4">
              <div className="space-y-1.5">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Provider Name</label>
                <Input
                  value={customName}
                  onChange={(e) => setCustomName(e.target.value)}
                  placeholder="e.g. DeepSeek"
                  className="bg-background/50 text-xs"
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">API Protocol</label>
                <Select
                  value={customType}
                  onChange={(val) => setCustomType(val)}
                  options={[
                    { value: 'custom_openai', label: 'OpenAI Compatible (Chat Completions)' },
                    { value: 'custom_anthropic', label: 'Anthropic Compatible (Messages)' }
                  ]}
                  className="w-full md:w-full"
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Base Endpoint URL</label>
                <Input
                  value={customBaseUrl}
                  onChange={(e) => setCustomBaseUrl(e.target.value)}
                  placeholder="e.g. https://api.deepseek.com/v1"
                  className="bg-background/50 text-xs"
                />
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-border/20 bg-muted/10">
              <Button
                variant="ghost"
                onClick={() => setShowAddCustomModal(false)}
                className="text-xs font-bold uppercase tracking-wider px-4 rounded-lg h-10"
              >
                Cancel
              </Button>
              <Button
                onClick={handleAddCustomProvider}
                className="text-xs font-bold uppercase tracking-wider px-5 rounded-lg shadow-sm h-10 gap-1"
              >
                <Plus className="w-3.5 h-3.5" />
                Add Provider
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  </div>
  )
}
