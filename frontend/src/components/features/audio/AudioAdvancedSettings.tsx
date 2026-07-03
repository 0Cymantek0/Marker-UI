import { useState, useEffect } from 'react'
import {
  ChevronDown,
  Mic,
  FileText,
  BookOpen,
  Users,
  BarChart3,
  Sparkles,
  Combine,
  Shield,
  GitCompare,
  HelpCircle,
  AlertTriangle,
  Cloud,
  HardDrive,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import {
  getAudioCapabilities,
  type ConversionConfig,
  type AudioOutputMode,
  type AudioProviderType,
  type AudioStructuralMode,
  type AudioProviderCapability,
} from '@/lib/api'
import { VocabularyPackEditor } from './VocabularyPackEditor'

interface AudioAdvancedSettingsProps {
  config: ConversionConfig
  onChange: <K extends keyof ConversionConfig>(key: K, value: ConversionConfig[K]) => void
  disabled?: boolean
}

const ENHANCEMENT_LABELS: Record<number, { label: string; desc: string }> = {
  0: { label: 'Off', desc: 'Raw STT output. Best for legal/audit.' },
  1: { label: 'Minimal', desc: 'Punctuation, casing, spacing only.' },
  2: { label: 'Conservative', desc: 'Fix likely ASR errors with vocabulary.' },
  3: { label: 'Balanced', desc: 'Readable notes, filler cleanup, paragraphs.' },
  4: { label: 'Strong', desc: 'Polished written notes with source refs.' },
  5: { label: 'Editorial', desc: 'Full rewrite with strict evidence binding.' },
}

const OUTPUT_STYLE_OPTIONS: { value: AudioOutputMode; label: string; desc: string }[] = [
  { value: 'transcript', label: 'Raw Transcript', desc: 'Timestamped speech segments' },
  { value: 'enhanced', label: 'Evidence-First Notes', desc: 'Extractive notes with source refs' },
  { value: 'meeting_notes', label: 'Meeting Notes', desc: 'Decisions, actions, questions' },
  { value: 'lecture_notes', label: 'Lecture Notes', desc: 'Topics, definitions, examples' },
  { value: 'interview_qna', label: 'Interview / Q&A', desc: 'Questions and answers extracted' },
  { value: 'action_decision_log', label: 'Action + Decision Log', desc: 'Compact operational table' },
]

const STRUCTURAL_MODE_OPTIONS: { value: AudioStructuralMode; label: string }[] = [
  { value: 'auto', label: 'Auto (detect from content)' },
  { value: 'paragraphs', label: 'Paragraphs' },
  { value: 'speaker_sections', label: 'Speaker Sections' },
  { value: 'meeting_notes', label: 'Meeting Notes' },
  { value: 'lecture_notes', label: 'Lecture Notes' },
  { value: 'interview_qna', label: 'Interview Q&A' },
  { value: 'action_decision_log', label: 'Action + Decision Log' },
  { value: 'timeline', label: 'Timeline' },
]

export function AudioAdvancedSettings({ config, onChange, disabled }: AudioAdvancedSettingsProps) {
  const [capabilities, setCapabilities] = useState<AudioProviderCapability[]>([])
  const [showAdvanced, setShowAdvanced] = useState(false)

  useEffect(() => {
    getAudioCapabilities()
      .then(setCapabilities)
      .catch(() => {})
  }, [])

  const activeProvider = config.audio_provider ?? 'local_faster_whisper'
  const cap = capabilities.find((c) => c.provider_id === activeProvider)
  const isCloud = cap?.cloud ?? false

  const selectableCapabilities = capabilities.filter((c) => c.available !== false)
  const comparableProviderCount = selectableCapabilities.filter((c) => c.supports_batch_compare).length
  const canCompareProviders = comparableProviderCount > 1
  const cloudEnhancementAvailable = false
  const providerOptions = selectableCapabilities.length > 0
    ? selectableCapabilities.map((c) => ({
        value: c.provider_id,
        label: `${c.provider_label}${c.cloud ? ' (cloud)' : ''}`,
      }))
    : [{ value: 'local_faster_whisper', label: 'Local faster-whisper' }]

  return (
    <div className="space-y-1" data-testid="audio-advanced-settings">
      {/* Section Header */}
      <div className="pb-1">
        <div className="flex items-center gap-2">
          <Mic className="w-4 h-4 text-primary" />
          <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase">
            Audio & Voice Notes
          </label>
        </div>
        <p className="text-[11px] text-muted-foreground mt-1 leading-normal">
          Configure transcription, speakers, vocabulary, enhancement, and more.
        </p>
      </div>

      {/* ─── 1. TRANSCRIPTION (always visible) ─── */}
      <CollapsibleSection
        icon={Mic}
        title="Transcription"
        defaultOpen
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <SectionLabel label="Provider" help="Speech-to-text engine. Local runs on your machine; cloud requires opt-in." />
            <Select
              value={activeProvider}
              onChange={(val) => {
                onChange('audio_provider', val as AudioProviderType)
                const selected = capabilities.find((c) => c.provider_id === val)
                if (selected?.cloud && !config.audio_allow_cloud_stt) {
                  onChange('audio_allow_cloud_stt', true)
                }
              }}
              options={providerOptions}
              disabled={disabled}
              className="w-full"
            />
            {isCloud && (
              <CloudBadge />
            )}
            {cap?.available === false && (
              <ProviderWarning message={`${cap.provider_label} is listed for future support but its adapter is not shipped in this build.`} />
            )}
          </div>

          <div className="space-y-1.5">
            <SectionLabel label="Model" help="Model name for transcription. Examples: tiny.en, base.en, small, medium, large-v3." />
            <Input
              value={config.audio_model ?? (cap?.default_model || 'tiny.en')}
              onChange={(e) => onChange('audio_model', e.target.value)}
              placeholder={cap?.default_model || 'tiny.en'}
              disabled={disabled}
              className="bg-background/50 h-9 text-xs"
            />
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
          <div className="space-y-1.5">
            <SectionLabel label="Language" help="Spoken language hint (e.g., en, es, fr, hi). Improves accuracy." />
            <Input
              value={config.audio_language ?? ''}
              onChange={(e) => onChange('audio_language', e.target.value || undefined)}
              placeholder="auto-detect"
              disabled={disabled}
              className="bg-background/50 h-9 text-xs"
            />
          </div>

          {!isCloud && (
            <div className="space-y-1.5">
              <SectionLabel label="Device" help="Local inference device: cpu or cuda." />
              <Select
                value={config.audio_device ?? 'cpu'}
                onChange={(val) => onChange('audio_device', val)}
                options={[
                  { value: 'cpu', label: 'CPU' },
                  { value: 'cuda', label: 'CUDA (GPU)' },
                ]}
                disabled={disabled}
                className="w-full"
              />
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-x-6 gap-y-2 mt-3">
          <ToggleChip
            label="Word Timestamps"
            checked={config.audio_word_timestamps ?? false}
            onChange={(v) => onChange('audio_word_timestamps', v)}
            disabled={disabled || (cap ? !cap.supports_word_timestamps : false)}
          />
          <ToggleChip
            label="VAD Filter"
            checked={config.audio_vad_filter ?? true}
            onChange={(v) => onChange('audio_vad_filter', v)}
            disabled={disabled || isCloud}
          />
        </div>
      </CollapsibleSection>

      {/* ─── 2. OUTPUT STYLE (always visible) ─── */}
      <CollapsibleSection
        icon={FileText}
        title="Output Style"
        defaultOpen
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {OUTPUT_STYLE_OPTIONS.map((opt) => {
            const isActive = (config.audio_output_mode ?? 'transcript') === opt.value
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => onChange('audio_output_mode', opt.value)}
                disabled={disabled}
                className={cn(
                  'p-2.5 rounded-lg border text-left transition-all duration-150',
                  isActive
                    ? 'border-primary/60 bg-primary/10'
                    : 'border-border/30 bg-card/30 hover:bg-muted/30 hover:border-border/50',
                  disabled && 'opacity-50 pointer-events-none'
                )}
              >
                <span className={cn('text-xs font-semibold block', isActive ? 'text-primary' : 'text-foreground')}>
                  {opt.label}
                </span>
                <span className="text-[10px] text-muted-foreground mt-0.5 block leading-snug">
                  {opt.desc}
                </span>
              </button>
            )
          })}
        </div>
      </CollapsibleSection>

      {/* ─── 3. VOCABULARY (always visible) ─── */}
      <CollapsibleSection
        icon={BookOpen}
        title="Vocabulary"
        defaultOpen
      >
        <div className="space-y-1.5">
          <SectionLabel label="Quick Terms" help="Comma-separated names, acronyms, domain terms. Used as STT prompt hints." />
          <Input
            value={config.audio_vocabulary ?? ''}
            onChange={(e) => onChange('audio_vocabulary', e.target.value)}
            placeholder="names, acronyms, terms"
            disabled={disabled}
            className="bg-background/50 h-9 text-xs"
          />
        </div>
        <div className="mt-3">
          <VocabularyPackEditor
            selectedPackIds={config.audio_vocabulary_pack_ids ?? []}
            onChangePackIds={(ids: string[]) => onChange('audio_vocabulary_pack_ids', ids)}
            disabled={disabled}
            providerSupportsVocab={cap?.supports_custom_vocabulary ?? true}
          />
        </div>
        {cap && !cap.supports_custom_vocabulary && (
          <ProviderWarning message={`${cap.provider_label} does not support vocabulary prompts. Terms will only be used in second-pass correction.`} />
        )}
      </CollapsibleSection>

      {/* ─── ADVANCED TOGGLE ─── */}
      <button
        type="button"
        onClick={() => setShowAdvanced(!showAdvanced)}
        className="flex items-center gap-2 w-full py-2 text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70 hover:text-foreground transition-colors"
      >
        <ChevronDown className={cn('w-3.5 h-3.5 transition-transform', showAdvanced && 'rotate-180')} />
        <span>{showAdvanced ? 'Hide Advanced Controls' : 'Show Advanced Controls'}</span>
      </button>

      {showAdvanced && (
        <div className="space-y-1 animate-fade-in">
          {/* ─── 4. SPEAKERS ─── */}
          <CollapsibleSection
            icon={Users}
            title="Speakers"
            badge={cap?.supports_diarization ? undefined : 'limited'}
          >
            <ToggleChip
              label="Enable Diarization"
              checked={config.audio_diarization ?? false}
              onChange={(v) => onChange('audio_diarization', v)}
              disabled={disabled || (cap ? !cap.supports_diarization : false)}
            />
            {cap && !cap.supports_diarization && (
              <ProviderWarning message={`${cap.provider_label} does not support speaker diarization. All segments will be labeled speaker_0.`} />
            )}
            {config.audio_diarization && cap?.supports_diarization && (
              <div className="grid grid-cols-2 gap-3 mt-3">
                <div className="space-y-1.5">
                  <SectionLabel label="Min Speakers" help="Expected minimum speaker count." />
                  <Input
                    type="number"
                    value={config.audio_min_speakers ?? ''}
                    onChange={(e) => onChange('audio_min_speakers', e.target.value ? Number(e.target.value) : undefined)}
                    placeholder="auto"
                    min={1}
                    max={20}
                    disabled={disabled}
                    className="bg-background/50 h-9 text-xs"
                  />
                </div>
                <div className="space-y-1.5">
                  <SectionLabel label="Max Speakers" help="Expected maximum speaker count." />
                  <Input
                    type="number"
                    value={config.audio_max_speakers ?? ''}
                    onChange={(e) => onChange('audio_max_speakers', e.target.value ? Number(e.target.value) : undefined)}
                    placeholder="auto"
                    min={1}
                    max={20}
                    disabled={disabled}
                    className="bg-background/50 h-9 text-xs"
                  />
                </div>
              </div>
            )}
            <p className="text-[10px] text-muted-foreground/70 mt-2 leading-snug">
              Speaker labels are anonymous by default. Only labels you explicitly map to names are renamed.
            </p>
          </CollapsibleSection>

          {/* ─── 5. CONFIDENCE & REVIEW ─── */}
          <CollapsibleSection
            icon={BarChart3}
            title="Confidence & Review"
          >
            <div className="space-y-3">
              <div className="space-y-1.5">
                <SectionLabel label="Low Confidence Threshold" help="Segments below this confidence get review warnings." />
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={config.audio_low_confidence_threshold ?? 0.65}
                    onChange={(e) => onChange('audio_low_confidence_threshold', Number(e.target.value))}
                    disabled={disabled}
                    className="flex-1 h-6 appearance-none bg-transparent accent-primary cursor-pointer [&::-webkit-slider-runnable-track]:h-1.5 [&::-webkit-slider-runnable-track]:rounded-full [&::-webkit-slider-runnable-track]:bg-muted"
                  />
                  <span className="text-xs font-bold tabular-nums text-foreground w-10 text-right">
                    {(config.audio_low_confidence_threshold ?? 0.65).toFixed(2)}
                  </span>
                </div>
              </div>
              <div className="flex flex-wrap gap-x-6 gap-y-2">
                <ToggleChip
                  label="Confidence Heatmap"
                  checked={config.audio_confidence_heatmap ?? true}
                  onChange={(v) => onChange('audio_confidence_heatmap', v)}
                  disabled={disabled}
                />
                <ToggleChip
                  label="Quality Diagnostics"
                  checked={config.audio_quality_diagnostics ?? true}
                  onChange={(v) => onChange('audio_quality_diagnostics', v)}
                  disabled={disabled}
                />
                <ToggleChip
                  label="Require Review on Low Confidence"
                  checked={config.audio_review_required_on_low_confidence ?? false}
                  onChange={(v) => onChange('audio_review_required_on_low_confidence', v)}
                  disabled={disabled}
                />
              </div>
            </div>
          </CollapsibleSection>

          {/* ─── 6. ENHANCEMENT & CORRECTION ─── */}
          <CollapsibleSection
            icon={Sparkles}
            title="Enhancement & Correction"
          >
            <div className="space-y-4">
              <div className="space-y-2">
                <ToggleChip
                  label="Improve Transcript Wording"
                  checked={config.audio_text_enhancement_enabled ?? false}
                  onChange={(v) => {
                    onChange('audio_text_enhancement_enabled', v)
                    if (v && !config.audio_text_enhancement_strength) {
                      onChange('audio_text_enhancement_strength', 1)
                    }
                  }}
                  disabled={disabled}
                />
                {config.audio_text_enhancement_enabled && (
                  <div className="pl-3 border-l-2 border-primary/20 space-y-2 mt-2 animate-fade-in">
                    <SectionLabel label="Strength" help="How much the system may alter transcript wording." />
                    <div className="flex items-center gap-3">
                      <input
                        type="range"
                        min={0}
                        max={5}
                        step={1}
                        value={config.audio_text_enhancement_strength ?? 0}
                        onChange={(e) => onChange('audio_text_enhancement_strength', Number(e.target.value))}
                        disabled={disabled}
                        className="flex-1 h-6 appearance-none bg-transparent accent-primary cursor-pointer [&::-webkit-slider-runnable-track]:h-1.5 [&::-webkit-slider-runnable-track]:rounded-full [&::-webkit-slider-runnable-track]:bg-muted"
                      />
                      <span className="text-xs font-bold text-foreground w-24 text-right">
                        {ENHANCEMENT_LABELS[config.audio_text_enhancement_strength ?? 0]?.label ?? 'Off'}
                      </span>
                    </div>
                    <p className="text-[10px] text-muted-foreground leading-snug">
                      {ENHANCEMENT_LABELS[config.audio_text_enhancement_strength ?? 0]?.desc}
                    </p>
                    {(config.audio_text_enhancement_strength ?? 0) >= 4 && (
                      <div className="text-[10px] text-amber-600 dark:text-amber-400 p-2 rounded-md border border-amber-500/20 bg-amber-500/5 leading-snug">
                        Higher levels may paraphrase speech. Raw transcript and source refs preserved for audit.
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="space-y-2">
                <ToggleChip
                  label="Improve Document Structure"
                  checked={config.audio_structural_enhancement_enabled ?? false}
                  onChange={(v) => onChange('audio_structural_enhancement_enabled', v)}
                  disabled={disabled}
                />
                {config.audio_structural_enhancement_enabled && (
                  <div className="pl-3 border-l-2 border-primary/20 space-y-2 mt-2 animate-fade-in">
                    <SectionLabel label="Structure Mode" help="How to reorganize the transcript into a document." />
                    <Select
                      value={config.audio_structural_enhancement_mode ?? 'auto'}
                      onChange={(val) => onChange('audio_structural_enhancement_mode', val as AudioStructuralMode)}
                      options={STRUCTURAL_MODE_OPTIONS}
                      disabled={disabled}
                      className="w-full"
                    />
                    {!config.audio_text_enhancement_enabled && (
                      <p className="text-[10px] text-blue-600 dark:text-blue-400 leading-snug">
                        Structural-only mode will reorganize the transcript but will not rewrite transcript words.
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </CollapsibleSection>

          {/* ─── 7. CONTEXT & FUSION ─── */}
          <CollapsibleSection
            icon={Combine}
            title="Context & Fusion"
          >
            <div className="space-y-3">
              <div className="space-y-1.5">
                <SectionLabel label="Batch Context" help="Background context for multi-audio batches. Transcript evidence stays authoritative." />
                <Input
                  value={config.audio_context ?? ''}
                  onChange={(e) => onChange('audio_context', e.target.value)}
                  placeholder="optional context for batch processing"
                  disabled={disabled}
                  className="bg-background/50 h-9 text-xs"
                />
              </div>
              <ToggleChip
                label="Contradiction Detection"
                checked={config.audio_contradiction_detection ?? false}
                onChange={(v) => onChange('audio_contradiction_detection', v)}
                disabled={disabled}
              />
              <p className="text-[10px] text-muted-foreground/70 leading-snug">
                When enabled, contradictory claims across segments and speakers are surfaced with source refs. The system will not auto-resolve conflicts.
              </p>
            </div>
          </CollapsibleSection>

          {/* ─── 8. PRIVACY & PROVIDERS ─── */}
          <CollapsibleSection
            icon={Shield}
            title="Privacy & Providers"
          >
            <div className="space-y-3">
              <ToggleChip
                label="Allow Cloud STT"
                checked={config.audio_allow_cloud_stt ?? false}
                onChange={(v) => onChange('audio_allow_cloud_stt', v)}
                disabled={disabled}
              />
              <ToggleChip
                label="Allow Cloud Enhancement"
                checked={config.audio_enhancement_allow_cloud ?? false}
                onChange={(v) => onChange('audio_enhancement_allow_cloud', v)}
                disabled={disabled || !cloudEnhancementAvailable}
              />
              {!cloudEnhancementAvailable && (
                <ProviderWarning message="Cloud transcript enhancement is not shipped in this build. Enhancement uses local deterministic source-bound notes only." />
              )}
              {(config.audio_allow_cloud_stt || config.audio_enhancement_allow_cloud) && (
                <div className="text-[10px] text-amber-600 dark:text-amber-400 p-2 rounded-md border border-amber-500/20 bg-amber-500/5 leading-snug flex items-start gap-2">
                  <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                  <span>Audio data will leave your machine. Cloud usage is recorded in job metadata.</span>
                </div>
              )}
              <div className="flex items-center gap-2 pt-1">
                {isCloud ? (
                  <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-amber-600 dark:text-amber-400">
                    <Cloud className="w-3 h-3" /> Cloud provider selected
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-emerald-600 dark:text-emerald-400">
                    <HardDrive className="w-3 h-3" /> Local processing only
                  </span>
                )}
              </div>
            </div>
          </CollapsibleSection>

          {/* ─── 9. BENCHMARK ─── */}
          <CollapsibleSection
            icon={GitCompare}
            title="Benchmark / Compare"
          >
            <div className="space-y-3">
              <ToggleChip
                label="Compare Providers"
                checked={config.audio_benchmark_compare ?? false}
                onChange={(v) => onChange('audio_benchmark_compare', v)}
                disabled={disabled || !canCompareProviders}
              />
              {!canCompareProviders && (
                <ProviderWarning message="Provider comparison requires at least two shipped transcription adapters. This build only has one usable adapter." />
              )}
              <p className="text-[10px] text-muted-foreground/70 leading-snug">
                When enabled, the primary and comparison providers both transcribe the same audio. A comparison report (latency, confidence, vocabulary hits, segment count) is attached to job metadata.
              </p>
            </div>
          </CollapsibleSection>
        </div>
      )}
    </div>
  )
}

// ─── Collapsible Section ───────────────────────────────────────────

function CollapsibleSection({
  icon: Icon,
  title,
  badge,
  defaultOpen = false,
  children,
}: {
  icon: typeof Mic
  title: string
  badge?: string
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="rounded-xl border border-border/30 bg-card/20 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-3.5 py-2.5 hover:bg-muted/20 transition-colors"
      >
        <Icon className="w-3.5 h-3.5 text-primary/80" />
        <span className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase flex-1 text-left">
          {title}
        </span>
        {badge && (
          <span className="text-[9px] font-semibold uppercase tracking-wider text-amber-500 bg-amber-500/10 px-1.5 py-0.5 rounded">
            {badge}
          </span>
        )}
        <ChevronDown className={cn('w-3.5 h-3.5 text-muted-foreground/60 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="px-3.5 pb-3.5 pt-1 animate-fade-in">
          {children}
        </div>
      )}
    </div>
  )
}

// ─── Section Label ─────────────────────────────────────────────────

function SectionLabel({ label, help }: { label: string; help: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
        {label}
      </label>
      <HelpBubble text={help} />
    </div>
  )
}

function HelpBubble({ text }: { text: string }) {
  return (
    <div className="group relative">
      <HelpCircle className="w-3.5 h-3.5 text-muted-foreground/60 hover:text-muted-foreground cursor-help" />
      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block w-48 p-2 rounded-lg bg-slate-900 dark:bg-slate-800 text-[10px] leading-normal text-slate-100 shadow-lg border border-slate-800/80 z-30 pointer-events-none text-left">
        {text}
      </div>
    </div>
  )
}

// ─── Toggle Chip (compact inline toggle) ───────────────────────────

function ToggleChip({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string
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
        'inline-flex items-center gap-2 py-1.5 px-2.5 rounded-lg transition-all text-left',
        'hover:bg-muted/30',
        disabled && 'opacity-50 pointer-events-none'
      )}
    >
      <div
        className={cn(
          'w-8 h-[18px] rounded-full transition-colors relative shrink-0 border border-border/10',
          checked ? 'bg-primary' : 'bg-muted'
        )}
      >
        <div
          className={cn(
            'absolute top-[2px] w-3 h-3 rounded-full bg-white shadow-sm transition-transform duration-200',
            checked ? 'left-[15px]' : 'left-[2px]'
          )}
        />
      </div>
      <span className="text-xs font-medium text-foreground">{label}</span>
    </button>
  )
}

// ─── Warning Badges ────────────────────────────────────────────────

function ProviderWarning({ message }: { message: string }) {
  return (
    <div className="text-[10px] text-amber-600 dark:text-amber-400 p-2 rounded-md border border-amber-500/20 bg-amber-500/5 leading-snug mt-2 flex items-start gap-2">
      <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
      <span>{message}</span>
    </div>
  )
}

function CloudBadge() {
  return (
    <span className="inline-flex items-center gap-1 text-[9px] font-semibold text-amber-500 bg-amber-500/10 px-1.5 py-0.5 rounded mt-1">
      <Cloud className="w-3 h-3" />
      Cloud provider — audio leaves your machine
    </span>
  )
}
