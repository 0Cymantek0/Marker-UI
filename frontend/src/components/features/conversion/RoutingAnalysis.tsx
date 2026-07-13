import { cn } from '@/lib/utils'
import { Cpu, AlertTriangle, ShieldCheck, Info, ChevronRight, Gauge, GitBranch } from 'lucide-react'
import type { ConversionMetadata, ConverterPlanResponse, MixedEngineSegment } from '@/lib/api'

interface RoutingAnalysisProps {
  plan: ConverterPlanResponse | ConversionMetadata | null
  title?: string
  className?: string
}

function isScoreUnsafe(key: string, value: number): boolean {
  if (key === 'text_layer_score') return value < 0.70
  if (key === 'text_quality_score') return value < 0.80
  if (key === 'scan_likelihood') return value > 0.20
  if (key === 'sandwich_likelihood') return value > 0.40
  if (key === 'visual_complexity_score') return value > 0.35
  if (key === 'layout_complexity_score') return value > 0.45
  return false
}

export function toRoutingPlan(plan: ConverterPlanResponse | ConversionMetadata | null): ConverterPlanResponse | null {
  if (!plan) return null
  if ('engine' in plan && typeof plan.engine === 'object' && plan.engine) {
    return {
      ...plan.engine,
      probe_result: plan.probe_result ?? plan.engine.probe_result ?? null,
      mixed_engine_segments: plan.mixed_engine_segments ?? plan.engine.mixed_engine_segments ?? null,
      preliminary: plan.engine.preliminary ?? false,
    }
  }
  if ('label' in plan) return plan as ConverterPlanResponse
  return null
}

const SCORE_LABELS: Record<string, string> = {
  text_layer_score: 'Text Layer Score',
  text_quality_score: 'Text Quality Score',
  scan_likelihood: 'Scan Likelihood',
  sandwich_likelihood: 'Sandwich Likelihood',
  visual_complexity_score: 'Visual Complexity',
  layout_complexity_score: 'Layout Complexity',
}

const ENGINE_LABELS: Record<string, string> = {
  liteparse_pdf: 'LiteParse',
  marker_pdf: 'Marker',
  mixed_pdf: 'Mixed',
}

function shortEngineName(engine?: string | null): string {
  if (!engine) return 'Unknown'
  return ENGINE_LABELS[engine] ?? engine
}

function formatPageRange(segment: MixedEngineSegment): string {
  if (segment.page_range) return segment.page_range
  if (Array.isArray(segment.pages) && segment.pages.length > 0) {
    return segment.pages.join(', ')
  }
  return 'Unknown'
}

export function RoutingAnalysis({ plan, title, className }: RoutingAnalysisProps) {
  const routingPlan = toRoutingPlan(plan)
  if (!routingPlan) return null

  const hasFallback = routingPlan.fallback_chain && routingPlan.fallback_chain.length > 0
  const hasReasons = routingPlan.reasons && routingPlan.reasons.length > 0
  const hasWarnings = routingPlan.warnings && routingPlan.warnings.length > 0
  const mixedSegments = Array.isArray(routingPlan.mixed_engine_segments) ? routingPlan.mixed_engine_segments : []
  const hasMixedSegments = mixedSegments.length > 0
  const showProbeGrid = !routingPlan.preliminary && routingPlan.probe_result && Object.keys(routingPlan.probe_result).length > 0

  return (
    <div className={cn('glass-card border border-border/30 rounded-xl p-4 space-y-4 shadow-sm bg-card/25', className)}>
      {title && (
        <div className="flex items-center gap-2 pb-2 border-b border-border/20">
          <Gauge className="w-4 h-4 text-primary" />
          <h4 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">{title}</h4>
        </div>
      )}

      {/* Engine & Execution Meta */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-3 bg-primary/10 border border-primary/20 rounded-xl px-3.5 py-2 shadow-sm">
          <ShieldCheck className="w-5 h-5 text-primary shrink-0" />
          <div className="flex flex-col justify-center gap-0.5">
            <span className="text-xs font-bold text-foreground leading-snug">{routingPlan.label}</span>
            {routingPlan.confidence > 0 && (
              <span className="text-xs text-muted-foreground font-mono leading-none">
                Confidence: {(routingPlan.confidence * 100).toFixed(0)}%
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1.5 bg-muted/60 border border-border/40 rounded-lg px-2.5 py-1 text-xs text-muted-foreground">
          <Cpu className="w-3.5 h-3.5" />
          <span>Backend: </span>
          <span className="font-bold text-foreground">
            {routingPlan.execution_backend === 'cpu_thread' ? 'CPU' : 'Marker worker'}
          </span>
        </div>

        {hasFallback && (
          <div className="flex items-center gap-1 bg-muted/40 border border-border/20 rounded-lg px-2.5 py-1 text-xs text-muted-foreground font-mono">
            <span>Fallback Chain: </span>
            {routingPlan.fallback_chain.map((item, idx) => (
              <span key={item} className="flex items-center">
                {idx > 0 && <ChevronRight className="w-3 h-3 mx-0.5 text-muted-foreground/45" />}
                <span className={cn('font-semibold', idx === routingPlan.fallback_chain.length - 1 ? 'text-primary' : 'text-foreground')}>{item}</span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Preliminary Warning Notice */}
      {routingPlan.preliminary && (
        <div className="flex gap-2.5 p-3 rounded-lg border border-primary/20 bg-primary/5 text-xs text-muted-foreground">
          <Info className="w-4.5 h-4.5 text-primary shrink-0 mt-0.5" />
          <div>
            <span className="font-bold text-foreground block mb-0.5">Preliminary Route Decision</span>
            File complexity has not been analyzed yet. The server will probe PDF bytes on upload/conversion. Full routing analysis and scores will be updated upon completion.
          </div>
        </div>
      )}

      {hasMixedSegments && (
        <div className="space-y-2" data-testid="mixed-routing-segments">
          <div className="flex items-center gap-1.5">
            <GitBranch className="w-3.5 h-3.5 text-primary" />
            <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">Page Segments</span>
          </div>
          <div className="overflow-hidden rounded-lg border border-border/30 bg-background/30">
            {mixedSegments.map((segment, index) => {
              const requested = shortEngineName(segment.requested_engine)
              const actual = shortEngineName(segment.actual_engine)
              const changedEngine = requested !== actual
              return (
                <div
                  key={`${formatPageRange(segment)}-${index}`}
                  className={cn(
                    'grid grid-cols-[minmax(4rem,0.8fr)_minmax(5rem,1fr)_minmax(0,2fr)] gap-2 px-3 py-2 text-xs items-center',
                    index > 0 && 'border-t border-border/20'
                  )}
                >
                  <span className="font-mono font-semibold text-foreground truncate" title={`Pages ${formatPageRange(segment)}`}>
                    Pages {formatPageRange(segment)}
                  </span>
                  <span className="font-semibold text-primary truncate" title={segment.actual_engine ?? actual}>
                    {actual}
                  </span>
                  <span className="text-xs text-muted-foreground truncate" title={segment.fallback_reason ?? (changedEngine ? `Requested ${requested}` : '')}>
                    {segment.fallback_reason ?? (changedEngine ? `Requested ${requested}` : 'Segment completed')}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Probe Scores Grid (suppressed when preliminary) */}
      {showProbeGrid && routingPlan.probe_result && (
        <div className="space-y-2">
          <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">PDF Probing Analysis</span>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2.5">
            {Object.entries(SCORE_LABELS).map(([key, label]) => {
              const val = routingPlan.probe_result?.[key]
              if (typeof val !== 'number') return null
              const isUnsafe = isScoreUnsafe(key, val)
              const formattedVal = `${(val * 100).toFixed(0)}%`

              return (
                <div
                  key={key}
                  className={cn(
                    'p-2.5 rounded-lg border text-left transition-all',
                    isUnsafe
                      ? 'border-rose-500/20 bg-rose-500/5 text-rose-700 dark:text-rose-400'
                      : 'border-border/30 bg-background/40 text-muted-foreground'
                  )}
                >
                  <span className="text-xs block leading-tight font-medium mb-1">{label}</span>
                  <div className="flex items-center gap-1">
                    <span className={cn('text-sm font-extrabold font-mono', isUnsafe ? 'text-rose-600 dark:text-rose-400' : 'text-foreground/90')}>
                      {formattedVal}
                    </span>
                    {isUnsafe && (
                      <span title="Outside safe LiteParse threshold">
                        <AlertTriangle className="w-3.5 h-3.5 text-rose-500 shrink-0" />
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Reasons & Warnings */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {hasReasons && (
          <div className="space-y-1.5">
            <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">Routing Reasons</span>
            <ul className="text-xs text-muted-foreground space-y-1 list-disc pl-4 leading-relaxed">
              {routingPlan.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {hasWarnings && (
          <div className="space-y-1.5">
            <span className="text-xs font-bold text-amber-600 dark:text-amber-400 uppercase tracking-wider block">Warnings</span>
            <ul className="text-xs text-amber-700 dark:text-amber-400 space-y-1 list-none leading-relaxed">
              {routingPlan.warnings.map((w, i) => (
                <li key={i} className="flex gap-1.5 items-start">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                  <span>{w}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
