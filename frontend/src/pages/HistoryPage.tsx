import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  Download,
  Trash2,
  Check,
  RotateCcw,
  FileText,
  Loader2,
  Search,
  Filter,
  XCircle,
  Clock,
  ChevronDown,
  ChevronUp,
  FileCode,
  AlertCircle,
  Activity,
  Award,
} from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { getHistory, deleteJob, downloadResult, getJobStatus, type JobStatus } from '@/lib/api'
import { filenameForDownload } from '@/lib/download'
import { cn } from '@/lib/utils'
import { formatDate } from '@/lib/datetime'
import { PageHeader } from '@/components/layout/PageHeader'
import { RoutingAnalysis } from '@/components/features/conversion/RoutingAnalysis'

const STATUS_VARIANT = {
  pending: 'secondary' as const,
  processing: 'warning' as const,
  completed: 'success' as const,
  failed: 'destructive' as const,
  cancelled: 'outline' as const,
}

export function HistoryPage() {
  const PAGE_SIZE = 50
  const [jobs, setJobs] = useState<JobStatus[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [isLoading, setIsLoading] = useState(true)

  // Filtering states
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [converterFilter, setConverterFilter] = useState<string>('all')

  // Expanded job tracking
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null)
  const [loadingPreviews, setLoadingPreviews] = useState<Record<string, boolean>>({})

  // Delete confirmation state
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null)

  // Reset delete confirmation after 3 seconds of inactivity
  useEffect(() => {
    if (!deleteConfirmId) return
    const timer = setTimeout(() => {
      setDeleteConfirmId(null)
    }, 3000)
    return () => clearTimeout(timer)
  }, [deleteConfirmId])

  // Reset delete confirmation when filters or page change
  useEffect(() => {
    setDeleteConfirmId(null)
  }, [searchQuery, statusFilter, converterFilter, page])

  const fetchJobs = useCallback(async () => {
    setIsLoading(true)
    try {
      const data = await getHistory(page, PAGE_SIZE)
      setJobs(data.jobs)
      setTotal(data.total)
    } catch {
      toast.error('Failed to load conversion history')
    } finally {
      setIsLoading(false)
    }
  }, [page])

  useEffect(() => {
    void fetchJobs()
  }, [fetchJobs])

  const handleDeleteClick = (e: React.MouseEvent, jobId: string) => {
    e.stopPropagation() // Prevent toggling expansion
    if (deleteConfirmId === jobId) {
      void executeDelete(jobId)
    } else {
      setDeleteConfirmId(jobId)
    }
  }

  const executeDelete = async (jobId: string) => {
    try {
      await deleteJob(jobId)
      toast.success('Job deleted successfully')
      if (expandedJobId === jobId) setExpandedJobId(null)
      setDeleteConfirmId(null)
      void fetchJobs()
    } catch {
      toast.error('Failed to delete job')
    }
  }

  const handleDownload = async (e: React.MouseEvent, job: JobStatus) => {
    e.stopPropagation() // Prevent toggling expansion
    try {
      const { blob, filename: headerFilename } = await downloadResult(job.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url

      a.download = filenameForDownload(blob, job.filename, headerFilename)

      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      toast.success('Download started')
    } catch {
      toast.error('Failed to download result package')
    }
  }

  // Local filtering & search
  const filteredJobs = useMemo(() => {
    return jobs.filter((job) => {
      const matchesSearch = job.filename.toLowerCase().includes(searchQuery.toLowerCase())
      const matchesStatus = statusFilter === 'all' || job.status === statusFilter
      const matchesConverter = converterFilter === 'all' || job.converter === converterFilter
      return matchesSearch && matchesStatus && matchesConverter
    })
  }, [jobs, searchQuery, statusFilter, converterFilter])

  // Statistics calculation
  const stats = useMemo(() => {
    const totalCount = jobs.length
    const completedCount = jobs.filter(j => j.status === 'completed').length
    const failedCount = jobs.filter(j => j.status === 'failed').length
    const processingCount = jobs.filter(j => j.status === 'processing' || j.status === 'pending').length
    const successRate = totalCount > 0 ? Math.round((completedCount / (totalCount - processingCount || 1)) * 100) : 100

    return {
      total: total,
      completed: completedCount,
      failed: failedCount,
      successRate,
      processing: processingCount
    }
  }, [jobs, total])

  const toggleExpand = async (jobId: string) => {
    setDeleteConfirmId(null)
    const nextId = expandedJobId === jobId ? null : jobId
    setExpandedJobId(nextId)

    if (nextId) {
      const targetJob = jobs.find((j) => j.id === jobId)
      // If the job is completed but doesn't have the result text loaded yet, fetch it dynamically
      if (targetJob && targetJob.status === 'completed' && !targetJob.result_text) {
        setLoadingPreviews((prev) => ({ ...prev, [jobId]: true }))
        try {
          const detailed = await getJobStatus(jobId)
          setJobs((prev) =>
            prev.map((j) => (j.id === jobId ? { ...j, result_text: detailed.result_text } : j))
          )
        } catch (err) {
          console.error('Failed to load job result text:', err)
        } finally {
          setLoadingPreviews((prev) => ({ ...prev, [jobId]: false }))
        }
      }
    }
  }

  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="flex flex-col min-h-full">
      <PageHeader
        title="Conversion History"
        description="Browse, preview, and download previous document conversions."
      >
        <Button variant="outline" size="sm" onClick={() => void fetchJobs()} className="rounded-lg hover:bg-muted/50">
          <RotateCcw className="w-3.5 h-3.5 mr-1.5" />
          Refresh
        </Button>
      </PageHeader>

      <div className="max-w-[1400px] mx-auto space-y-8 pb-12 px-4 md:px-6 w-full">
        {/* Stats Dashboard Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
        <div className="glass-card p-5 flex items-center justify-between border border-border/30 shadow-sm">
          <div>
            <p className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Total Conversions</p>
            <h3 className="text-3xl font-extrabold mt-2 text-foreground">{stats.total}</h3>
          </div>
          <div className="p-3 rounded-xl bg-secondary text-foreground border border-border/30">
            <Activity className="w-5 h-5" />
          </div>
        </div>

        <div className="glass-card p-5 flex items-center justify-between border border-border/30 shadow-sm">
          <div>
            <p className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Success Rate</p>
            <h3 className="text-3xl font-extrabold mt-2 text-foreground">{stats.successRate}%</h3>
          </div>
          <div className="p-3 rounded-xl bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">
            <Award className="w-5 h-5" />
          </div>
        </div>

        <div className="glass-card p-5 flex items-center justify-between border border-border/30 shadow-sm">
          <div>
            <p className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Failed Jobs</p>
            <h3 className="text-3xl font-extrabold mt-2 text-rose-600 dark:text-rose-400">{stats.failed}</h3>
          </div>
          <div className="p-3 rounded-xl bg-rose-500/10 text-rose-600 dark:text-rose-400 border border-rose-500/20">
            <XCircle className="w-5 h-5" />
          </div>
        </div>

        <div className="glass-card p-5 flex items-center justify-between border border-border/30 shadow-sm">
          <div>
            <p className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Active Tasks</p>
            <h3 className="text-3xl font-extrabold mt-2 text-amber-600 dark:text-amber-400">{stats.processing}</h3>
          </div>
          <div className="p-3 rounded-xl bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
            <Clock className="w-5 h-5" />
          </div>
        </div>
      </div>

      {/* Filters Area */}
      <div className="flex flex-col md:flex-row items-center gap-4 py-2">
        <div className="relative flex-1 w-full">
          <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground/60" />
          <Input
            placeholder="Search filename..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9 bg-background/40 border-border/50 text-xs"
          />
        </div>

        <div className="flex items-center gap-3 w-full md:w-auto">
          <div className="flex items-center gap-1.5 shrink-0 text-xs font-semibold text-muted-foreground">
            <Filter className="w-3.5 h-3.5" />
            <span>Filter:</span>
          </div>

          <Select
            value={statusFilter}
            onChange={setStatusFilter}
            options={[
              { value: 'all', label: 'All Statuses' },
              { value: 'completed', label: 'Completed' },
              { value: 'failed', label: 'Failed' },
              { value: 'processing', label: 'Processing' },
              { value: 'queued', label: 'Queued' },
            ]}
          />

          <Select
            value={converterFilter}
            onChange={setConverterFilter}
            options={[
              { value: 'all', label: 'All Engines' },
              { value: 'PdfConverter', label: 'Standard PDF' },
              { value: 'TableConverter', label: 'Table Focused' },
              { value: 'OCRConverter', label: 'OCR Extraction' },
              { value: 'ExtractionConverter', label: 'Marker Text Extract' },
            ]}
          />
        </div>
      </div>

      {/* Jobs History List */}
      {isLoading ? (
        <div className="flex flex-col items-center justify-center py-24 space-y-4">
          <Loader2 className="w-8 h-8 text-primary animate-spin" />
          <p className="text-sm text-muted-foreground animate-pulse">Loading conversion logs...</p>
        </div>
      ) : filteredJobs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <FileText className="w-12 h-12 text-muted-foreground/20 mb-4 select-none" />
          <p className="text-base font-semibold text-muted-foreground">No matches found</p>
          <p className="text-xs text-muted-foreground/60 mt-1 max-w-[280px]">
            Try clearing filters or search query, or upload a document on the Convert page.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-border/10 border-y border-border/10 animate-fade-in-stagger select-none">
          {filteredJobs.map((job) => {
            const isExpanded = expandedJobId === job.id
            return (
              <div
                key={job.id}
                className={cn(
                  'transition-all duration-200 cursor-pointer hover:bg-muted/10',
                  isExpanded ? 'bg-muted/5' : 'bg-transparent'
                )}
                onClick={() => toggleExpand(job.id)}
              >
                  {/* Job Header Row */}
                  <div className="flex items-center gap-4 py-2.5 px-3">
                    {/* File icon badge */}
                    <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-primary/10 text-primary shrink-0">
                      <FileCode className="w-4 h-4" />
                    </div>

                    {/* Metadata details */}
                    <div className="flex-1 min-w-0">
                      <div className="flex flex-col sm:flex-row sm:items-center gap-1.5 sm:gap-3">
                        <p className="text-sm font-semibold truncate text-foreground">{job.filename}</p>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <Badge variant={STATUS_VARIANT[job.status]} className="w-fit text-xs py-0.5 px-1.5">
                            {job.status}
                          </Badge>
                          {job.conversion_metadata?.engine?.label && (
                            <Badge variant="outline" className="w-fit text-xs py-0 px-1 border-primary/20 bg-primary/5 text-primary">
                              {job.conversion_metadata.engine.label}
                            </Badge>
                          )}
                        </div>
                      </div>
                      <div className="flex flex-wrap items-center gap-2 mt-1 text-xs text-muted-foreground/90 font-medium">
                        <span className="uppercase text-primary font-bold">{job.output_format}</span>
                        <span className="w-1 h-1 rounded-full bg-muted-foreground/30" />
                        <span>{job.converter}</span>
                        <span className="w-1 h-1 rounded-full bg-muted-foreground/30" />
                        <span>{formatDate(job.created_at)}</span>
                      </div>
                    </div>

                    {/* Actions & Expand toggler */}
                    <div className="flex items-center gap-1.5 shrink-0" onClick={(e) => e.stopPropagation()}>
                      {job.status === 'completed' && (
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={(e) => handleDownload(e, job)}
                          className="w-8 h-8 rounded-lg hover:bg-muted"
                          title="Download Result"
                        >
                          <Download className="w-4 h-4 text-muted-foreground hover:text-foreground" />
                        </Button>
                      )}

                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={(e) => handleDeleteClick(e, job.id)}
                        className={cn(
                          "w-8 h-8 rounded-lg transition-all duration-200",
                          deleteConfirmId === job.id
                            ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/20"
                            : "hover:bg-rose-500/10 hover:text-rose-500 text-muted-foreground hover:text-rose-500"
                        )}
                        title={deleteConfirmId === job.id ? "Confirm delete" : "Delete entry"}
                      >
                        {deleteConfirmId === job.id ? (
                          <Check className="w-4 h-4" />
                        ) : (
                          <Trash2 className="w-4 h-4" />
                        )}
                      </Button>

                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => toggleExpand(job.id)}
                        className="w-8 h-8 rounded-lg hover:bg-muted"
                      >
                        {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                      </Button>
                    </div>
                  </div>

                  {/* Expandable Details Panel */}
                  <div className={cn(
                    "grid transition-all duration-300 ease-[cubic-bezier(0.16,1,0.3,1)]",
                    isExpanded ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
                  )}>
                    <div className="overflow-hidden">
                      <div className="px-3 pb-3 pt-1 border-t border-border/10 bg-muted/5 cursor-default select-text" onClick={(e) => e.stopPropagation()}>
                        <div className="space-y-3 pt-3">
                          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                            <div className="p-2.5 rounded-lg border border-border/30 bg-background/50">
                              <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">Job ID</span>
                              <span className="text-xs font-mono break-all text-foreground/90 select-all block mt-1">{job.id}</span>
                            </div>

                            <div className="p-2.5 rounded-lg border border-border/30 bg-background/50">
                              <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">Completed At</span>
                              <span className="text-xs font-medium text-foreground/90 block mt-1">
                                {job.completed_at ? formatDate(job.completed_at) : 'N/A'}
                              </span>
                            </div>

                            <div className="p-2.5 rounded-lg border border-border/30 bg-background/50">
                              <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">Status Code</span>
                              <span className="text-xs font-mono font-bold capitalize text-foreground/90 block mt-1">{job.status}</span>
                            </div>
                          </div>

                          {job.conversion_metadata && (
                            <RoutingAnalysis
                              plan={job.conversion_metadata}
                              title="Routing & Probing Analysis"
                            />
                          )}

                          {/* Error Traceback (if failed) */}
                          {job.status === 'failed' && job.error_message && (
                            <div className="p-3 rounded-lg border border-rose-500/20 bg-rose-500/5 text-rose-600 dark:text-rose-400">
                              <div className="flex items-center gap-1.5 text-xs font-semibold">
                                <AlertCircle className="w-4 h-4 shrink-0 text-rose-500" />
                                <span>Execution Mismatch (Error Message)</span>
                              </div>
                              <pre className="text-xs font-mono whitespace-pre-wrap leading-relaxed mt-2 p-2 rounded bg-rose-950/20 border border-rose-500/10">
                                {job.error_message}
                              </pre>
                            </div>
                          )}

                          {/* Converted text preview (if completed) */}
                          {job.status === 'completed' && (
                            <div className="space-y-1.5">
                              <span className="text-xs font-bold text-muted-foreground uppercase tracking-wider block">Document Output Preview</span>
                              <div className="border border-border/40 rounded-xl overflow-hidden shadow-sm bg-slate-950/5/10 max-h-[220px] overflow-y-auto">
                                {loadingPreviews[job.id] ? (
                                  <div className="p-6 text-center text-xs text-muted-foreground italic bg-background/40 flex items-center justify-center gap-2">
                                    <Loader2 className="w-3.5 h-3.5 animate-spin text-violet-500" />
                                    Loading document preview...
                                  </div>
                                ) : job.result_text ? (
                                  <pre className="p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap select-text text-slate-800 dark:text-slate-300">
                                    {job.result_text}
                                  </pre>
                                ) : (
                                  <div className="p-6 text-center text-xs text-muted-foreground italic bg-background/40">
                                    No inline text preview available. Result was compiled and archived in the zip download package.
                                  </div>
                                )}
                              </div>
                            </div>
                          )}

                        </div>
                      </div>
                    </div>
                  </div>

              </div>
            )
          })}
        </div>
      )}

      {/* Pagination Controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 pt-4 select-none">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded-lg h-8 px-3"
          >
            Previous
          </Button>
          <span className="text-xs font-semibold text-muted-foreground">
            Page {page} of {totalPages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="rounded-lg h-8 px-3"
          >
            Next
          </Button>
        </div>
      )}
    </div>
  </div>
  )
}
