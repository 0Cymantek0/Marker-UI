import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { shortToken } from './token'

interface StaleBannerProps {
  observedToken: string
  currentToken: string
  refreshing: boolean
  onRefresh: () => void
}

/**
 * Reconciliation surface for a rejected stale revision. Comparison tokens are
 * both shown (observed vs server current); the only exit is adopting the
 * server's current state. Rendered instead of — never alongside — success UI.
 */
export function StaleBanner({ observedToken, currentToken, refreshing, onRefresh }: StaleBannerProps) {
  return (
    <section
      role="alert"
      aria-label="Stale state"
      className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 space-y-3"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
        <div className="space-y-2 min-w-0">
          <h3 className="text-sm font-bold text-amber-700 dark:text-amber-300">
            This revision moved on the server
          </h3>
          <p className="text-sm text-amber-700/90 dark:text-amber-300/90">
            The state token you are acting on no longer matches the job&apos;s current server state, so no
            verified export can run against it. Refresh to adopt the server&apos;s current state.
          </p>
          <dl className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
            <div className="rounded-lg bg-background/60 border border-amber-500/20 px-3 py-2 min-w-0">
              <dt className="font-bold text-muted-foreground uppercase tracking-wider">Your token (observed)</dt>
              <dd className="font-mono break-all mt-0.5" title={observedToken}>
                {shortToken(observedToken)}
              </dd>
            </div>
            <div className="rounded-lg bg-background/60 border border-amber-500/20 px-3 py-2 min-w-0">
              <dt className="font-bold text-muted-foreground uppercase tracking-wider">Server current token</dt>
              <dd className="font-mono break-all mt-0.5" title={currentToken}>
                {shortToken(currentToken)}
              </dd>
            </div>
          </dl>
        </div>
      </div>
      <div className="pl-8">
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
          <RefreshCw className={refreshing ? 'w-3.5 h-3.5 animate-spin' : 'w-3.5 h-3.5'} />
          {refreshing ? 'Refreshing…' : 'Refresh current state'}
        </Button>
      </div>
    </section>
  )
}
