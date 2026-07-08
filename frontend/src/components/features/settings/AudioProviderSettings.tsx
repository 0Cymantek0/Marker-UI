import { useEffect, useState } from 'react'
import { Activity, AlertTriangle, Loader2, Plus, Save, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Select } from '@/components/ui/select'
import {
  getAudioCapabilities,
  getAudioProviders,
  saveAudioProviders,
  getActiveAudioProvider,
  setActiveAudioProvider,
  type ActiveAudioProvider,
  type AudioProviderCapability,
  type AudioProviderConfig,
} from '@/lib/api'
import { cn } from '@/lib/utils'

export function AudioProviderSettings() {
  const [audioProviders, setAudioProviders] = useState<AudioProviderConfig[]>([])
  const [audioCapabilities, setAudioCapabilities] = useState<AudioProviderCapability[]>([])
  const [activeAudio, setActiveAudioState] = useState<ActiveAudioProvider>({
    provider_id: 'local_faster_whisper',
    model_id: '',
  })
  const [newAudioProviderType, setNewAudioProviderType] = useState('openai')
  const [isSavingAudioProviders, setIsSavingAudioProviders] = useState(false)

  useEffect(() => {
    async function loadAudioSettings() {
      try {
        const [audioCaps, configuredAudioProviders, audioActive] = await Promise.all([
          getAudioCapabilities(),
          getAudioProviders(),
          getActiveAudioProvider(),
        ])
        setAudioCapabilities(audioCaps)
        setAudioProviders(configuredAudioProviders)
        setActiveAudioState(audioActive)
        const firstCloudCap = audioCaps.find((cap) => cap.cloud)
        if (firstCloudCap) setNewAudioProviderType(firstCloudCap.provider_id)
      } catch (e) {
        console.error('Failed to load audio provider configuration', e)
      }
    }

    void loadAudioSettings()
  }, [])

  const updateAudioProvider = (providerId: string, updater: (provider: AudioProviderConfig) => AudioProviderConfig) => {
    setAudioProviders((current) =>
      current.map((provider) => provider.id === providerId ? updater(provider) : provider)
    )
  }

  const handleActiveAudioChange = async (providerId: string, modelId: string) => {
    const active = { provider_id: providerId, model_id: modelId }
    try {
      const saved = await setActiveAudioProvider(active)
      setActiveAudioState(saved)
      toast.success('Active audio provider updated')
    } catch (err) {
      console.error('Failed to update active audio provider:', err)
      toast.error('Failed to update active audio provider')
    }
  }

  const handleAddAudioProvider = () => {
    const cap = audioCapabilities.find((item) => item.provider_id === newAudioProviderType)
    if (!cap) {
      toast.error('Choose an audio provider type')
      return
    }

    const baseId = cap.provider_id
    let nextId: string = baseId
    let suffix = 2
    while (audioProviders.some((provider) => provider.id === nextId)) {
      nextId = `${baseId}-${suffix}`
      suffix += 1
    }

    const nextProvider: AudioProviderConfig = {
      id: nextId,
      type: cap.provider_id,
      label: cap.provider_label,
      api_key: '',
      base_url: '',
      region: '',
      deployment: '',
      concurrency: null,
      timeout: null,
      max_retries: null,
      default_model: cap.default_model || '',
      models: cap.default_model ? [cap.default_model] : [],
      enabled: true,
      cloud: cap.cloud,
    }
    setAudioProviders((current) => [...current, nextProvider])
  }

  const handleDeleteAudioProvider = (providerId: string) => {
    setAudioProviders((current) => current.filter((provider) => provider.id !== providerId))
    if (activeAudio.provider_id === providerId) {
      void handleActiveAudioChange('local_faster_whisper', '')
    }
  }

  const handleSaveAudioProviders = async () => {
    setIsSavingAudioProviders(true)
    try {
      const saved = await saveAudioProviders(audioProviders)
      setAudioProviders(saved)
      toast.success('Audio providers saved')
    } catch (err) {
      console.error('Failed to save audio providers:', err)
      toast.error('Failed to save audio providers')
    } finally {
      setIsSavingAudioProviders(false)
    }
  }

  const activeModelOptions = [
    { value: '', label: 'Provider default' },
    ...(
      activeAudio.provider_id === 'local_faster_whisper'
        ? [audioCapabilities.find((cap) => cap.provider_id === 'local_faster_whisper')?.default_model]
        : audioProviders.find((provider) => provider.id === activeAudio.provider_id)?.models
    )
      ?.filter((model): model is string => Boolean(model))
      .map((model) => ({ value: model, label: model })) ?? [],
  ]
  const capabilityById = new Map<string, AudioProviderCapability>(
    audioCapabilities.map((cap) => [cap.provider_id, cap])
  )
  const activeProviderOptions = [
    { value: 'local_faster_whisper', label: 'Local Faster Whisper' },
    ...audioProviders
      .filter((provider) => {
        if (!provider.enabled) return false
        const cap = capabilityById.get(String(provider.type))
        return !!cap && cap.available !== false && (cap.implementation_state ?? 'implemented') === 'implemented'
      })
      .map((provider) => ({
        value: provider.id,
        label: `${provider.label} (${provider.type})`,
      })),
  ]
  const activeProviderIsSelectable = activeProviderOptions.some((option) => option.value === activeAudio.provider_id)

  return (
    <div className="space-y-4 pt-6 border-t border-border/20">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="space-y-1">
          <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-2">
            <Activity className="w-4 h-4 text-primary" />
            Audio STT Providers
          </h3>
          <p className="text-xs text-muted-foreground leading-relaxed max-w-3xl">
            Configure speech-to-text providers used by audio conversion jobs.
          </p>
        </div>

        <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
          <Select
            value={newAudioProviderType}
            onChange={setNewAudioProviderType}
            options={audioCapabilities.map((cap) => ({
              value: cap.provider_id,
              label: `${cap.provider_label}${cap.cloud ? ' (cloud)' : ' (local)'}`,
            }))}
            disabled={audioCapabilities.length === 0}
            className="w-full sm:w-64"
          />
          <Button
            type="button"
            onClick={handleAddAudioProvider}
            disabled={audioCapabilities.length === 0}
            className="text-xs font-bold uppercase tracking-wider h-10 rounded-lg gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" />
            Add Audio Provider
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Active Audio Provider</label>
          <Select
            value={activeProviderIsSelectable ? activeAudio.provider_id : 'local_faster_whisper'}
            onChange={(val) => {
              const provider = audioProviders.find((item) => item.id === val)
              const firstModel = provider?.default_model || provider?.models[0] || ''
              void handleActiveAudioChange(val, firstModel)
            }}
            options={activeProviderOptions}
            className="w-full md:w-full"
          />
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Active Audio Model</label>
          <Select
            value={activeAudio.model_id}
            onChange={(val) => void handleActiveAudioChange(activeAudio.provider_id, val)}
            options={activeModelOptions}
            className="w-full md:w-full"
          />
        </div>
      </div>

      {!activeProviderIsSelectable && activeAudio.provider_id !== 'local_faster_whisper' && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            Saved active audio provider "{activeAudio.provider_id}" is not selectable because its adapter is not shipped or it is disabled.
            Choose a shipped provider before running audio conversions.
          </span>
        </div>
      )}

      {audioProviders.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border/40 bg-card/20 p-5 text-xs text-muted-foreground">
          No cloud audio providers configured. Local Faster Whisper remains available.
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {audioProviders.map((provider) => (
            <div key={provider.id} className="border border-border/50 rounded-xl bg-card/35 p-4 space-y-4 shadow-sm">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <Input
                    aria-label={`${provider.label} audio provider label`}
                    value={provider.label}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({ ...current, label: e.target.value }))}
                    className="h-9 bg-background/50 text-sm font-bold"
                  />
                  <div className="flex items-center gap-2 mt-2">
                    <Badge variant={provider.cloud ? 'warning' : 'secondary'} className="text-xs uppercase font-bold tracking-wider">
                      {provider.cloud ? 'Cloud' : 'Local'}
                    </Badge>
                    <span className="text-xs text-muted-foreground font-mono truncate">{provider.type}</span>
                  </div>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={provider.enabled}
                    aria-label={`${provider.label} audio provider enabled`}
                    onClick={() => updateAudioProvider(provider.id, (current) => ({ ...current, enabled: !current.enabled }))}
                    className={cn(
                      'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                      provider.enabled ? 'bg-primary' : 'bg-muted border-border/20'
                    )}
                  >
                    <span
                      className={cn(
                        'pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
                        provider.enabled ? 'translate-x-4' : 'translate-x-0'
                      )}
                    />
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDeleteAudioProvider(provider.id)}
                    className="p-2 rounded-lg hover:bg-rose-500/10 text-muted-foreground hover:text-rose-500 transition-colors"
                    title="Delete audio provider"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">API Key</label>
                  <Input
                    type="password"
                    value={provider.api_key ?? ''}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({ ...current, api_key: e.target.value }))}
                    placeholder={provider.cloud ? 'Required for cloud STT' : 'Optional'}
                    className="bg-background/50 text-xs"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Base URL</label>
                  <Input
                    value={provider.base_url ?? ''}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({ ...current, base_url: e.target.value }))}
                    placeholder="Provider endpoint"
                    className="bg-background/50 text-xs"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Default Model</label>
                  <Input
                    value={provider.default_model ?? ''}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({ ...current, default_model: e.target.value }))}
                    placeholder="Provider default"
                    className="bg-background/50 text-xs"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Models</label>
                  <Input
                    value={provider.models.join(', ')}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({
                      ...current,
                      models: e.target.value
                        .split(',')
                        .map((model) => model.trim())
                        .filter(Boolean),
                    }))}
                    placeholder="model-a, model-b"
                    className="bg-background/50 text-xs"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Region</label>
                  <Input
                    value={provider.region ?? ''}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({ ...current, region: e.target.value }))}
                    placeholder="Optional"
                    className="bg-background/50 text-xs"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Deployment</label>
                  <Input
                    value={provider.deployment ?? ''}
                    onChange={(e) => updateAudioProvider(provider.id, (current) => ({ ...current, deployment: e.target.value }))}
                    placeholder="Optional"
                    className="bg-background/50 text-xs"
                  />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="flex justify-end">
        <Button
          type="button"
          onClick={handleSaveAudioProviders}
          disabled={isSavingAudioProviders}
          className="text-xs font-bold uppercase tracking-wider h-10 rounded-lg gap-1.5"
        >
          {isSavingAudioProviders ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Save Audio Providers
        </Button>
      </div>
    </div>
  )
}
