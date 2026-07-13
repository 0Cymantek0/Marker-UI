import { useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'
import { Badge, badgeVariants } from '@/components/ui/badge'
import type { ImageUnderstandingMeta } from '@/lib/api'

type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>['variant']>

// Maps an image_type to (badge label, badge variant) per UX doc §7.3.
const TYPE_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  chart_bar: { label: 'Chart → Table', variant: 'warning' },
  chart_line: { label: 'Chart → Table', variant: 'warning' },
  chart_pie: { label: 'Chart → Table', variant: 'warning' },
  chart_scatter: { label: 'Chart → Table', variant: 'warning' },
  chart_other: { label: 'Chart → JSON', variant: 'warning' },
  table_image: { label: 'Table → Markdown', variant: 'outline' },
  diagram_flow: { label: 'Diagram → Mermaid', variant: 'processing' },
  diagram_sequence: { label: 'Diagram → Mermaid', variant: 'processing' },
  diagram_state: { label: 'Diagram → Mermaid', variant: 'processing' },
  diagram_class: { label: 'Diagram → Mermaid', variant: 'processing' },
  diagram_architecture: { label: 'Diagram → Mermaid', variant: 'processing' },
  equation: { label: 'Equation → LaTeX', variant: 'secondary' },
  screenshot_ui: { label: 'Screenshot → Description', variant: 'default' },
  figure_technical: { label: 'Figure → Description', variant: 'default' },
  photo: { label: 'Photo → Alt-text', variant: 'default' },
  decorative: { label: 'Decorative — omitted', variant: 'secondary' },
  other: { label: 'Image → Description', variant: 'default' },
}

function badgeFor(imageType: string): { label: string; variant: BadgeVariant } {
  return TYPE_BADGE[imageType] ?? { label: 'Image → Description', variant: 'default' }
}

interface ImageUnderstandingBadgeProps {
  meta: ImageUnderstandingMeta
  // 1-based ordinal for the aria-label ("Image 2 of 5: ...").
  index: number
  total: number
  inline?: boolean
}

/**
 * Inline badge overlaid on a rendered image token. Hover shows a short
 * tooltip; click opens a read-only detail modal (UX doc §7.3 + §11.1).
 *
 * Phase 1 is read-only audit; manual correction is deferred to Phase 2.
 */
export function ImageUnderstandingBadge({ meta, index, total, inline = false }: ImageUnderstandingBadgeProps) {
  const [open, setOpen] = useState(false)
  const { label, variant } = badgeFor(meta.image_type)
  const confidencePct = Math.round((meta.confidence ?? 0) * 100)

  return (
    <>
      <div className={cn("group relative", inline ? "inline-flex items-center" : "inline-flex")}>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label={`Image ${index} of ${total}: ${meta.image_type} converted via VLM. Confidence ${confidencePct}%. Click for details.`}
          className={cn(inline ? "relative" : "absolute -top-2 -right-2 z-10")}
        >
          <Badge variant={variant} className="px-3 py-0.5 text-xs font-bold uppercase tracking-wide shadow-sm cursor-pointer">
            {label}
          </Badge>
        </button>
        {/* Hover tooltip */}
        <div className="absolute top-full left-1/2 -translate-x-1/2 mt-1.5 hidden group-hover:block w-56 p-2.5 rounded-lg bg-slate-900 dark:bg-slate-800 text-xs leading-relaxed text-slate-100 shadow-lg border border-slate-800/80 z-50 pointer-events-none text-left">
          <div className="font-bold uppercase tracking-wide text-xs text-slate-400 mb-1">
            Image Understanding
          </div>
          <div className="space-y-0.5">
            <div><span className="text-slate-400">Type:</span> {meta.image_type}</div>
            <div><span className="text-slate-400">Confidence:</span> {confidencePct}%</div>
            {meta.model && <div><span className="text-slate-400">Model:</span> {meta.model}</div>}
          </div>
          <div className="mt-1 text-slate-400 italic">Click for details</div>
        </div>
      </div>

      {open && createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-overlay-fade-in"
          onClick={() => setOpen(false)}
        >
          <div
            className="glass-card max-w-md w-full bg-background border border-border/50 rounded-2xl shadow-xl overflow-hidden animate-modal-zoom-in flex flex-col max-h-[80vh] text-left"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-3.5 border-b border-border/20">
              <h3 className="font-extrabold text-sm text-foreground uppercase tracking-wider">
                Image Understanding
              </h3>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="p-1 rounded-md text-muted-foreground hover:text-foreground transition-colors"
                aria-label="Close detail"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-5 space-y-3 text-xs">
              <Row label="Image" value={meta.image_name} mono />
              <Row label="Type" value={meta.image_type} />
              <Row label="Confidence" value={`${confidencePct}%`} />
              <Row label="Model" value={meta.model ?? 'auto-resolved'} />
              <Row label="Omitted" value={meta.omitted ? 'Yes' : 'No'} />
              <div className="pt-2 border-t border-border/20">
                <div className="text-xs font-bold tracking-widest text-muted-foreground/70 uppercase mb-1">
                  Representation
                </div>
                <Badge variant={variant} className="text-xs">{label}</Badge>
              </div>
              <p className="text-xs text-muted-foreground/60 italic pt-1">
                Phase 1 is read-only. Manual correction is planned for a later release.
              </p>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="text-xs font-bold tracking-widest text-muted-foreground/70 uppercase shrink-0 pt-0.5">
        {label}
      </span>
      <span className={cn('text-foreground text-right break-all', mono && 'font-mono')}>{value}</span>
    </div>
  )
}
