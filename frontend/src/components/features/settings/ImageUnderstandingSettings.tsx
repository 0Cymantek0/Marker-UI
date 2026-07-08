import { Sparkles } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import type { LLMProvider } from '@/lib/api'

interface ImageUnderstandingSettingsProps {
  providers: LLMProvider[]
  vlmModel: string
  maxImagesPerDoc: number
  isSavingImageSetting: boolean
  onVlmModelChange: (value: string) => void
  onImageCapChange: (value: number) => void
  onImageCapCommit: (value: number) => void
}

export function ImageUnderstandingSettings({
  providers,
  vlmModel,
  maxImagesPerDoc,
  isSavingImageSetting,
  onVlmModelChange,
  onImageCapChange,
  onImageCapCommit,
}: ImageUnderstandingSettingsProps) {
  const hasVisionModel = providers.some((p) => (p.models ?? []).some((m) => m.vision_capable))
  const visionModelOptions = [
    { value: '', label: 'Auto (first vision-capable model)' },
    ...providers.flatMap((p) =>
      (p.models ?? []).map((m) => ({
        value: m.model_id,
        label: `${p.label}: ${m.model_id}${m.vision_capable ? '' : ' (mark vision-capable)'}`,
      }))
    ),
  ]

  return (
    <div className="space-y-4 pt-6 border-t border-border/20">
      <div className="space-y-1">
        <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-primary" />
          Image Understanding Defaults
        </h3>
        <p className="text-xs text-muted-foreground leading-relaxed max-w-3xl">
          Global defaults for VLM-powered image understanding. Per-conversion overrides live in Advanced Settings on the Convert page.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        <div className="space-y-1.5">
          <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase block">
            Default Vision Model
          </label>
          <Select
            value={vlmModel}
            onChange={onVlmModelChange}
            options={visionModelOptions}
            className="w-full md:w-full"
          />
          <p className="text-xs text-muted-foreground/70 leading-normal">
            {hasVisionModel
              ? 'Override auto-resolution. Only models marked vision-capable in the provider editors above are used for understanding.'
              : 'No vision-capable models yet. Pick one here, then mark it vision-capable in its provider editor above to enable understanding modes.'}
          </p>
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase block">
            Per-document Image Cap
          </label>
          <Input
            type="number"
            min={1}
            max={1000}
            value={maxImagesPerDoc}
            aria-label="Per-document image cap"
            onChange={(e) => {
              const val = e.target.value ? Number(e.target.value) : 50
              onImageCapChange(val)
            }}
            onBlur={(e) => {
              const val = Math.max(1, Math.min(1000, Number(e.target.value) || 50))
              onImageCapChange(val)
              onImageCapCommit(val)
            }}
            disabled={isSavingImageSetting}
            className="bg-background/50 h-9 text-xs"
          />
          <p className="text-xs text-muted-foreground/70 leading-normal">
            Caps VLM work per document (1-1000). Images beyond the cap keep their original reference.
          </p>
        </div>
      </div>

      <p className="text-xs text-muted-foreground/60 leading-normal">
        Cache management, privacy mode, and batch-API toggles are planned for a later phase (see roadmap).
      </p>
    </div>
  )
}
