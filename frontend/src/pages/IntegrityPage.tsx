import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AlertCircle, Loader2, RotateCcw, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { PageHeader } from '@/components/layout/PageHeader'
import { ApiError, getHistory, getJobStatus, downloadResult, type AsOfContract, type JobStatus } from '@/lib/api'
import { filenameForDownload, saveBlob } from '@/lib/download'
import { JobPicker } from '@/components/features/integrity/JobPicker'
import { RevisionContextCard } from '@/components/features/integrity/RevisionContextCard'
import { StaleBanner } from '@/components/features/integrity/StaleBanner'
import { ExportPanel, type VerifiedExport } from '@/components/features/integrity/ExportPanel'

/**
 * Review-integrity surface. The server is the only validity authority: the
 * page adopts tokens from server envelopes (status fetch, 409 payload) and
 * never derives validity on its own — it only compares the pinned token
 * against the server-provided state token.
 *
 * State machine:
 *   selecting → loading → ready ⇄ acting
 *                     ↘ stale → (refresh adopts server token) → ready
 *                     ↘ error → (retry re-fetches) → loading
 */
type IntegrityPhase = 'selecting' | 'loading' | 'ready' | 'stale' | 'acting' | 'error'

interface StaleCompare {
  observed: string
  current: string
}

export function IntegrityPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const jobParam = searchParams.get('job') ?? ''
  const asOfParam = searchParams.get('as_of') ?? ''

  const [phase, setPhase] = useState<IntegrityPhase>('selecting')
  const [job, setJob] = useState<JobStatus | null>(null)
  const [serverAsOf, setServerAsOf] = useState<AsOfContract | null>(null)
  const [pinnedToken, setPinnedToken] = useState<string | null>(null)
  const [staleCompare, setStaleCompare] = useState<StaleCompare | null>(null)
  const [verifiedExport, setVerifiedExport] = useState<VerifiedExport | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [format, setFormat] = useState<string | null>(null)

  // Picker state (recent completed jobs)
  const [recentJobs, setRecentJobs] = useState<JobStatus[]>([])
  const [recentLoading, setRecentLoading] = useState(false)
  const [recentFailed, setRecentFailed] = useState(false)

  const formats = useMemo(() => {
    if (!job) return []
    return job.available_formats?.length ? job.available_formats : [job.output_format || 'markdown']
  }, [job])

  // Derived effective format: the user's choice when still offered, else the
  // first available. Deriving (instead of syncing via effect) guarantees the
  // export action is usable on the same render the job lands.
  const activeFormat = format && formats.includes(format) ? format : formats[0] ?? null

  const loadJob = useCallback(async (jobId: string, pinnedFromUrl?: string) => {
    setPhase('loading')
    setJob(null)
    setServerAsOf(null)
    setPinnedToken(null)
    setStaleCompare(null)
    setVerifiedExport(null)
    setErrorMessage(null)
    try {
      const status = await getJobStatus(jobId)
      if (status.status !== 'completed') {
        setPhase('error')
        setErrorMessage(
          `Job is ${status.status}. Only completed jobs have server-verified artifact state to act on.`
        )
        return
      }
      const envelope = status.as_of
      if (!envelope) {
        setPhase('error')
        setErrorMessage('The server did not provide an as-of state envelope for this job, so currency cannot be verified.')
        return
      }
      setJob(status)
      setServerAsOf(envelope)
      // Compare a bookmarked token against the server envelope (the server is
      // the truth being compared — this is not a client validity algorithm).
      if (pinnedFromUrl && pinnedFromUrl !== envelope.state_token) {
        setPinnedToken(pinnedFromUrl)
        setStaleCompare({ observed: pinnedFromUrl, current: envelope.state_token })
        setPhase('stale')
      } else {
        setPinnedToken(envelope.state_token)
        setPhase('ready')
      }
    } catch {
      setPhase('error')
      setErrorMessage('Job state could not be loaded from the server. Nothing has been exported.')
    }
  }, [])

  const fetchRecentJobs = useCallback(async () => {
    setRecentLoading(true)
    setRecentFailed(false)
    try {
      const data = await getHistory(1, 10, undefined, 'completed')
      setRecentJobs(data.jobs)
    } catch {
      setRecentFailed(true)
    } finally {
      setRecentLoading(false)
    }
  }, [])

  // Deep link: ?job=<id> loads that job authoritatively; &as_of=<token> pins
  // a bookmarked token. Each unique job|as_of pair loads exactly once.
  const loadedKeyRef = useRef<string | null>(null)
  useEffect(() => {
    if (!jobParam) {
      loadedKeyRef.current = null
      setPhase((prev) => (prev === 'selecting' ? prev : 'selecting'))
      return
    }
    const key = `${jobParam}|${asOfParam}`
    if (loadedKeyRef.current === key) return
    loadedKeyRef.current = key
    void loadJob(jobParam, asOfParam || undefined)
  }, [jobParam, asOfParam, loadJob])

  useEffect(() => {
    void fetchRecentJobs()
  }, [fetchRecentJobs])

  const selectJob = useCallback(
    (jobId: string) => {
      setSearchParams({ job: jobId }, { replace: true })
    },
    [setSearchParams]
  )

  const backToSelection = useCallback(() => {
    setSearchParams({}, { replace: true })
  }, [setSearchParams])

  const handleDownload = useCallback(async () => {
    if (!job || !pinnedToken || !activeFormat) return
    const token = pinnedToken
    const chosenFormat = activeFormat
    setPhase('acting')
    setVerifiedExport(null)
    setErrorMessage(null)
    try {
      const { blob, filename: headerFilename, asOfMode } = await downloadResult(job.id, chosenFormat, token)
      if (asOfMode !== 'verified') {
        // Server did not confirm currency: no success may be shown.
        setPhase('error')
        setErrorMessage(
          'The server did not confirm this export as verified, so no download was recorded as successful.'
        )
        toast.error('Export could not be verified by the server')
        return
      }
      const filename = filenameForDownload(blob, job.filename, headerFilename)
      saveBlob(blob, filename)
      setVerifiedExport({ filename, stateToken: token })
      setPhase('ready')
      toast.success('Verified export downloaded')
    } catch (err) {
      if (err instanceof ApiError && err.code === 'stale_state') {
        const currentEnvelope = err.currentAsOf
        if (!currentEnvelope) {
          // Ambiguous rejection: the server did not include its current state,
          // so there is nothing authoritative to reconcile to. Fail
          // conservatively rather than showing a dead stale UI.
          setPhase('error')
          setErrorMessage(
            'The server rejected this export as stale but did not include its current state. Retry the load to re-establish authoritative state.'
          )
          return
        }
        // Reconcile visibly: adopt the 409 envelope as server truth, keep the
        // observed token for comparison. Zero false success.
        setServerAsOf(currentEnvelope)
        setStaleCompare({ observed: token, current: currentEnvelope.state_token })
        setPhase('stale')
        return
      }
      setVerifiedExport(null)
      setPhase('error')
      setErrorMessage('The download failed before the server could verify it. Nothing has been exported.')
    }
  }, [job, pinnedToken, activeFormat])

  const handleRefreshCurrentState = useCallback(async () => {
    if (!job) return
    setRefreshing(true)
    try {
      const status = await getJobStatus(job.id)
      const envelope = status.as_of
      if (!envelope) {
        setPhase('error')
        setErrorMessage('The server did not provide an as-of state envelope, so currency cannot be verified.')
        return
      }
      // Adopt the server's current state token wholesale.
      setJob((prev) => (prev ? { ...prev, ...status } : status))
      setServerAsOf(envelope)
      setPinnedToken(envelope.state_token)
      setStaleCompare(null)
      setVerifiedExport(null)
      setPhase('ready')
      toast.success('Current server state adopted')
    } catch {
      setPhase('error')
      setErrorMessage('Refreshing the current state failed. The revision has not changed.')
    } finally {
      setRefreshing(false)
    }
  }, [job])

  const retryLoad = useCallback(() => {
    if (jobParam) void loadJob(jobParam, asOfParam || undefined)
    else setPhase('selecting')
  }, [jobParam, asOfParam, loadJob])

  const downloadDisabled = phase !== 'ready' || !activeFormat
  const disabledReason =
    phase === 'stale'
      ? 'Download is disabled: the pinned state token is stale. Refresh the current state first.'
      : phase === 'error'
        ? 'Download is disabled: the job state could not be confirmed.'
        : formats.length === 0
          ? 'Download is disabled: the server reported no available formats.'
          : null

  const liveMessage =
    phase === 'loading'
      ? 'Loading job state from the server.'
      : phase === 'ready'
        ? verifiedExport
          ? 'Verified export downloaded. State current.'
          : 'State current. Ready for verified export.'
        : phase === 'acting'
          ? 'Verifying export with the server.'
          : phase === 'stale'
            ? 'State stale: the revision moved on the server. Download disabled.'
            : phase === 'error'
              ? 'Error: the job state could not be confirmed.'
              : 'Select a job to review its artifact integrity.'

  return (
    <div className="flex flex-col min-h-full" data-page="integrity">
      <PageHeader
        title="Review Integrity"
        description="See which artifact revision you are acting on, confirm server-authoritative currency, and run a verified export."
      >
        <span className="inline-flex items-center gap-2 text-xs font-semibold text-muted-foreground">
          <ShieldCheck className="w-4 h-4 text-primary" />
          Server-authoritative state
        </span>
        {jobParam && phase !== 'selecting' && (
          <Button variant="ghost" size="sm" onClick={backToSelection}>
            Change job
          </Button>
        )}
      </PageHeader>

      <div className="sr-only" role="status" aria-live="polite">
        {liveMessage}
      </div>

      <div className="max-w-3xl mx-auto space-y-8 pb-12 px-4 md:px-6 w-full">
        {phase === 'selecting' ? (
          <JobPicker
            jobs={recentJobs}
            loading={recentLoading}
            loadFailed={recentFailed}
            onSubmitJobId={selectJob}
            onPickJob={(picked) => selectJob(picked.id)}
            onRetryLoad={() => void fetchRecentJobs()}
          />
        ) : phase === 'loading' ? (
          <div className="flex flex-col items-center justify-center py-24 space-y-4">
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
            <p className="text-sm text-muted-foreground animate-pulse">Loading server state…</p>
          </div>
        ) : job && serverAsOf ? (
          <>
            <RevisionContextCard job={job} asOf={serverAsOf} pinnedToken={pinnedToken} />

            {phase === 'stale' && staleCompare && (
              <StaleBanner
                observedToken={staleCompare.observed}
                currentToken={staleCompare.current}
                refreshing={refreshing}
                onRefresh={() => void handleRefreshCurrentState()}
              />
            )}

            <ExportPanel
              formats={formats}
              format={activeFormat ?? ''}
              onFormatChange={setFormat}
              acting={phase === 'acting'}
              disabled={downloadDisabled}
              disabledReason={disabledReason}
              onDownload={() => void handleDownload()}
              verifiedExport={phase === 'stale' ? null : verifiedExport}
            />
          </>
        ) : null}

        {phase === 'error' && (
          <section
            role="alert"
            className="rounded-xl border border-rose-500/25 bg-rose-500/5 p-4 flex flex-col items-start gap-3"
            aria-label="Operation failed"
          >
            <div className="flex items-start gap-2.5 min-w-0">
              <AlertCircle className="w-4 h-4 text-rose-500 shrink-0 mt-0.5" />
              <p className="text-sm text-rose-600 dark:text-rose-400">
                {errorMessage ?? 'Something failed. Nothing has been exported.'}
              </p>
            </div>
            <Button variant="outline" size="sm" onClick={retryLoad}>
              <RotateCcw className="w-3.5 h-3.5" />
              Retry
            </Button>
          </section>
        )}
      </div>
    </div>
  )
}
