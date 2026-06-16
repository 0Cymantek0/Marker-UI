import { useState, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, X, Repeat, Activity, Loader2, Zap } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, type SelectOption } from '@/components/ui/select'
import { getLLMProviders, applyLiveOverride, type LLMProvider } from '@/lib/api'
import { cn } from '@/lib/utils'
import { toast } from 'sonner'

interface ModelSwapDialogProps {
  open: boolean
  /** Whether the dialog was auto-surfaced by rate-limit detection (vs manual). */
  auto: boolean
  filename: string
  providerId?: string
  currentModel?: string
  onClose: () => void
  /** Called after a swap/concurrency change is applied successfully. */
  onApplied: () => void
}

export function ModelSwapDialog({
  open,
  auto,
  filename,
  providerId,
  currentModel,
  onClose,
  onApplied,
}: ModelSwapDialogProps) {
  const [provider, setProvider] = useState<LLMProvider | null>(null)
  const [loading, setLoading] = useState(false)
  const [applying, setApplying] = useState(false)
  const [selectedModel, setSelectedModel] = useState(currentModel ?? '')
  const [concurrency, setConcurrency] = useState<string>('')

  // Load the provider record so we can offer its sibling models + show the
  // current concurrency cap. Same-provider only: a swap reuses the running
  // client's host/auth, so cross-provider isn't offered here.
  useEffect(() => {
    if (!open || !providerId) return
    let active = true
    setLoading(true)
    getLLMProviders()
      .then((providers) => {
        if (!active) return
        const p = providers.find((x) => x.id === providerId) ?? null
        setProvider(p)
        if (p?.concurrency != null) setConcurrency(String(p.concurrency))
      })
      .catch(() => {
        if (active) toast.error('Could not load provider models')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [open, providerId])

  useEffect(() => {
    setSelectedModel(currentModel ?? '')
  }, [currentModel, open])

  const modelOptions: SelectOption[] = useMemo(
    () => (provider?.models ?? []).map((m) => ({ value: m.model_id, label: m.model_id })),
    [provider]
  )

  const modelChanged = selectedModel && selectedModel !== currentModel
  const parsedConcurrency = concurrency.trim() === '' ? null : parseInt(concurrency, 10)
  const concurrencyChanged =
    parsedConcurrency != null &&
    !isNaN(parsedConcurrency) &&
    parsedConcurrency >= 1 &&
    parsedConcurrency !== (provider?.concurrency ?? null)
  const canApply = !applying && !loading && Boolean(providerId) && (modelChanged || concurrencyChanged)

  const handleApply = async () => {
    if (!providerId || !canApply) return
    setApplying(true)
    try {
      await applyLiveOverride({
        provider_id: providerId,
        old_model: modelChanged ? currentModel : undefined,
        new_model: modelChanged ? selectedModel : undefined,
        concurrency: concurrencyChanged ? parsedConcurrency! : undefined,
        persist: true,
      })
      const bits: string[] = []
      if (modelChanged) bits.push(`model -> ${selectedModel}`)
      if (concurrencyChanged) bits.push(`concurrency -> ${parsedConcurrency}`)
      toast.success(`Applied live: ${bits.join(', ')}`, {
        description: 'In-flight calls pick up the change. Your progress is kept.',
      })
      onApplied()
      onClose()
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to apply override'
      toast.error(msg)
    } finally {
      setApplying(false)
    }
  }

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/55 backdrop-blur-sm animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="model-swap-title"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-md bg-background border border-border/60 rounded-2xl shadow-2xl text-left overflow-hidden animate-in fade-in zoom-in-95 duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4 border-b border-border/20">
          <div className="flex items-start gap-3 min-w-0">
            <div
              className={cn(
                'p-2 rounded-lg shrink-0',
                auto ? 'bg-amber-500/10 text-amber-600 dark:text-amber-400' : 'bg-primary/10 text-primary'
              )}
            >
              {auto ? <AlertTriangle className="w-4.5 h-4.5" /> : <Repeat className="w-4.5 h-4.5" />}
            </div>
            <div className="min-w-0">
              <h3
                id="model-swap-title"
                className="font-extrabold text-sm text-foreground uppercase tracking-wider"
              >
                {auto ? 'Hitting Rate Limits' : 'Switch Model'}
              </h3>
              <p className="text-[11px] text-muted-foreground mt-0.5 leading-normal">
                {auto
                  ? 'Keys are exhausted or throttled. Swap to another model — your progress is kept.'
                  : 'Swap the model for the running job without losing progress.'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="p-1 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-5">
          {/* File + provider context */}
          <div className="flex items-center gap-2 text-[11px]">
            <span className="font-mono text-muted-foreground truncate" title={filename}>
              {filename}
            </span>
            {providerId && (
              <span className="px-1.5 py-0.5 rounded bg-muted/65 text-muted-foreground font-mono text-[9px] uppercase tracking-wide shrink-0">
                {providerId}
              </span>
            )}
          </div>

          {/* Model picker (same provider only) */}
          <div className="space-y-2">
            <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-1.5">
              <Zap className="w-3.5 h-3.5 text-muted-foreground" />
              Model
            </label>
            {loading ? (
              <div className="flex items-center gap-2 h-10 px-3 rounded-lg border border-border/40 bg-background/40 text-xs text-muted-foreground">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Loading models...
              </div>
            ) : modelOptions.length > 0 ? (
              <Select
                value={selectedModel}
                onChange={setSelectedModel}
                options={modelOptions}
                className="w-full md:w-full"
              />
            ) : (
              <div className="text-[11px] text-muted-foreground/70 italic py-2">
                No other models configured for this provider. Add one in Settings.
              </div>
            )}
            {currentModel && (
              <p className="text-[10px] text-muted-foreground/60 font-mono">
                Current: {currentModel}
              </p>
            )}
          </div>

          {/* Concurrency (optional, on the fly) */}
          <div className="space-y-2 pt-1 border-t border-border/10">
            <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-1.5">
              <Activity className="w-3.5 h-3.5 text-muted-foreground" />
              Max Concurrent API Calls
            </label>
            <Input
              type="number"
              min={1}
              value={concurrency}
              onChange={(e) => setConcurrency(e.target.value)}
              placeholder="Unlimited"
              className="bg-background/50 text-xs"
            />
            <p className="text-[10px] text-muted-foreground/60 leading-normal">
              Applies live to this provider. Lower it to ease off a throttling endpoint.
            </p>
          </div>
        </div>

        {/* Footer — single primary action, dismiss subordinate */}
        <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-border/20 bg-muted/10">
          <Button
            variant="ghost"
            onClick={onClose}
            className="text-xs font-bold uppercase tracking-wider px-4 rounded-lg h-10 text-muted-foreground hover:text-foreground"
          >
            {auto ? 'Keep Waiting' : 'Cancel'}
          </Button>
          <Button
            onClick={handleApply}
            disabled={!canApply}
            className="text-xs font-bold uppercase tracking-wider px-5 rounded-lg shadow-sm h-10 gap-1.5"
          >
            {applying ? <Loader2 className="w-4 h-4 animate-spin" /> : <Repeat className="w-4 h-4" />}
            Apply Live
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}
