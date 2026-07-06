import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { FileText, Code, Braces, Layers, HelpCircle, Settings2, X, ChevronDown, FlaskConical, Trash2, Save } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { messageFromUnknownError } from '@/lib/errors'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { AudioAdvancedSettings } from '@/components/features/audio/AudioAdvancedSettings'
import {
  getLLMProviders,
  getActiveLLM,
  getPresets,
  savePreset,
  deletePreset,
  normalizeOcrEngine,
  type LLMProvider,
  type ConversionConfig,
  type OutputFormat,
  type ConverterType,
  type ImageHandlingMode,
  type OcrEngine,
  type SmartRouterLevel,
  type ActiveLLM,
  type ConversionPreset
} from '@/lib/api'

interface ConversionOptionsProps {
  config: ConversionConfig
  onChange: (config: ConversionConfig) => void
  disabled?: boolean
  supportsMultiFormat?: boolean
}

const OUTPUT_FORMATS: { value: OutputFormat; label: string; desc: string; icon: typeof FileText }[] = [
  { value: 'markdown', label: 'Markdown', desc: 'Standard formatted Markdown', icon: FileText },
  { value: 'json', label: 'JSON', desc: 'Structured JSON layout metadata', icon: Braces },
  { value: 'html', label: 'HTML', desc: 'Rendered HTML structure', icon: Code },
  { value: 'chunks', label: 'Chunks', desc: 'Fragmented text layout chunks', icon: Layers },
]

const CONVERTERS: { value: ConverterType; label: string; desc: string }[] = [
  { value: 'PdfConverter', label: 'Standard PDF', desc: 'Extracts layout, text, tables, and images' },
  { value: 'TableConverter', label: 'Table Focused', desc: 'Optimized for spreadsheet/table sheets' },
  { value: 'OCRConverter', label: 'OCR Extraction', desc: 'Best for scanned or low-quality documents' },
  { value: 'ExtractionConverter', label: 'Marker Text Extract', desc: 'Marker extraction mode without LiteParse routing' },
]

const CONVERSION_PROFILES: { value: 'auto' | 'fast' | 'high_accuracy'; label: string; desc: string }[] = [
  {
    value: 'auto',
    label: 'Auto',
    desc: 'Probes PDF to automatically choose the safest engine.',
  },
  {
    value: 'fast',
    label: 'Fast',
    desc: 'Force the faster LiteParse path first. Lower accuracy on complex PDFs.',
  },
  {
    value: 'high_accuracy',
    label: 'High Accuracy',
    desc: 'Forces deep neural Marker PDF routing for maximum accuracy.',
  },
]

const IMAGE_HANDLING_OPTIONS: {
  value: ImageHandlingMode
  label: string
  desc: string
}[] = [
  {
    value: 'understanding',
    label: 'Understanding only',
    desc: 'Replace images with VLM text. Best for RAG pipelines.',
  },
  {
    value: 'extraction',
    label: 'Extraction only',
    desc: 'Keep current image files and Markdown image links. No VLM cost.',
  },
  {
    value: 'both',
    label: 'Both',
    desc: 'Add VLM text and keep the original image reference for audit.',
  },
]

// Two local engine choices only. Surya is the default baseline; Hybrid OCR runs
// a local multi-engine refinement pipeline on top of Surya. The specialist
// engines (GLM-OCR, PaddleOCR-VL) are internal — never user-facing dropdown
// values. Mistral OCR is removed entirely (cloud/API conflicts with local-first).
const OCR_ENGINE_OPTIONS: { value: OcrEngine; label: string }[] = [
  { value: 'surya', label: 'Surya (local, default)' },
  { value: 'hybrid_ocr', label: 'Hybrid OCR (local, experimental)' },
]

// Smart Image Router intelligence levels. Each option carries a pros/cons line
// shown live under the dropdown so the user sees the speed/accuracy trade.
const SMART_ROUTER_OPTIONS: {
  value: SmartRouterLevel
  label: string
  desc: string
}[] = [
  {
    value: 'disabled',
    label: 'Disabled (density only)',
    desc: 'Cheapest and fastest: routes on text density alone. May mis-route text-heavy charts to OCR and drop textless graphics.',
  },
  {
    value: 'smart',
    label: 'Smart (layout-aware)',
    desc: 'Classifies each crop with the local Surya layout model. Big accuracy gain for one extra local pass per image — no API cost.',
  },
  {
    value: 'beeg_brain',
    label: 'Beeg Brain (layout + density fusion)',
    desc: 'Highest accuracy: fuses layout and density and escalates disagreements to the VLM. Most local GPU and more cloud escalations.',
  },
]

// Schema defaults (ImageUnderstandingConfig) used as control fallbacks so a knob
// shows its real default until the user changes it.
const IU_DEFAULTS = {
  router_enabled: true,
  smart_router_level: 'smart' as SmartRouterLevel,
  dedup_enabled: true,
  downscale_vlm_crops: true,
  batch_enabled: true,
  ocr_engine: 'surya' as OcrEngine,
  decorative_max_text_density: 0.02,
  ocr_min_text_density: 0.45,
  ocr_min_lines: 3,
  dedup_max_distance: 0,
  vlm_crop_max_px: 768,
  vlm_batch_size: 8,
  max_batch_retries: 2,
} as const

export function ConversionOptions({ config, onChange, disabled, supportsMultiFormat = true }: ConversionOptionsProps) {
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [showTuning, setShowTuning] = useState(false)
  const [tempConfig, setTempConfig] = useState<ConversionConfig>(config)
  const [providers, setProviders] = useState<LLMProvider[]>([])
  const [activeLLM, setActiveLLM] = useState<ActiveLLM | null>(null)
  const hasVisionModel = providers.some((provider) =>
    provider.models?.some((model) => model.vision_capable)
  )
  const tempUsesImageUnderstanding = (tempConfig.image_handling_mode ?? 'extraction') !== 'extraction'

  const [presets, setPresets] = useState<ConversionPreset[]>([])
  const [selectedPresetId, setSelectedPresetId] = useState<string>('custom')
  const [isSavingPreset, setIsSavingPreset] = useState(false)
  const [presetName, setPresetName] = useState('')
  const [presetDesc, setPresetDesc] = useState('')
  const [showManage, setShowManage] = useState(false)

  const loadPresets = () => {
    getPresets()
      .then((data) => setPresets(data.map((preset) => ({
        ...preset,
        config: {
          ...preset.config,
          ocr_engine: normalizeOcrEngine(preset.config.ocr_engine),
        },
      }))))
      .catch((err) => console.error('Failed to load presets', err))
  }

  useEffect(() => {
    loadPresets()
  }, [])

  useEffect(() => {
    const matched = presets.find((p) => {
      const pConfig = p.config
      return Object.keys(pConfig).every(
        (key) => pConfig[key as keyof ConversionConfig] === config[key as keyof ConversionConfig]
      )
    })
    setSelectedPresetId(matched ? matched.id : 'custom')
  }, [config, presets])

  const handleSelectPreset = (presetId: string) => {
    if (presetId === 'custom') return
    const selected = presets.find((p) => p.id === presetId)
    if (selected) {
      onChange({
        ...config,
        ...selected.config,
        ocr_engine: normalizeOcrEngine(selected.config.ocr_engine),
      })
      setSelectedPresetId(presetId)
      toast.success(`Preset "${selected.name}" applied!`)
    }
  }

  const handleSavePreset = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!presetName.trim()) {
      toast.error('Preset name is required.')
      return
    }

    try {
      const configToSave = { ...config }
      const newPreset = await savePreset(presetName.trim(), configToSave, presetDesc.trim() || undefined)
      toast.success(`Preset "${newPreset.name}" saved!`)
      setPresetName('')
      setPresetDesc('')
      setIsSavingPreset(false)
      loadPresets()
    } catch (err: unknown) {
      toast.error(messageFromUnknownError(err, 'Failed to save preset.'))
    }
  }

  const handleDeletePreset = async (presetId: string, name: string) => {
    if (!window.confirm(`Are you sure you want to delete the preset "${name}"?`)) {
      return
    }
    try {
      await deletePreset(presetId)
      toast.success(`Preset "${name}" deleted.`)
      loadPresets()
    } catch (err: unknown) {
      toast.error(messageFromUnknownError(err, 'Failed to delete preset.'))
    }
  }

  useEffect(() => {
    if (isModalOpen) {
      Promise.all([getLLMProviders(), getActiveLLM()])
        .then(([provs, active]) => {
          setProviders(provs)
          setActiveLLM(active)
        })
        .catch((err) => {
          console.error('Failed to load LLM settings', err)
        })
    }
  }, [isModalOpen])

  const update = <K extends keyof ConversionConfig>(key: K, value: ConversionConfig[K]) => {
    onChange({ ...config, [key]: value })
  }

  const openModal = () => {
    setTempConfig({ ...config, ocr_engine: normalizeOcrEngine(config.ocr_engine) })
    setShowTuning(false)
    setIsModalOpen(true)
  }

  const handleSave = () => {
    onChange(tempConfig)
    setIsModalOpen(false)
    toast.success('Advanced settings applied successfully!')
  }

  const updateTemp = <K extends keyof ConversionConfig>(key: K, value: ConversionConfig[K]) => {
    setTempConfig((prev) => ({ ...prev, [key]: value }))
  }

  return (
    <div className="space-y-6">
      {/* Presets Selection & Management */}
      <div className="space-y-3 pb-4 border-b border-border/20">
        <div className="flex items-center justify-between">
          <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
            Conversion Preset
          </label>
          {presets.length > 0 && (
            <button
              type="button"
              onClick={() => setShowManage((prev) => !prev)}
              className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors focus:outline-none"
            >
              {showManage ? 'Hide Presets' : `Manage Presets (${presets.length})`}
            </button>
          )}
        </div>

        <div className="flex flex-col sm:flex-row gap-3">
          <Select
            value={selectedPresetId}
            onChange={handleSelectPreset}
            options={[
              { value: 'custom', label: 'Custom Configuration' },
              ...presets.map((p) => ({ value: p.id, label: p.name })),
            ]}
            className="flex-1"
            disabled={disabled}
          />
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            onClick={() => setIsSavingPreset((prev) => !prev)}
            className="h-10 text-[10px] font-bold uppercase tracking-wider flex items-center gap-1.5 shrink-0"
          >
            {isSavingPreset ? <X className="w-3.5 h-3.5" /> : <Save className="w-3.5 h-3.5 text-primary" />}
            {isSavingPreset ? 'Close' : 'Save Current'}
          </Button>
        </div>

        {/* Display selected preset description */}
        {selectedPresetId !== 'custom' && (
          (() => {
            const activePreset = presets.find((p) => p.id === selectedPresetId)
            return activePreset?.description ? (
              <p className="text-[10px] text-muted-foreground/90 italic leading-normal pl-1">
                {activePreset.description}
              </p>
            ) : null
          })()
        )}

        {/* Save Preset Inline Form */}
        {isSavingPreset && (
          <form onSubmit={handleSavePreset} className="mt-3 p-3 rounded-xl border border-primary/20 bg-primary/5 space-y-3 animate-fade-in">
            <div className="space-y-1">
              <label className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">
                Preset Name
              </label>
              <Input
                placeholder="e.g. OCR High Accuracy"
                value={presetName}
                onChange={(e) => setPresetName(e.target.value)}
                disabled={disabled}
                className="h-8 text-xs bg-background/50"
                required
              />
            </div>
            <div className="space-y-1">
              <label className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">
                Description (optional)
              </label>
              <Input
                placeholder="e.g. Max layout/VLM settings for scanned PDFs"
                value={presetDesc}
                onChange={(e) => setPresetDesc(e.target.value)}
                disabled={disabled}
                className="h-8 text-xs bg-background/50"
              />
            </div>
            <div className="flex gap-2 justify-end">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setIsSavingPreset(false)}
                className="h-7 text-[9px] uppercase tracking-wider"
              >
                Cancel
              </Button>
              <Button
                type="submit"
                size="sm"
                className="h-7 text-[9px] uppercase tracking-wider font-bold"
              >
                Save Preset
              </Button>
            </div>
          </form>
        )}

        {/* Manage Presets Area (Handles the edge case: scroll-contained to prevent UI extension) */}
        {showManage && presets.length > 0 && (
          <div className="mt-2 rounded-xl border border-border/50 bg-card/45 p-2.5 space-y-2">
            <div className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground/60 px-1 border-b border-border/20 pb-1">
              Saved Preset Profiles
            </div>
            <div className="max-h-36 overflow-y-auto pr-1 space-y-1.5 scrollbar-thin">
              {presets.map((p) => (
                <div key={p.id} className="flex items-start justify-between gap-3 p-1.5 rounded-lg hover:bg-muted/40 transition-colors">
                  <div className="space-y-0.5 min-w-0">
                    <div className="font-semibold text-xs text-foreground truncate">{p.name}</div>
                    {p.description && (
                      <div className="text-[10px] text-muted-foreground leading-normal truncate">{p.description}</div>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => handleDeletePreset(p.id, p.name)}
                    disabled={disabled}
                    className="p-1 rounded-md text-muted-foreground hover:text-red-500 hover:bg-red-500/10 transition-colors shrink-0 disabled:opacity-50"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
      {/* Output Format */}
      <div className="space-y-3">
        <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
          Output Format
        </label>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {OUTPUT_FORMATS.map((fmt) => {
            const selected = config.output_formats ?? ['markdown']
            const isActive = selected.includes(fmt.value)
            const isFormatDisabled = disabled || (!supportsMultiFormat && fmt.value !== 'markdown' && fmt.value !== 'chunks')
            return (
              <button
                key={fmt.value}
                type="button"
                onClick={() => {
                  if (isActive) {
                    if (selected.length <= 1) return
                    update('output_formats', selected.filter((f) => f !== fmt.value))
                  } else {
                    update('output_formats', [...selected, fmt.value])
                  }
                }}
                disabled={isFormatDisabled}
                className={cn(
                  'flex flex-col items-center justify-center p-3.5 rounded-xl border text-center transition-all duration-200 hover:scale-[1.01]',
                  isActive
                    ? 'border-primary bg-primary text-primary-foreground shadow-sm'
                    : 'border-border/60 bg-card/45 text-muted-foreground hover:bg-muted/30 hover:text-foreground',
                  isFormatDisabled && 'opacity-50 cursor-not-allowed'
                )}
              >
                <fmt.icon className={cn('w-5 h-5 mb-1.5', isActive ? 'text-primary-foreground' : 'text-muted-foreground')} />
                <span className={cn('font-semibold text-xs block', isActive ? 'text-primary-foreground' : 'text-foreground')}>{fmt.label}</span>
              </button>
            )
          })}
        </div>
      </div>

      {/* Converter Type */}
      <div className="space-y-3">
        <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
          Converter Engine
        </label>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {CONVERTERS.map((conv) => {
            const isActive = config.converter === conv.value
            return (
              <button
                key={conv.value}
                type="button"
                onClick={() => update('converter', conv.value)}
                disabled={disabled}
                className={cn(
                  'p-4 rounded-xl border text-left transition-all duration-200 hover:scale-[1.002]',
                  isActive
                    ? 'border-primary bg-primary text-primary-foreground shadow-sm'
                    : 'border-border/60 bg-card/45 text-muted-foreground hover:bg-muted/30 hover:text-foreground'
                )}
              >
                <span className={cn('block font-semibold text-xs', isActive ? 'text-primary-foreground' : 'text-foreground')}>{conv.label}</span>
                <span className={cn('block text-[11px] mt-1 leading-normal', isActive ? 'text-primary-foreground/85' : 'text-muted-foreground/90')}>
                  {conv.desc}
                </span>
              </button>
            )
          })}
        </div>
      </div>

      {/* Conversion Profile */}
      <div className="space-y-3">
        <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
          Conversion Profile
        </label>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {CONVERSION_PROFILES.map((prof) => {
            const isActive = (config.conversion_profile ?? 'auto') === prof.value
            return (
              <button
                key={prof.value}
                type="button"
                onClick={() => update('conversion_profile', prof.value)}
                disabled={disabled}
                className={cn(
                  'p-3.5 rounded-xl border text-left transition-all duration-200 hover:scale-[1.002] flex flex-col justify-between',
                  isActive
                    ? 'border-primary bg-primary text-primary-foreground shadow-sm'
                    : 'border-border/60 bg-card/45 text-muted-foreground hover:bg-muted/30 hover:text-foreground'
                )}
              >
                <div>
                  <span className={cn('block font-semibold text-xs', isActive ? 'text-primary-foreground' : 'text-foreground')}>{prof.label}</span>
                  <span className={cn('block text-[10px] mt-1 leading-normal', isActive ? 'text-primary-foreground/85' : 'text-muted-foreground/90')}>
                    {prof.desc}
                  </span>
                </div>
              </button>
            )
          })}
        </div>
        {(config.conversion_profile ?? 'auto') === 'fast' && (
          <div className="text-[11px] text-amber-600 dark:text-amber-400 p-2.5 rounded-lg border border-amber-500/20 bg-amber-500/5 leading-normal">
            Fast runs LiteParse first even for riskier PDFs. This is faster but may be less accurate; empty or very short output still retries with Marker.
          </div>
        )}
      </div>

      {/* Advanced Toggle */}
      <div className="pt-3 border-t border-border/20">
        <button
          type="button"
          onClick={openModal}
          disabled={disabled}
          className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-widest text-muted-foreground/80 hover:text-foreground transition-colors disabled:opacity-50"
        >
          <Settings2 className="w-4 h-4 text-primary" />
          <span>Configure Advanced Settings</span>
        </button>
      </div>

      {/* Popup Dialog Modal */}
      {isModalOpen && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-overlay-fade-in">
          <div className="glass-card max-w-lg w-full bg-background border border-border/50 rounded-2xl shadow-xl overflow-hidden animate-modal-zoom-in flex flex-col max-h-[90vh] text-left">
            
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-border/20">
              <div className="flex items-center gap-2.5">
                <Settings2 className="w-5 h-5 text-primary" />
                <div>
                  <h3 className="font-extrabold text-sm text-foreground uppercase tracking-wider">Advanced Options</h3>
                  <p className="text-[10px] text-muted-foreground mt-0.5">Fine-tune converters, OCR, and model overrides.</p>
                </div>
              </div>
              <button 
                type="button" 
                onClick={() => setIsModalOpen(false)} 
                className="p-1 rounded-md text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-6 space-y-4">
              <div className="space-y-2.5">
              <AudioAdvancedSettings
                  config={tempConfig}
                  onChange={updateTemp}
                  disabled={disabled}
                />
              </div>

              <hr className="border-border/20" />

              {/* Archive Extraction */}
              <div className="space-y-2.5">
                <div>
                  <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                    Archive Extraction
                  </label>
                  <p className="text-[11px] text-muted-foreground mt-1 leading-normal">
                    Configure extraction options when submitting ZIP archives.
                  </p>
                </div>

                <ToggleOption
                  label="Recursive Conversion"
                  description="Recursively convert safe children files inside the ZIP archive."
                  checked={tempConfig.archive_recursive ?? true}
                  onChange={(v) => updateTemp('archive_recursive', v)}
                  disabled={disabled}
                />

                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                  <NumberField
                    label="Max Archive Files"
                    help="Maximum number of files to scan inside the ZIP archive."
                    value={tempConfig.archive_max_files ?? 100}
                    min={1}
                    max={1000}
                    step={1}
                    onChange={(v) => updateTemp('archive_max_files', v)}
                    disabled={disabled}
                  />

                  <NumberField
                    label="Max Converted Children"
                    help="Maximum number of children files to convert."
                    value={tempConfig.archive_max_converted_children ?? 25}
                    min={1}
                    max={100}
                    step={1}
                    onChange={(v) => updateTemp('archive_max_converted_children', v)}
                    disabled={disabled}
                  />

                  <NumberField
                    label="Max Child Size (MB)"
                    help="Skip child files larger than this size (MB)."
                    value={(tempConfig.archive_max_child_bytes ?? 2 * 1024 * 1024) / (1024 * 1024)}
                    min={1}
                    max={50}
                    step={1}
                    onChange={(v) => updateTemp('archive_max_child_bytes', v * 1024 * 1024)}
                    disabled={disabled}
                  />
                </div>
              </div>

              <hr className="border-border/20" />

              <div className="space-y-2.5">
                <div className="flex items-center gap-1.5">
                  <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                    PDF / OCR Engine
                  </label>
                  <HelpIcon text="Engine used to OCR and refine PDF/image text. Surya is the local default. Hybrid OCR adds local specialist engines for tables, formulas, and difficult scans when installed — it never sends crops to the cloud." />
                </div>
                <Select
                  value={tempConfig.ocr_engine ?? IU_DEFAULTS.ocr_engine}
                  onChange={(val) => updateTemp('ocr_engine', val as OcrEngine)}
                  disabled={disabled}
                  options={OCR_ENGINE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
                  className="w-full md:w-full"
                />
                {(tempConfig.ocr_engine ?? IU_DEFAULTS.ocr_engine) === 'hybrid_ocr' && (
                  <div
                    className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2"
                    data-testid="hybrid-ocr-warning"
                  >
                    <p className="text-[11px] text-amber-700 dark:text-amber-300 leading-normal">
                      Hybrid OCR runs a local multi-engine refinement pipeline. Surya builds the baseline document, then local
                      specialist engines improve selected tables, formulas, and difficult scanned regions when installed. It is
                      slower and may require extra local model setup, but no OCR crop is sent to cloud services by this engine.
                    </p>
                  </div>
                )}
              </div>

              <hr className="border-border/20" />

              <div className="space-y-2.5">
                <div>
                  <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                    Image Understanding
                  </label>
                  <p className="text-[11px] text-muted-foreground mt-1 leading-normal">
                    Choose how extracted document images appear in Markdown.
                  </p>
                </div>

                <div className="grid grid-cols-1 gap-2" role="radiogroup" aria-label="Image Understanding">
                  {IMAGE_HANDLING_OPTIONS.map((option) => {
                    const isSelected = (tempConfig.image_handling_mode ?? 'extraction') === option.value
                    const needsVision = option.value !== 'extraction'
                    const optionDisabled = disabled || (needsVision && !hasVisionModel)
                    return (
                      <RadioOption
                        key={option.value}
                        label={option.label}
                        description={option.desc}
                        selected={isSelected}
                        disabled={optionDisabled}
                        onClick={() => {
                          updateTemp('image_handling_mode', option.value)
                          updateTemp('disable_image_extraction', option.value === 'understanding')
                        }}
                      />
                    )
                  })}
                </div>

                {!hasVisionModel && (
                  <p className="text-[11px] text-amber-600 dark:text-amber-400 leading-normal">
                    Enable vision capability for at least one model in Settings to use understanding modes.
                  </p>
                )}

                {tempUsesImageUnderstanding && (
                  <ToggleOption
                    label="Allow Cloud Image Analysis"
                    description="Send image crops that need chart, diagram, or photo understanding to the configured vision model."
                    checked={tempConfig.allow_cloud_vlm ?? false}
                    onChange={(v) => updateTemp('allow_cloud_vlm', v)}
                    disabled={disabled || !hasVisionModel}
                  />
                )}

                {tempUsesImageUnderstanding && (
                  <div className="space-y-1 pt-1 animate-fade-in relative z-30">
                    <ToggleOption
                      label="Smart Image Router"
                      description="Grade each image locally first: skip decorative, OCR text-as-image, send only genuine visuals to the VLM. Off uses the legacy classify-every-image path."
                      checked={tempConfig.router_enabled ?? IU_DEFAULTS.router_enabled}
                      onChange={(v) => updateTemp('router_enabled', v)}
                      disabled={disabled}
                    />
                    {(tempConfig.router_enabled ?? IU_DEFAULTS.router_enabled) && (
                      <div className="space-y-1.5 px-2.5 pt-1.5">
                        <div className="flex items-center gap-1.5">
                          <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                            Router Intelligence
                          </label>
                          <HelpIcon text="How hard the local router thinks before routing each image. Higher levels cost more local GPU but route more accurately." />
                        </div>
                        <Select
                          value={tempConfig.smart_router_level ?? IU_DEFAULTS.smart_router_level}
                          onChange={(val) => updateTemp('smart_router_level', val as SmartRouterLevel)}
                          disabled={disabled}
                          options={SMART_ROUTER_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
                          className="w-full md:w-full"
                        />
                        <p className="text-[11px] text-muted-foreground leading-normal" data-testid="smart-router-desc">
                          {SMART_ROUTER_OPTIONS.find(
                            (o) => o.value === (tempConfig.smart_router_level ?? IU_DEFAULTS.smart_router_level),
                          )?.desc ?? 'Classifies each crop with local layout signals before routing.'}
                        </p>
                      </div>
                    )}
                    <ToggleOption
                      label="Deduplicate Repeated Images"
                      description="Collapse identical images (logos, recurring figures) to a single analysis, reused for every copy."
                      checked={tempConfig.dedup_enabled ?? IU_DEFAULTS.dedup_enabled}
                      onChange={(v) => updateTemp('dedup_enabled', v)}
                      disabled={disabled}
                    />
                    <ToggleOption
                      label="Downscale Crops Before Send"
                      description="Shrink image crops into the cheaper vision-token band before the VLM call. The biggest cost lever."
                      checked={tempConfig.downscale_vlm_crops ?? IU_DEFAULTS.downscale_vlm_crops}
                      onChange={(v) => updateTemp('downscale_vlm_crops', v)}
                      disabled={disabled}
                    />
                    <ToggleOption
                      label="Batch VLM Calls"
                      description="Route and extract many images in one structured call instead of two serial calls per image."
                      checked={tempConfig.batch_enabled ?? IU_DEFAULTS.batch_enabled}
                      onChange={(v) => updateTemp('batch_enabled', v)}
                      disabled={disabled}
                    />

                    {/* Experimental / tuning disclosure */}
                    <div className="pt-2">
                      <button
                        type="button"
                        onClick={() => setShowTuning((s) => !s)}
                        aria-expanded={showTuning}
                        className="flex items-center gap-2 w-full px-2.5 py-2 rounded-lg text-[10px] font-bold uppercase tracking-widest text-muted-foreground/80 hover:text-foreground hover:bg-muted/30 transition-colors"
                      >
                        <FlaskConical className="w-3.5 h-3.5 text-primary" />
                        <span>Experimental / Tuning</span>
                        <ChevronDown className={cn('w-3.5 h-3.5 ml-auto transition-transform', showTuning && 'rotate-180')} />
                      </button>

                      {showTuning && (
                        <div className="mt-1 pl-3 border-l border-primary/20 space-y-3 animate-fade-in">
                          <p className="text-[11px] text-muted-foreground leading-normal pt-1">
                            Power-user thresholds. Leave at defaults unless you are tuning routing against your own documents.
                          </p>
                          <SliderField
                            label="Decorative Max Text Density"
                            help="At or below this text-area fraction (and no real lines), an image is treated as decorative and skipped."
                            value={tempConfig.decorative_max_text_density ?? IU_DEFAULTS.decorative_max_text_density}
                            min={0} max={1} step={0.01}
                            onChange={(v) => updateTemp('decorative_max_text_density', v)}
                            disabled={disabled}
                          />
                          <SliderField
                            label="OCR Min Text Density"
                            help="At or above this text-area fraction, an image routes to local OCR instead of the cloud VLM."
                            value={tempConfig.ocr_min_text_density ?? IU_DEFAULTS.ocr_min_text_density}
                            min={0} max={1} step={0.01}
                            onChange={(v) => updateTemp('ocr_min_text_density', v)}
                            disabled={disabled}
                          />
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                            <NumberField
                              label="OCR Min Lines"
                              help="Minimum detected text lines before the OCR route is considered."
                              value={tempConfig.ocr_min_lines ?? IU_DEFAULTS.ocr_min_lines}
                              min={1} step={1}
                              onChange={(v) => updateTemp('ocr_min_lines', v)}
                              disabled={disabled}
                            />
                            <NumberField
                              label="Dedup Max Distance"
                              help="Max aHash Hamming distance treated as a duplicate. 0 = exact match (safest)."
                              value={tempConfig.dedup_max_distance ?? IU_DEFAULTS.dedup_max_distance}
                              min={0} max={64} step={1}
                              onChange={(v) => updateTemp('dedup_max_distance', v)}
                              disabled={disabled}
                            />
                            <NumberField
                              label="VLM Crop Max Px"
                              help="Longest-side pixel cap applied to a crop before the VLM send."
                              value={tempConfig.vlm_crop_max_px ?? IU_DEFAULTS.vlm_crop_max_px}
                              min={64} max={4096} step={64}
                              onChange={(v) => updateTemp('vlm_crop_max_px', v)}
                              disabled={disabled}
                            />
                            <NumberField
                              label="VLM Batch Size"
                              help="Images per batched VLM call (clamped per provider)."
                              value={tempConfig.vlm_batch_size ?? IU_DEFAULTS.vlm_batch_size}
                              min={1} max={64} step={1}
                              onChange={(v) => updateTemp('vlm_batch_size', v)}
                              disabled={disabled}
                            />
                            <NumberField
                              label="Max Batch Retries"
                              help="Max extra batch calls to recover missing or garbled images."
                              value={tempConfig.max_batch_retries ?? IU_DEFAULTS.max_batch_retries}
                              min={0} max={5} step={1}
                              onChange={(v) => updateTemp('max_batch_retries', v)}
                              disabled={disabled}
                            />
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>

              <ToggleOption
                label="Enable LLM Integration"
                description="Use a Large Language Model (Gemini, Claude, GPT, etc.) to format tables, clean up layout artifacts, and fix extraction errors."
                checked={tempConfig.use_llm ?? false}
                onChange={(v) => updateTemp('use_llm', v)}
                disabled={disabled}
              />

              {tempConfig.use_llm && (
                <div className="pl-4 border-l border-primary/20 space-y-3 animate-fade-in relative z-20">
                  <div className="space-y-1.5">
                    <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                      LLM Provider Override
                    </label>
                    <Select
                      value={tempConfig.llm_provider ?? ''}
                      onChange={(val) => {
                        updateTemp('llm_provider', val || undefined)
                        const prov = providers.find((p) => p.id === val)
                        const firstModel = prov?.models?.[0]?.model_id || ''
                        updateTemp('llm_model', firstModel || undefined)
                      }}
                      disabled={disabled}
                      options={[
                        {
                          value: '',
                          label: activeLLM && activeLLM.provider_id !== 'none'
                            ? `Use Global Active (${providers.find(p => p.id === activeLLM.provider_id)?.label || activeLLM.provider_id}: ${activeLLM.model_id})`
                            : 'Use Global Active (No Override)'
                        },
                        ...providers.map((p) => ({
                          value: p.id,
                          label: p.label
                        }))
                      ]}
                      className="w-full md:w-full"
                    />
                  </div>

                  {tempConfig.llm_provider && (
                    <div className="space-y-1.5 animate-fade-in">
                      <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                        LLM Model Override
                      </label>
                      <Select
                        value={tempConfig.llm_model ?? ''}
                        onChange={(val) => updateTemp('llm_model', val || undefined)}
                        disabled={disabled}
                        options={(() => {
                          const selectedProv = providers.find((p) => p.id === tempConfig.llm_provider)
                          if (!selectedProv || !selectedProv.models || selectedProv.models.length === 0) {
                            return [{ value: '', label: 'No models configured for this provider' }]
                          }
                          return selectedProv.models.map((m) => ({
                            value: m.model_id,
                            label: m.model_id
                          }))
                        })()}
                        className="w-full md:w-full"
                      />
                    </div>
                  )}
                </div>
              )}

              <ToggleOption 
                label="Force OCR on All Pages" 
                description="Force Optical Character Recognition on all pages. Recommended for scans with corrupt or missing text layers."
                checked={tempConfig.force_ocr ?? false} 
                onChange={(v) => updateTemp('force_ocr', v)} 
                disabled={disabled} 
              />

              <ToggleOption 
                label="Paginate Output Layout" 
                description="Include page breaks and page numbers in the output Markdown to match the original document pagination."
                checked={tempConfig.paginate ?? false} 
                onChange={(v) => updateTemp('paginate', v)} 
                disabled={disabled} 
              />

              <ToggleOption 
                label="Disable Image Extraction" 
                description="Skip extracting and saving images. Speeds up processing and reduces final file size."
                checked={tempConfig.disable_image_extraction ?? false} 
                onChange={(v) => updateTemp('disable_image_extraction', v)} 
                disabled={disabled} 
              />

              {/* Text fields */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2">
                <div className="space-y-1.5">
                  <div className="flex items-center gap-1.5">
                    <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                      Page Range
                    </label>
                    <HelpIcon text="Convert only specific pages. Format: '1-10', '1,3,5', or '1-5,7-9'." />
                  </div>
                  <Input
                    value={tempConfig.page_range ?? ''}
                    onChange={(e) => updateTemp('page_range', e.target.value)}
                    placeholder="e.g. 1-10"
                    disabled={disabled}
                    className="bg-background/50 h-9 text-xs"
                  />
                </div>
                
                <div className="space-y-1.5">
                  <div className="flex items-center gap-1.5">
                    <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
                      Language Hint
                    </label>
                    <HelpIcon text="Primary language code (e.g., 'en', 'es', 'fr') to improve OCR spelling and character recognition." />
                  </div>
                  <Input
                    value={tempConfig.language ?? ''}
                    onChange={(e) => updateTemp('language', e.target.value)}
                    placeholder="e.g. en"
                    disabled={disabled}
                    className="bg-background/50 h-9 text-xs"
                  />
                </div>
              </div>

              <ToggleOption 
                label="Disable Multiprocessing" 
                description="Run conversion on a single thread. Saves CPU and RAM on resource-constrained systems."
                checked={tempConfig.disable_multiprocessing ?? false} 
                onChange={(v) => updateTemp('disable_multiprocessing', v)} 
                disabled={disabled} 
              />

              <ToggleOption 
                label="Debug Execution Mode" 
                description="Stream verbose internal logs and keep intermediate temp files to help troubleshoot conversion issues."
                checked={tempConfig.debug ?? false} 
                onChange={(v) => updateTemp('debug', v)} 
                disabled={disabled} 
              />
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-border/20 bg-muted/10">
              <Button 
                variant="ghost" 
                onClick={() => setIsModalOpen(false)}
                className="text-xs font-bold uppercase tracking-wider px-4 rounded-lg h-10"
              >
                Cancel
              </Button>
              <Button 
                onClick={handleSave}
                className="text-xs font-bold uppercase tracking-wider px-5 rounded-lg shadow-sm h-10"
              >
                Apply Settings
              </Button>
            </div>
            
          </div>
        </div>,
        document.body
      )}
    </div>
  )
}

// ─── Help Icon Helper ───────────────────────────────────────────────────

function HelpIcon({ text }: { text: string }) {
  return (
    <div className="group relative">
      <HelpCircle className="w-3.5 h-3.5 text-muted-foreground/60 hover:text-muted-foreground cursor-help" />
      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block w-48 p-2 rounded-lg bg-slate-900 dark:bg-slate-800 text-[10px] leading-normal text-slate-100 shadow-lg border border-slate-800/80 z-20 pointer-events-none text-left">
        {text}
      </div>
    </div>
  )
}

// ─── Toggle Option ───────────────────────────────────────────────────

function ToggleOption({
  label,
  description,
  checked,
  onChange,
  disabled,
}: {
  label: string
  description?: string
  checked: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      disabled={disabled}
      className={cn(
        'flex items-start justify-between w-full p-2.5 rounded-xl border border-transparent transition-all',
        'hover:bg-muted/30 hover:border-border/30',
        disabled && 'opacity-50 pointer-events-none'
      )}
    >
      <div className="text-left max-w-[85%]">
        <span className="text-xs font-semibold text-foreground block">{label}</span>
        {description && (
          <span className="block text-[11px] text-muted-foreground mt-0.5 leading-normal">
            {description}
          </span>
        )}
      </div>
      <div
        className={cn(
          'w-9 h-5 rounded-full transition-colors relative shrink-0 mt-0.5 border border-border/10',
          checked ? 'bg-primary' : 'bg-muted'
        )}
      >
        <div
          className={cn(
            'absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white shadow-sm transition-transform duration-200',
            checked ? 'left-[17px]' : 'left-0.5'
          )}
        />
      </div>
    </button>
  )
}

function RadioOption({
  label,
  description,
  selected,
  disabled,
  onClick,
}: {
  label: string
  description: string
  selected: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'flex items-start gap-3 w-full p-3 rounded-xl border text-left transition-all',
        selected
          ? 'border-primary/60 bg-primary/10 text-foreground'
          : 'border-border/40 bg-card/35 text-muted-foreground hover:bg-muted/30 hover:text-foreground',
        disabled && 'opacity-50 pointer-events-none'
      )}
    >
      <span
        className={cn(
          'mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full border',
          selected ? 'border-primary' : 'border-muted-foreground/40'
        )}
      >
        {selected && <span className="h-2 w-2 rounded-full bg-primary" />}
      </span>
      <span className="min-w-0">
        <span className="text-xs font-semibold text-foreground block">{label}</span>
        <span className="block text-[11px] text-muted-foreground mt-0.5 leading-normal">
          {description}
        </span>
      </span>
    </button>
  )
}

// ─── Slider Field (0–1 thresholds) ───────────────────────────────────

function SliderField({
  label,
  help,
  value,
  min,
  max,
  step,
  onChange,
  disabled,
}: {
  label: string
  help: string
  value: number
  min: number
  max: number
  step: number
  onChange: (value: number) => void
  disabled?: boolean
}) {
  return (
    <div className={cn('space-y-1.5', disabled && 'opacity-50 pointer-events-none')}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
            {label}
          </label>
          <HelpIcon text={help} />
        </div>
        <span className="text-[11px] font-bold tabular-nums text-foreground">{value.toFixed(2)}</span>
      </div>
      <input
        type="range"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full h-6 appearance-none bg-transparent accent-primary cursor-pointer [&::-webkit-slider-runnable-track]:h-1.5 [&::-webkit-slider-runnable-track]:rounded-full [&::-webkit-slider-runnable-track]:bg-muted [&::-moz-range-track]:h-1.5 [&::-moz-range-track]:rounded-full [&::-moz-range-track]:bg-muted"
      />
    </div>
  )
}

// ─── Number Field (integer knobs) ────────────────────────────────────

function NumberField({
  label,
  help,
  value,
  min,
  max,
  step,
  onChange,
  disabled,
}: {
  label: string
  help: string
  value: number
  min: number
  max?: number
  step: number
  onChange: (value: number) => void
  disabled?: boolean
}) {
  const clamp = (n: number) => {
    if (Number.isNaN(n)) return min
    if (n < min) return min
    if (max !== undefined && n > max) return max
    return n
  }
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
          {label}
        </label>
        <HelpIcon text={help} />
      </div>
      <Input
        type="number"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(clamp(Number(e.target.value)))}
        className="bg-background/50 h-9 text-xs"
      />
    </div>
  )
}
