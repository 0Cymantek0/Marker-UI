import { Badge } from '@/components/ui/badge'
import type { AsOfContract } from '@/lib/api'

interface AsOfStatusProps {
  asOf?: AsOfContract | null
  stale?: boolean
  onRefresh?: () => void
}

/**
 * Accessible exposure of the server-derived operational as-of envelope
 * (readiness invariant 56). WCAG 4.1.3: state is announced via role=status
 * and conveyed in text, not colour alone.
 */
export function AsOfStatus({ asOf, stale = false, onRefresh }: AsOfStatusProps) {
  if (!asOf) return null

  let badgeVariant: 'success' | 'warning' | 'destructive' | 'secondary' | 'processing'
  let badgeText: string
  let liveText: string

  if (stale) {
    badgeVariant = 'warning'
    badgeText = 'Stale'
    liveText = 'Output state: stale. The result changed on the server; the last action used an out-of-date state.'
  } else if (asOf.completeness === 'complete') {
    badgeVariant = 'success'
    badgeText = 'Current'
    liveText = 'Output state: current, complete.'
  } else if (asOf.completeness === 'incomplete') {
    badgeVariant = 'processing'
    badgeText = 'Incomplete'
    liveText = 'Output state: incomplete.'
  } else if (asOf.completeness === 'failed') {
    badgeVariant = 'destructive'
    badgeText = 'Failed'
    liveText = 'Output state: failed.'
  } else {
    badgeVariant = 'secondary'
    badgeText = 'Cancelled'
    liveText = 'Output state: cancelled.'
  }

  return (
    <div role="status" aria-live="polite" className="flex items-center gap-1.5">
      <Badge variant={badgeVariant} className="py-0.5 px-2 text-xs leading-tight">
        {badgeText}
      </Badge>
      <span className="sr-only">{liveText}</span>
      {stale && onRefresh && (
        <button
          type="button"
          onClick={onRefresh}
          className="text-xs font-semibold uppercase tracking-wider rounded-md px-2 py-0.5 bg-amber-500/15 text-amber-700 dark:text-amber-300 hover:bg-amber-500/25 transition-colors"
        >
          Refresh
        </button>
      )}
    </div>
  )
}
