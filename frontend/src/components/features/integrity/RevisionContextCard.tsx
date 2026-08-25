import {
  Archive,
  CheckCircle2,
  CircleDashed,
  FileText,
  Fingerprint,
  GitBranch,
  Hash,
  Settings2,
  Tag,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { AsOfContract, JobStatus } from '@/lib/api'
import { CopyableValue } from './CopyableValue'
import { shortToken } from './token'

interface RevisionContextCardProps {
  job: JobStatus
  /** Server-authoritative envelope (last known truth for this job). */
  asOf: AsOfContract
  /** Token the user is acting with; falls back to the server token. */
  pinnedToken: string | null
}

function FieldRow({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-start gap-3 py-2">
      <span className="mt-0.5 text-muted-foreground shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <span className="block text-xs font-bold text-muted-foreground uppercase tracking-wider">{label}</span>
        <div className="mt-0.5">{children}</div>
      </div>
    </div>
  )
}

function MonoText({ value }: { value: string }) {
  return (
    <span className="font-mono text-xs break-all text-foreground/90 select-all" title={value}>
      {shortToken(value)}
    </span>
  )
}

/**
 * Identity of the artifact revision under review. Every value is server-
 * derived; labels are text + icon so state is never colour-only.
 */
export function RevisionContextCard({ job, asOf, pinnedToken }: RevisionContextCardProps) {
  const actingToken = pinnedToken ?? asOf.state_token

  return (
    <section
      aria-labelledby="integrity-context-heading"
      className="glass-card border border-border/30 rounded-xl p-5 space-y-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id="integrity-context-heading" className="text-base font-bold text-foreground flex items-center gap-2">
            <FileText className="w-4 h-4 text-primary shrink-0" />
            <span className="truncate">{job.filename}</span>
          </h3>
          <p className="text-xs text-muted-foreground mt-0.5">Revision you are acting on</p>
        </div>
        <Badge variant={asOf.completeness === 'complete' ? 'success' : 'warning'} className="shrink-0">
          {asOf.completeness === 'complete' ? (
            <CheckCircle2 className="w-3 h-3 mr-1" aria-hidden="true" />
          ) : (
            <CircleDashed className="w-3 h-3 mr-1" aria-hidden="true" />
          )}
          {asOf.completeness}
        </Badge>
      </div>

      <div className="divide-y divide-border/10">
        <FieldRow icon={<Fingerprint className="w-4 h-4" />} label="State token (acting)">
          <CopyableValue value={actingToken} display={shortToken(actingToken)} />
        </FieldRow>

        <FieldRow icon={<FileText className="w-4 h-4" />} label="Job ID">
          <CopyableValue value={job.id} />
        </FieldRow>

        <FieldRow icon={<Hash className="w-4 h-4" />} label="Result digest">
          {asOf.result_digest ? <MonoText value={asOf.result_digest} /> : <NotProvided />}
        </FieldRow>

        <FieldRow icon={<GitBranch className="w-4 h-4" />} label="Source revision">
          {asOf.source_revision_id ? <MonoText value={asOf.source_revision_id} /> : <NotProvided />}
        </FieldRow>

        <FieldRow icon={<Settings2 className="w-4 h-4" />} label="Config digest">
          {asOf.config_digest ? <MonoText value={asOf.config_digest} /> : <NotProvided />}
        </FieldRow>

        <FieldRow icon={<Archive className="w-4 h-4" />} label="Artifacts purged">
          <span className="text-xs font-semibold text-foreground/90">
            {asOf.artifacts_purged ? 'Yes — artifacts were purged' : 'No'}
          </span>
        </FieldRow>

        <FieldRow icon={<Tag className="w-4 h-4" />} label="Schema version">
          <span className="font-mono text-xs text-foreground/90 select-all">{asOf.schema_version}</span>
        </FieldRow>
      </div>
    </section>
  )
}

function NotProvided() {
  return <span className="text-xs text-muted-foreground/70 italic">Not provided by server</span>
}
