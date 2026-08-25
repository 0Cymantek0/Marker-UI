import { BadgeCheck, Download, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { CopyableValue } from './CopyableValue'
import { shortToken } from './token'

export interface VerifiedExport {
  filename: string
  stateToken: string
}

interface ExportPanelProps {
  formats: string[]
  format: string
  onFormatChange: (format: string) => void
  acting: boolean
  disabled: boolean
  disabledReason: string | null
  onDownload: () => void
  verifiedExport: VerifiedExport | null
}

/**
 * Protected export action. Sits below the revision context (chronological
 * order: understand the revision first, then act). Disabled while any
 * blocking condition holds, with the reason in text.
 */
export function ExportPanel({
  formats,
  format,
  onFormatChange,
  acting,
  disabled,
  disabledReason,
  onDownload,
  verifiedExport,
}: ExportPanelProps) {
  return (
    <section
      aria-labelledby="integrity-export-heading"
      className="glass-card border border-border/30 rounded-xl p-5 space-y-4"
    >
      <h3 id="integrity-export-heading" className="text-base font-bold text-foreground">
        Verified export
      </h3>

      <div className="space-y-1.5">
        <label id="integrity-format-label" className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">
          Format
        </label>
        <Select
          value={format}
          onChange={onFormatChange}
          options={formats.map((f) => ({ value: f, label: f }))}
          disabled={acting}
        />
      </div>

      <div className="space-y-2">
        <Button onClick={onDownload} disabled={disabled} className="w-full sm:w-auto">
          {acting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
          {acting ? 'Verifying with server…' : 'Download (verified)'}
        </Button>
        {disabled && !acting && disabledReason && (
          <p className="text-xs text-muted-foreground">{disabledReason}</p>
        )}
        {acting && (
          <p className="text-xs text-muted-foreground">Checking the pinned state token against the server…</p>
        )}
      </div>

      {verifiedExport && (
        <div
          className="rounded-lg border border-emerald-500/25 bg-emerald-500/10 p-3 flex items-start gap-2.5"
          data-testid="verified-export-result"
        >
          <BadgeCheck className="w-4 h-4 text-emerald-600 dark:text-emerald-400 shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-emerald-700 dark:text-emerald-400">
              Verified export downloaded
            </p>
            <p className="text-xs text-muted-foreground mt-0.5">
              The server confirmed the pinned state token is current. Saved as{' '}
              <span className="font-semibold text-foreground/90">{verifiedExport.filename}</span> against state{' '}
              <span className="font-mono" title={verifiedExport.stateToken}>
                {shortToken(verifiedExport.stateToken)}
              </span>
              .
            </p>
            <CopyableValue value={verifiedExport.stateToken} className="mt-1" />
          </div>
        </div>
      )}
    </section>
  )
}
