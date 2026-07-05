import { useEffect, useState, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { Eye, X, ChevronDown, ChevronRight, Image as ImageIcon, FileText, Zap, Clock, Hash, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { getLlmTraces, type LlmTrace } from '@/lib/api'
import { cn } from '@/lib/utils'

interface LlmTraceViewerProps {
  open: boolean
  jobId?: string
  filename: string
  /** Stop polling once the job is no longer running. */
  isRunning: boolean
  onClose: () => void
  /** Poll interval in ms (default 2000). Lower for tests. */
  pollIntervalMs?: number
}

export function LlmTraceViewer({ open, jobId, filename, isRunning, onClose, pollIntervalMs = 2000 }: LlmTraceViewerProps) {
  const [traces, setTraces] = useState<LlmTrace[]>([])
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [viewMode, setViewMode] = useState<'raw' | 'rendered'>('raw')
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchTraces = useCallback(async () => {
    if (!jobId) return
    try {
      const data = await getLlmTraces(jobId)
      setTraces(data.traces)
    } catch {
      // Silent: viewer is best-effort.
    } finally {
      setLoading(false)
    }
  }, [jobId])

  // Initial load + polling while the job is running.
  useEffect(() => {
    if (!open || !jobId) return
    setLoading(true)
    fetchTraces()

    const poll = () => {
      fetchTraces().finally(() => {
        if (isRunning) {
          pollRef.current = setTimeout(poll, pollIntervalMs)
        }
      })
    }
    if (isRunning) {
      pollRef.current = setTimeout(poll, pollIntervalMs)
    }
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [open, jobId, isRunning, fetchTraces, pollIntervalMs])

  const toggle = (idx: number) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })
  }

  const expandAll = () => setExpanded(new Set(traces.map((_, i) => i)))
  const collapseAll = () => setExpanded(new Set())

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center p-4 bg-black/65 backdrop-blur-sm animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="llm-trace-title"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-6xl h-[85vh] bg-background border border-border/60 rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 px-6 py-4 border-b border-border/20 shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <div className="p-2 rounded-lg bg-primary/10 text-primary shrink-0">
              <Eye className="w-5 h-5" />
            </div>
            <div className="min-w-0">
              <h3 id="llm-trace-title" className="font-extrabold text-base text-foreground uppercase tracking-wider">
                LLM Call Inspector
              </h3>
              <p className="text-[11px] text-muted-foreground mt-0.5 truncate">
                {filename}
                {jobId && <span className="font-mono ml-2 opacity-60">{jobId.slice(0, 12)}...</span>}
                {isRunning && <span className="ml-2 text-primary">live</span>}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {traces.length > 0 && (
              <>
                <div className="flex rounded-lg border border-border/40 overflow-hidden">
                  <button
                    type="button"
                    onClick={() => setViewMode('raw')}
                    className={cn(
                      'px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider transition-colors',
                      viewMode === 'raw' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
                    )}
                  >
                    Raw
                  </button>
                  <button
                    type="button"
                    onClick={() => setViewMode('rendered')}
                    className={cn(
                      'px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider transition-colors border-l border-border/40',
                      viewMode === 'rendered' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
                    )}
                  >
                    Rendered
                  </button>
                </div>
                <Button variant="ghost" size="sm" onClick={expandAll} className="h-8 text-[10px] font-bold uppercase tracking-wider">
                  Expand All
                </Button>
                <Button variant="ghost" size="sm" onClick={collapseAll} className="h-8 text-[10px] font-bold uppercase tracking-wider">
                  Collapse
                </Button>
              </>
            )}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors shrink-0"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading && traces.length === 0 ? (
            <div className="flex items-center justify-center h-full text-muted-foreground text-sm gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              Loading traces...
            </div>
          ) : traces.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
              <Eye className="w-10 h-10 mb-3 opacity-40" />
              <p className="text-sm font-semibold">No LLM calls captured yet</p>
              <p className="text-[11px] mt-1 max-w-sm text-center leading-relaxed">
                Traces appear here as the converter refines tables, equations, and other
                blocks with the LLM. {isRunning ? 'Waiting for the first call...' : 'Run a job with LLM enabled.'}
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {traces.map((t, i) => (
                <TraceCard
                  key={t.index}
                  trace={t}
                  expanded={expanded.has(i)}
                  onToggle={() => toggle(i)}
                  viewMode={viewMode}
                />
              ))}
            </div>
          )}
        </div>

        {/* Footer summary */}
        {traces.length > 0 && (
          <div className="shrink-0 px-6 py-3 border-t border-border/20 bg-muted/10 flex items-center justify-between text-[10px] text-muted-foreground font-mono">
            <span>
              {traces.length} call{traces.length !== 1 ? 's' : ''} |
              {' '}{traces.filter((t) => t.cache_hit).length} cached |
              {' '}{traces.filter((t) => t.status >= 400).length} failed
            </span>
            <span>
              {traces.reduce((a, t) => a + t.elapsed_ms, 0)}ms total
            </span>
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}

interface TraceCardProps {
  trace: LlmTrace
  expanded: boolean
  onToggle: () => void
  viewMode: 'raw' | 'rendered'
}

function TraceCard({ trace, expanded, onToggle, viewMode }: TraceCardProps) {
  const ok = trace.status >= 200 && trace.status < 300
  const time = new Date(trace.ts * 1000).toLocaleTimeString([], { hour12: false })

  return (
    <div className={cn(
      'rounded-xl border overflow-hidden transition-colors',
      ok ? 'border-border/40 bg-card/30' : 'border-rose-500/30 bg-rose-500/5'
    )}>
      {/* Card header: click to expand */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-muted/30 transition-colors"
      >
        {expanded ? <ChevronDown className="w-4 h-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 shrink-0 text-muted-foreground" />}
        <span className="font-mono text-[10px] text-muted-foreground/60 shrink-0">#{trace.index + 1}</span>
        <span className={cn(
          'px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wide shrink-0',
          trace.cache_hit
            ? 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400'
            : ok
              ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'bg-rose-500/10 text-rose-600 dark:text-rose-400'
        )}>
          {trace.cache_hit ? 'CACHE' : `HTTP ${trace.status}`}
        </span>
        <span className="text-[11px] font-mono text-foreground/80 truncate">
          {trace.model || trace.host}
        </span>
        <span className="flex items-center gap-1 text-[10px] text-muted-foreground shrink-0">
          {trace.image_count > 0 && (
            <span className="flex items-center gap-0.5" title={`${trace.image_count} image(s) sent`}>
              <ImageIcon className="w-3 h-3" />{trace.image_count}
            </span>
          )}
          <span className="flex items-center gap-0.5" title="prompt chars">
            <FileText className="w-3 h-3" />{trace.prompt_chars}
          </span>
        </span>
        <span className="ml-auto flex items-center gap-3 text-[10px] text-muted-foreground shrink-0 font-mono">
          {trace.elapsed_ms > 0 && (
            <span className="flex items-center gap-0.5" title="elapsed">
              <Clock className="w-3 h-3" />{trace.elapsed_ms}ms
            </span>
          )}
          <span className="flex items-center gap-0.5" title="time">
            <Hash className="w-3 h-3" />{time}
          </span>
        </span>
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="px-4 pb-4 pt-1 space-y-4 border-t border-border/20">
          {/* Request parts */}
          <div>
            <h4 className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70 mb-2 mt-3 flex items-center gap-1.5">
              <Zap className="w-3 h-3" /> Sent to LLM
            </h4>
            <div className="space-y-3">
              {trace.parts.map((part, pi) => (
                <PartView key={pi} part={part} viewMode={viewMode} />
              ))}
            </div>
          </div>

          {/* Response */}
          <div>
            <h4 className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70 mb-2 mt-3 flex items-center gap-1.5">
              <FileText className="w-3 h-3" /> Received
            </h4>
            <ResponseView trace={trace} viewMode={viewMode} />
          </div>
        </div>
      )}
    </div>
  )
}

function PartView({ part, viewMode }: { part: import('@/lib/api').LlmTracePart; viewMode: 'raw' | 'rendered' }) {
  if (part.type === 'image') {
    if (part.truncated) {
      return (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-300">
          {part.note || 'Image skipped (too large)'}
        </div>
      )
    }
    return (
      <div className="rounded-lg border border-border/30 bg-background/50 p-2 inline-block">
        <img
          src={part.data_url}
          alt="LLM input"
          className="max-w-full max-h-80 rounded object-contain"
        />
        <p className="text-[9px] text-muted-foreground/60 font-mono mt-1.5">
          {part.mime} | {((part.size_bytes ?? 0) * 0.75 / 1024).toFixed(1)} KB
        </p>
      </div>
    )
  }

  // Text part
  const text = part.text || ''
  // Try to render HTML tables when in rendered mode.
  const hasTable = /<table[\s>]/i.test(text)

  if (viewMode === 'rendered' && hasTable) {
    const tableMatch = text.match(/<table[\s\S]*?<\/table>/i)
    if (tableMatch) {
      return (
        <div className="space-y-2">
          <div className="rounded-lg border border-border/30 bg-background/50 p-3 overflow-x-auto">
            <table
              className="text-[11px] border-collapse"
              style={{ tableLayout: 'auto' }}
              dangerouslySetInnerHTML={{ __html: sanitizeTableHtml(tableMatch[0]) }}
            />
          </div>
          <details className="text-[10px] text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">Full prompt ({text.length} chars)</summary>
            <pre className="mt-2 p-3 rounded-lg bg-muted/30 text-[10px] whitespace-pre-wrap break-words font-mono text-muted-foreground max-h-60 overflow-y-auto">
              {text}
            </pre>
          </details>
        </div>
      )
    }
  }

  return (
    <pre className="p-3 rounded-lg bg-muted/30 text-[11px] whitespace-pre-wrap break-words font-mono text-foreground/80 max-h-72 overflow-y-auto">
      {text}
    </pre>
  )
}

function ResponseView({ trace, viewMode }: { trace: LlmTrace; viewMode: 'raw' | 'rendered' }) {
  const text = trace.response || ''
  // The response is typically JSON like {"corrected_html": "<table>...</table>"}
  let correctedHtml = ''
  if (viewMode === 'rendered') {
    try {
      const parsed = JSON.parse(text)
      correctedHtml = parsed.corrected_html || parsed.html || ''
    } catch {
      // Not JSON: fall through to raw.
    }
  }

  if (viewMode === 'rendered' && correctedHtml && /<table[\s>]/i.test(correctedHtml)) {
    const tableMatch = correctedHtml.match(/<table[\s\S]*?<\/table>/i)
    if (tableMatch) {
      return (
        <div className="space-y-2">
          <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3 overflow-x-auto">
            <table
              className="text-[11px] border-collapse"
              dangerouslySetInnerHTML={{ __html: sanitizeTableHtml(tableMatch[0]) }}
            />
          </div>
          <details className="text-[10px] text-muted-foreground">
            <summary className="cursor-pointer hover:text-foreground">Raw response ({text.length} chars)</summary>
            <pre className="mt-2 p-3 rounded-lg bg-muted/30 text-[10px] whitespace-pre-wrap break-words font-mono text-muted-foreground max-h-60 overflow-y-auto">
              {text}
            </pre>
          </details>
        </div>
      )
    }
  }

  return (
    <pre className={cn(
      'p-3 rounded-lg text-[11px] whitespace-pre-wrap break-words font-mono max-h-72 overflow-y-auto',
      trace.status >= 400 ? 'bg-rose-500/5 text-rose-600 dark:text-rose-400' : 'bg-muted/30 text-foreground/80'
    )}>
      {text || '[empty response]'}
    </pre>
  )
}

// Strip script/style/event handlers from table HTML before rendering.
function sanitizeTableHtml(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/\son\w+="[^"]*"/gi, '')
    .replace(/\son\w+='[^']*'/gi, '')
    .replace(/javascript:/gi, '')
}
