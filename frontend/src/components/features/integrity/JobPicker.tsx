import { useState } from 'react'
import { Clock, FileText, Loader2, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import type { JobStatus } from '@/lib/api'
import { formatDate } from '@/lib/datetime'

interface JobPickerProps {
  jobs: JobStatus[]
  loading: boolean
  loadFailed: boolean
  onSubmitJobId: (jobId: string) => void
  onPickJob: (job: JobStatus) => void
  onRetryLoad: () => void
}

/**
 * Entry surface of the integrity review: pick which artifact revision to act
 * on. Chronological layout — manual Job ID entry first, then recent completed
 * jobs as quick picks.
 */
export function JobPicker({ jobs, loading, loadFailed, onSubmitJobId, onPickJob, onRetryLoad }: JobPickerProps) {
  const [jobIdInput, setJobIdInput] = useState('')

  const completed = jobs.filter((job) => job.status === 'completed')

  return (
    <div className="space-y-8">
      {/* Manual entry comes first: it is always available. */}
      <section aria-labelledby="integrity-manual-heading" className="space-y-3">
        <h3 id="integrity-manual-heading" className="text-sm font-bold text-foreground">
          Load by Job ID
        </h3>
        <form
          className="flex flex-col sm:flex-row gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            const trimmed = jobIdInput.trim()
            if (trimmed) onSubmitJobId(trimmed)
          }}
        >
          <div className="relative flex-1">
            <Search className="absolute left-3 top-3 h-4 w-4 text-muted-foreground/60 pointer-events-none" />
            <Input
              value={jobIdInput}
              onChange={(e) => setJobIdInput(e.target.value)}
              placeholder="Paste a conversion job id…"
              aria-label="Job ID"
              className="pl-9 bg-background/40 border-border/50"
            />
          </div>
          <Button type="submit" disabled={!jobIdInput.trim()} className="sm:w-auto">
            Load job state
          </Button>
        </form>
      </section>

      {/* Quick picks below: recent server-side completed jobs. */}
      <section aria-labelledby="integrity-recent-heading" className="space-y-3">
        <h3 id="integrity-recent-heading" className="text-sm font-bold text-foreground">
          Recent completed jobs
        </h3>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span>Loading recent jobs…</span>
          </div>
        ) : loadFailed ? (
          <div className="flex flex-col items-start gap-3 py-4">
            <p className="text-sm text-muted-foreground">
              Recent jobs could not be loaded. You can still enter a Job ID above.
            </p>
            <Button variant="outline" size="sm" onClick={onRetryLoad}>
              Retry loading jobs
            </Button>
          </div>
        ) : completed.length === 0 ? (
          <p className="text-sm text-muted-foreground/70 py-4">
            No completed jobs yet. Run a conversion first.
          </p>
        ) : (
          <ul className="divide-y divide-border/10 border-y border-border/10">
            {completed.map((job) => (
              <li key={job.id}>
                <button
                  type="button"
                  onClick={() => onPickJob(job)}
                  className="w-full flex items-center gap-3 py-2.5 px-3 text-left rounded-lg hover:bg-muted/10 transition-colors"
                >
                  <span className="flex items-center justify-center w-8 h-8 rounded-lg bg-primary/10 text-primary shrink-0">
                    <FileText className="w-4 h-4" />
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="block text-sm font-semibold truncate text-foreground">{job.filename}</span>
                    <span className="block text-xs font-mono text-muted-foreground truncate">{job.id}</span>
                  </span>
                  <span className="flex items-center gap-2 shrink-0">
                    <Badge variant="success" className="text-xs py-0.5 px-2.5">
                      {job.status}
                    </Badge>
                    <span className="hidden sm:flex items-center gap-1 text-xs text-muted-foreground">
                      <Clock className="w-3 h-3" />
                      {formatDate(job.created_at)}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
