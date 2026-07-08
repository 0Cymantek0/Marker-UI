import { useState, useCallback, useMemo } from 'react'
import { Download, Copy, Check, FileText, Code, Braces, Eye, FileSpreadsheet, Loader2, Mic, AlertTriangle, type LucideIcon } from 'lucide-react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { ImageUnderstandingBadge } from '@/components/features/image-understanding/ImageUnderstandingBadge'
import type { ImageUnderstandingMeta } from '@/lib/api'

type OutputTab = 'markdown' | 'html' | 'json' | 'chunks' | 'raw' | 'audio'
type JsonRecord = Record<string, unknown>

const ALL_TABS: { value: OutputTab; label: string; icon: LucideIcon; formatKey?: string }[] = [
  { value: 'markdown', label: 'Markdown', icon: FileText, formatKey: 'markdown' },
  { value: 'html', label: 'HTML', icon: Code, formatKey: 'html' },
  { value: 'json', label: 'JSON', icon: Braces, formatKey: 'json' },
  { value: 'chunks', label: 'Chunks', icon: Braces, formatKey: 'chunks' },
  { value: 'raw', label: 'Raw Text', icon: Eye },
  { value: 'audio', label: 'Audio', icon: Mic },
]

interface OutputViewerProps {
  content: string | null
  formats?: Record<string, string> | null
  availableFormats?: string[]
  regeneratableFormats?: string[]
  onRegenerate?: (format: string) => Promise<void>
  onDownload: (format: string) => void
  imageUnderstanding?: ImageUnderstandingMeta[] | null
  audioMetadata?: JsonRecord | null
  filename?: string
  jobId?: string
}

export function OutputViewer({
  content,
  formats = null,
  availableFormats = [],
  regeneratableFormats = [],
  onRegenerate,
  onDownload,
  imageUnderstanding,
  audioMetadata,
  jobId,
}: OutputViewerProps) {
  const [activeTab, setActiveTab] = useState<OutputTab>('markdown')
  const [copied, setCopied] = useState(false)
  const [regenerating, setRegenerating] = useState<string | null>(null)

  const metaByFilename = useMemo(() => {
    const m = new Map<string, { meta: ImageUnderstandingMeta; index: number }>()
    ;(imageUnderstanding ?? []).forEach((meta, i) => {
      m.set(meta.image_name, { meta, index: i + 1 })
    })
    return m
  }, [imageUnderstanding])
  const metaTotal = metaByFilename.size

  const markdownComponents = useMemo<Components>(() => ({
    pre: ({ children }) => (
      <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed bg-transparent p-0 select-text">
        {children}
      </pre>
    ),
    code: ({ className, children, ...props }) => (
      <code className={cn('font-mono text-xs', className)} {...props}>
        {children}
      </code>
    ),
    img: ({ src, alt, ...props }) => {
      const safeSrc = safeMarkdownImageSrc(src)
      if (!safeSrc) {
        const blockedSrc = String(src ?? '').trim()
        return (
          <span
            role="note"
            aria-label="External image blocked for privacy"
            className="not-prose inline-flex max-w-full items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs font-sans text-amber-800 dark:text-amber-200"
            title={blockedSrc}
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">External image blocked</span>
            {blockedSrc && <code className="max-w-[220px] truncate font-mono">{blockedSrc}</code>}
          </span>
        )
      }
      const imageSrc = markdownAssetSrc(safeSrc, jobId)
      const imageFilename = (safeSrc.split(/[?#]/, 1)[0] ?? '').split('/').pop() ?? ''
      const entry = metaByFilename.get(imageFilename)
      return (
        <span className="relative inline-block align-middle my-1">
          <img src={imageSrc} alt={alt} {...props} />
          {entry && (
            <ImageUnderstandingBadge
              meta={entry.meta}
              index={entry.index}
              total={metaTotal}
            />
          )}
        </span>
      )
    },
  }), [jobId, metaByFilename, metaTotal])

  const regeneratableFormatSet = useMemo(
    () => new Set(regeneratableFormats),
    [regeneratableFormats],
  )

  const visibleTabs = useMemo(() => {
    return ALL_TABS.filter((tab) => {
      if (tab.value === 'audio') return !!audioMetadata
      if (tab.value === 'raw') return true
      if (!tab.formatKey) return true
      if (tab.value === 'markdown') return true
      if (availableFormats.includes(tab.formatKey) || !!formats?.[tab.formatKey]) return true
      return !!onRegenerate && regeneratableFormatSet.has(tab.formatKey)
    })
  }, [availableFormats, formats, onRegenerate, audioMetadata, regeneratableFormatSet])

  const activeContent = useMemo(() => {
    if (activeTab === 'raw') return content
    const fmtKey = visibleTabs.find((t) => t.value === activeTab)?.formatKey
    if (fmtKey && formats?.[fmtKey]) return formats[fmtKey]
    return content
  }, [activeTab, formats, content, visibleTabs])

  const downloadFormat = useMemo(() => {
    return visibleTabs.find((t) => t.value === activeTab)?.formatKey ?? 'markdown'
  }, [activeTab, visibleTabs])

  const copyToClipboard = useCallback(async () => {
    if (!activeContent) return
    await navigator.clipboard.writeText(activeContent)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [activeContent])

  const handleTabClick = useCallback(async (tab: OutputTab) => {
    setActiveTab(tab)
    const fmtKey = visibleTabs.find((t) => t.value === tab)?.formatKey
    if (!fmtKey) return
    if (formats?.[fmtKey]) return
    if (regenerating) return
    if (!onRegenerate) return

    setRegenerating(fmtKey)
    try {
      await onRegenerate(fmtKey)
    } finally {
      setRegenerating(null)
    }
  }, [formats, regenerating, onRegenerate, visibleTabs])

  if (!content && !formats) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center glass-card border border-border/40 min-h-[300px]">
        <div className="flex items-center justify-center w-12 h-12 rounded-full bg-muted text-muted-foreground/40 mb-4 select-none">
          <FileSpreadsheet className="w-6 h-6 stroke-[1.5]" />
        </div>
        <p className="text-sm font-semibold text-muted-foreground">Converted output will appear here</p>
        <p className="text-xs text-muted-foreground/60 mt-1 max-w-[280px] leading-relaxed">
          Upload a source document and press Convert to stream logs and generate your output.
        </p>
      </div>
    )
  }

  const isTabAvailable = (tab: typeof ALL_TABS[number]) => {
    if (!tab.formatKey) return true
    if (availableFormats.includes(tab.formatKey)) return true
    return !!formats?.[tab.formatKey]
  }

  const isTabRegenerating = (tab: typeof ALL_TABS[number]) => {
    return tab.formatKey === regenerating
  }

  return (
    <div className="glass-card border border-border/40 overflow-hidden animate-fade-in shadow-sm flex flex-col h-[400px]">

      {/* Tab bar header */}
      <div className="flex items-center justify-between border-b border-border/30 px-2 bg-muted/20">
        <div className="flex gap-1 py-1">
          {visibleTabs.map((tab) => {
            const isActive = activeTab === tab.value
            const available = isTabAvailable(tab)
            const loading = isTabRegenerating(tab)
            return (
              <button
                key={tab.value}
                type="button"
                onClick={() => handleTabClick(tab.value)}
                disabled={loading}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all duration-200',
                  isActive
                    ? 'bg-card text-foreground shadow-sm border border-border/30'
                    : available
                      ? 'text-muted-foreground hover:text-foreground hover:bg-muted/30'
                      : 'text-muted-foreground/40 hover:text-muted-foreground/60 hover:bg-muted/20'
                )}
              >
                {loading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <tab.icon className="w-3.5 h-3.5" />
                )}
                {tab.label}
              </button>
            )
          })}
        </div>

        <div className="flex items-center gap-1.5 py-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={copyToClipboard}
            className="h-8 px-2.5 rounded-lg text-xs font-semibold hover:bg-muted/50 transition-colors"
          >
            {copied ? (
              <Check className="w-3.5 h-3.5 text-emerald-500 mr-1.5" />
            ) : (
              <Copy className="w-3.5 h-3.5 text-muted-foreground mr-1.5" />
            )}
            <span>{copied ? 'Copied' : 'Copy'}</span>
          </Button>

          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDownload(downloadFormat)}
            className="h-8 px-2.5 rounded-lg text-xs font-semibold hover:bg-muted/50 transition-colors"
          >
            <Download className="w-3.5 h-3.5 text-muted-foreground mr-1.5" />
            <span>Download</span>
          </Button>
        </div>
      </div>

      {/* Content panel */}
      <div className={cn('flex-1 p-4 overflow-auto font-mono text-xs leading-relaxed text-foreground bg-muted/10 border border-border/40')}>
        {regenerating === ALL_TABS.find((t) => t.value === activeTab)?.formatKey ? (
          <div className="flex flex-col items-center justify-center h-full gap-3">
            <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
            <p className="text-xs text-muted-foreground font-semibold">Regenerating {activeTab} format...</p>
          </div>
        ) : (
          <>
            {activeTab === 'markdown' && (
              <div className="prose prose-sm dark:prose-invert max-w-none">
                {imageUnderstanding && imageUnderstanding.length > 0 && (
                  <div className="not-prose flex flex-wrap items-center gap-x-6 gap-y-3 p-4 mb-5 rounded-2xl bg-slate-50 dark:bg-slate-900/40 border border-slate-200/60 dark:border-slate-800/60 shadow-sm">
                    <span className="text-xs font-extrabold tracking-widest text-slate-400 dark:text-slate-500 uppercase w-full mb-0.5">
                      VLM Processed Images ({imageUnderstanding.length})
                    </span>
                    {imageUnderstanding.map((meta, i) => (
                      <div key={meta.image_name} className="flex items-center gap-2 pr-4 border-r border-slate-200 dark:border-slate-800 last:border-0">
                        <span className="text-xs font-mono text-muted-foreground max-w-[120px] truncate" title={meta.image_name}>
                          {meta.image_name}
                        </span>
                        <ImageUnderstandingBadge
                          meta={meta}
                          index={i + 1}
                          total={imageUnderstanding.length}
                          inline
                        />
                      </div>
                    ))}
                  </div>
                )}
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={markdownComponents}
                >
                  {activeContent ?? ''}
                </ReactMarkdown>
              </div>
            )}
            {activeTab === 'html' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text">
                {activeContent}
              </pre>
            )}
            {activeTab === 'json' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text">
                {activeContent}
              </pre>
            )}
            {activeTab === 'chunks' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text">
                {activeContent}
              </pre>
            )}
            {activeTab === 'raw' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text text-slate-700 dark:text-slate-300">
                {activeContent}
              </pre>
            )}
            {activeTab === 'audio' && audioMetadata && (
              <AudioInspectionPanel audio={audioMetadata} />
            )}
          </>
        )}
      </div>
    </div>
  )
}

function safeMarkdownImageSrc(src: unknown): string | null {
  const raw = String(src ?? '').trim()
  if (!raw) return null

  if ([...raw].some((ch) => ch === '<' || ch === '>' || ch.charCodeAt(0) < 32)) return null
  if (/^(?:https?:)?\/\//i.test(raw)) return null
  if (/^[\\/]/.test(raw)) return null

  const protocolMatch = raw.match(/^([a-z][a-z0-9+.-]*):/i)
  if (protocolMatch) return null

  const pathPart = raw.split(/[?#]/, 1)[0] ?? ''
  if (!pathPart) return null
  let decodedPath: string
  try {
    decodedPath = decodeURIComponent(pathPart)
  } catch {
    return null
  }
  if (decodedPath.includes('\\') || decodedPath.startsWith('/')) return null
  const parts = decodedPath.split('/')
  if (parts.some((part) => part === '' || part === '.' || part === '..')) return null
  if (!/\.(?:png|jpe?g|gif|webp|bmp|tiff?)$/i.test(parts[parts.length - 1] ?? '')) return null
  return raw
}

function markdownAssetSrc(src: string, jobId?: string): string {
  const raw = String(src ?? '').trim()
  if (!jobId) return raw
  const pathPart = raw.split(/[?#]/, 1)[0] ?? ''
  if (!pathPart) return raw
  const encodedPath = pathPart
    .split('/')
    .map((part) => {
      try {
        return encodeURIComponent(decodeURIComponent(part))
      } catch {
        return encodeURIComponent(part)
      }
    })
    .join('/')
  return `/api/convert/assets/${encodeURIComponent(jobId)}/${encodedPath}`
}

function AudioInspectionPanel({ audio }: { audio: JsonRecord }) {
  const transcript = asRecord(audio.transcript)
  const providerCapability = asRecord(audio.provider_capability)
  const quality = asRecord(audio.quality ?? transcript.risk_summary)
  const segments = asRecordArray(transcript.segments)
  const speakersMeta = asRecord(audio.speakers)
  const speakers = asRecordArray(speakersMeta.timeline)
  const vocabulary = asRecord(audio.vocabulary)
  const transcriptWarnings = stringList(transcript.warnings)
  const lowConfidenceCount = numberValue(quality.low_confidence_count) ?? 0
  const unknownConfidenceCount = numberValue(quality.unknown_confidence_count) ?? 0
  const reviewRequired = quality.review_required === true
  const requestedVocabularyCount = numberValue(vocabulary.requested_count) ?? 0
  const detectedVocabularyCount = numberValue(vocabulary.detected_count) ?? 0
  const detectedVocabulary = stringList(vocabulary.detected)
  const missedVocabulary = stringList(vocabulary.likely_missed)

  return (
    <div className="space-y-4 font-sans text-xs">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <AudioMetric label="Provider" value={displayValue(transcript.provider ?? providerCapability.provider_id)} />
        <AudioMetric label="Model" value={displayValue(transcript.model)} />
        <AudioMetric label="Segments" value={String(segments.length)} />
        <AudioMetric label="Review" value={reviewRequired ? 'required' : 'not required'} tone={reviewRequired ? 'warn' : 'ok'} />
      </div>

      {(lowConfidenceCount > 0 || unknownConfidenceCount > 0 || transcriptWarnings.length > 0) && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/25 bg-amber-500/5 p-3 text-amber-700 dark:text-amber-300">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-bold">Audio review suggested</div>
            <div className="mt-0.5 text-xs">
              Low confidence: {lowConfidenceCount}; unknown confidence: {unknownConfidenceCount}
            </div>
          </div>
        </div>
      )}

      {speakers.length > 0 && (
        <section className="space-y-2">
          <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Speaker Timeline</h4>
          <div className="flex flex-wrap gap-2">
            {speakers.map((speaker, index) => {
              const speakerId = displayValue(speaker.speaker, `speaker_${index}`)
              return (
                <span key={`${speakerId}-${index}`} className="rounded-lg border border-border/40 bg-card/40 px-2.5 py-1.5">
                  <span className="font-semibold">{displayValue(speaker.display_label, speakerId)}</span>
                  <span className="ml-2 text-muted-foreground">{displayValue(speaker.segment_count, '0')} segments</span>
                </span>
              )
            })}
          </div>
        </section>
      )}

      {(requestedVocabularyCount > 0 || detectedVocabularyCount > 0) && (
        <section className="space-y-2">
          <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Vocabulary</h4>
          <div className="rounded-lg border border-border/40 bg-card/30 p-3">
            <div>Requested: {requestedVocabularyCount}; detected: {detectedVocabularyCount}</div>
            {detectedVocabulary.length > 0 && (
              <div className="mt-1 text-emerald-700 dark:text-emerald-300">Hits: {detectedVocabulary.join(', ')}</div>
            )}
            {missedVocabulary.length > 0 && (
              <div className="mt-1 text-muted-foreground">Likely missed: {missedVocabulary.join(', ')}</div>
            )}
          </div>
        </section>
      )}

      <section className="space-y-2">
        <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground">Confidence Timeline</h4>
        <div className="space-y-2">
          {segments.length === 0 ? (
            <div className="text-muted-foreground">No audio segments in metadata.</div>
          ) : segments.map((segment, index) => {
            const segmentWarnings = stringList(segment.warnings)
            const confidence = numberValue(segment.confidence)
            const tone = confidenceTone(confidence, segmentWarnings)
            const segmentId = displayValue(segment.segment_id, `segment_${index + 1}`)
            return (
              <div
                key={`${segmentId}-${index}`}
                className={cn(
                  'rounded-lg border p-3 bg-card/30',
                  tone === 'low'
                    ? 'border-red-500/35'
                    : tone === 'unknown'
                      ? 'border-slate-400/35'
                      : 'border-emerald-500/25'
                )}
              >
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <code>{formatMs(segment.start_ms)}-{formatMs(segment.end_ms)}</code>
                  <span>{displayValue(segment.speaker, 'speaker_0')}</span>
                  <span>{segmentId}</span>
                  <span>confidence {confidence == null ? 'unknown' : confidence.toFixed(2)}</span>
                  {segmentWarnings.length > 0 && (
                    <span className="text-amber-600 dark:text-amber-300">{segmentWarnings.join(', ')}</span>
                  )}
                </div>
                <div className="mt-2 leading-relaxed text-foreground">{displayValue(segment.text, '')}</div>
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}

function AudioMetric({ label, value, tone }: { label: string; value: string; tone?: 'ok' | 'warn' }) {
  return (
    <div className="rounded-lg border border-border/40 bg-card/30 p-3 min-w-0">
      <div className="text-xs font-bold uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={cn('mt-1 truncate font-semibold', tone === 'ok' && 'text-emerald-600 dark:text-emerald-400', tone === 'warn' && 'text-amber-600 dark:text-amber-400')}>
        {value}
      </div>
    </div>
  )
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asRecord(value: unknown): JsonRecord {
  return isRecord(value) ? value : {}
}

function asRecordArray(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value
      .filter((item) => ['string', 'number', 'boolean'].includes(typeof item))
      .map(String)
    : []
}

function numberValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function displayValue(value: unknown, fallback = 'unknown'): string {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value === 'boolean') return String(value)
  return fallback
}

function confidenceTone(confidence: unknown, warnings: unknown): 'ok' | 'low' | 'unknown' {
  if (Array.isArray(warnings) && warnings.length > 0) return 'low'
  if (confidence == null) return 'unknown'
  return Number(confidence) < 0.65 ? 'low' : 'ok'
}

function formatMs(raw: unknown): string {
  const ms = Math.max(0, Number(raw) || 0)
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  const milli = Math.floor(ms % 1000)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(milli).padStart(3, '0')}`
}
