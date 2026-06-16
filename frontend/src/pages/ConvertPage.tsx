import { useState, useCallback, useEffect } from 'react'
import { Play, Loader2, Download, Trash2, FileText, Terminal, Repeat } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { FileUpload } from '@/components/features/FileUpload'
import { ConversionOptions } from '@/components/features/ConversionOptions'
import { TerminalLog } from '@/components/features/TerminalLog'
import { OutputViewer } from '@/components/features/OutputViewer'
import { ModelSwapDialog } from '@/components/features/conversion/ModelSwapDialog'
import { useConversionQueue } from '@/hooks/useConversionQueue'
import type { ConversionConfig } from '@/lib/api'
import { Progress } from '@/components/ui/progress'
import { PageHeader } from '@/components/layout/PageHeader'

const DEFAULT_CONFIG: ConversionConfig = {
  output_format: 'markdown',
  converter: 'PdfConverter',
  use_llm: false,
  image_handling_mode: 'extraction',
  force_ocr: false,
  paginate: false,
  disable_image_extraction: false,
  page_range: '',
  language: '',
  disable_multiprocessing: false,
  debug: false,
}

export function ConvertPage() {
  const [files, setFiles] = useState<File[]>([])
  const [localPaths, setLocalPaths] = useState<string>('')
  const [outputDir, setOutputDir] = useState<string>('')
  const [config, setConfig] = useState<ConversionConfig>(() => {
    const saved = localStorage.getItem('marker-conversion-config')
    if (saved) {
      try {
        return { ...DEFAULT_CONFIG, ...JSON.parse(saved) }
      } catch (e) {
        console.error('Failed to parse saved conversion config', e)
      }
    }
    return DEFAULT_CONFIG
  })
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [showConsole, setShowConsole] = useState(false)
  // Model-swap dialog: which job it targets, and whether it auto-surfaced.
  const [swapJobId, setSwapJobId] = useState<string | null>(null)
  const [swapAuto, setSwapAuto] = useState(false)

  useEffect(() => {
    localStorage.setItem('marker-conversion-config', JSON.stringify(config))
  }, [config])

  const { jobs, start, cancel, download, clearLogs, removeJob, dismissSwapPrompt, clearRateLimited } = useConversionQueue()

  // Auto-surface the swap dialog when a running job reports it's stuck on rate
  // limits (key rotation exhausted) and the user hasn't dismissed it yet.
  useEffect(() => {
    if (swapJobId) return // a dialog is already open
    const stuck = jobs.find(
      (j) =>
        j.rateLimited &&
        !j.swapPromptDismissed &&
        (j.phase === 'processing' || j.phase === 'uploading') &&
        j.llmProvider
    )
    if (stuck) {
      setSwapJobId(stuck.id)
      setSwapAuto(true)
    }
  }, [jobs, swapJobId])

  const swapJob = jobs.find((j) => j.id === swapJobId) || null

  const closeSwap = useCallback(() => {
    if (swapJob && swapAuto) dismissSwapPrompt(swapJob.id)
    setSwapJobId(null)
    setSwapAuto(false)
  }, [swapJob, swapAuto, dismissSwapPrompt])

  const openSwapManual = useCallback((id: string) => {
    setSwapJobId(id)
    setSwapAuto(false)
  }, [])

  // Auto-select the latest job if none is selected
  const selectedJob = jobs.find((j) => j.id === selectedJobId) || jobs[jobs.length - 1]

  // Inline preview uses the clean document text captured from /status
  // (result_text). The download blob must NOT be decoded here: when images are
  // extracted the blob is a ZIP, and reading it as text renders binary garbage.
  const previewText = selectedJob?.phase === 'completed' ? (selectedJob.resultText ?? null) : null

  const completedJobs = jobs.filter((j) => j.phase === 'completed')
  const overallProgress = jobs.length > 0
    ? Math.round(jobs.reduce((sum, j) => sum + j.progress, 0) / jobs.length)
    : 0

  const handleConvert = useCallback(async () => {
    const parsedLocalPaths = localPaths
      .split('\n')
      .map((p) => p.trim())
      .filter((p) => p.length > 0)

    if (files.length === 0 && parsedLocalPaths.length === 0) {
      toast.error('Please select a file first or specify local paths')
      return
    }

    try {
      await start(files, parsedLocalPaths, config, outputDir)
      setFiles([])
      setLocalPaths('')
      toast.success('Conversion queued successfully!')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Conversion failed')
    }
  }, [files, localPaths, config, outputDir, start])

  const getButtonText = () => {
    const parsedLocalPaths = localPaths
      .split('\n')
      .map((p) => p.trim())
      .filter((p) => p.length > 0)
    const total = files.length + parsedLocalPaths.length
    if (total === 0) return 'Convert Document'
    return `Convert ${total} Document${total > 1 ? 's' : ''}`
  }

  const handleRemoveFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx))
  }

  const handleClearAll = () => {
    setFiles([])
  }

  return (
    <div className="flex flex-col min-h-full">
      <PageHeader 
        title="Convert Document"
        description="Transform PDFs, Word documents, spreadsheets, slides, and images into clean, layout-aware, production-ready Markdown files."
      />

      <div className="max-w-[1400px] mx-auto space-y-8 pb-12 px-4 md:px-6 w-full">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 xl:gap-12 items-start">
        {/* Left Column: File Upload Zone & Config Options (5 cols) */}
        <div className="lg:col-span-5 space-y-8">
          {/* Step 1: Upload */}
          <div className="space-y-3">
            <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase pb-2 border-b border-border/20">
              01 / SOURCE DOCUMENTS
            </h3>
            <FileUpload
              onFilesSelect={(newFiles) => setFiles((prev) => [...prev, ...newFiles])}
              selectedFiles={files}
              onRemoveFile={handleRemoveFile}
              onClearAll={handleClearAll}
              localPaths={localPaths}
              onLocalPathsChange={setLocalPaths}
              outputDir={outputDir}
              onOutputDirChange={setOutputDir}
            />
          </div>

          <hr className="border-border/30" />

          {/* Step 2: Settings */}
          <div className="space-y-4">
            <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase pb-2 border-b border-border/20">
              02 / CONVERSION PARAMETERS
            </h3>
            <ConversionOptions
              config={config}
              onChange={setConfig}
            />
          </div>

          <hr className="border-border/30" />

          {/* Action: Convert Button */}
          <Button
            onClick={handleConvert}
            disabled={files.length === 0 && localPaths.trim().length === 0}
            className="w-full h-12 text-xs font-bold uppercase tracking-wider shadow-md rounded-xl hover:scale-[1.002] active:scale-[0.99] transition-all duration-200"
            size="lg"
          >
            <Play className="w-3.5 h-3.5 mr-2" />
            {getButtonText()}
          </Button>
        </div>

        {/* Right Column: Execution Terminal & Conversion Queue (7 cols) */}
        <div className="lg:col-span-7 space-y-6">
          {/* Step 3: Console Logs at the top */}
          <div className="space-y-3">
            <div className="flex items-center justify-between border-b border-border/20 pb-2">
              <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase mt-0.5">
                03 / EXECUTION CONSOLE
              </h3>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setShowConsole(true)}
                className="h-8 text-[10px] font-bold uppercase tracking-wider gap-1.5 rounded-lg text-muted-foreground hover:text-foreground"
              >
                <Terminal className="w-3.5 h-3.5" />
                Open Console
              </Button>
            </div>
            
            {showConsole && (
              <TerminalLog
                logs={selectedJob ? selectedJob.logs : []}
                phase={selectedJob ? selectedJob.phase : 'idle'}
                onClear={selectedJob ? () => clearLogs(selectedJob.id) : undefined}
                onClose={() => setShowConsole(false)}
              />
            )}
          </div>

          {/* Queue List & Overall Progress */}
          {jobs.length > 0 && (
            <div className="glass-card p-5 space-y-5 border border-border/30 shadow-sm animate-fade-in">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-2 border-b border-border/20">
                <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">
                  Conversion Queue ({jobs.length})
                </h3>
                
                {/* Sleek Universal Progress Info */}
                <div className="text-[10px] font-bold text-muted-foreground tracking-wider uppercase flex items-center gap-2">
                  <span>Overall:</span>
                  <span className="text-foreground">{completedJobs.length} of {jobs.length} completed</span>
                  <span className="px-1.5 py-0.5 rounded bg-primary/10 text-primary font-mono text-[9px]">
                    {overallProgress}%
                  </span>
                </div>
              </div>

              {/* Universal Progress Bar */}
              <div className="space-y-1.5">
                <Progress 
                  value={overallProgress} 
                  className="h-1.5 transition-all duration-300"
                  indicatorClassName="bg-primary shadow-[0_0_8px_rgba(var(--primary-rgb),0.5)]"
                />
              </div>

              {/* Space-Saving Vertical Queue Area */}
              <div className="space-y-2 max-h-[300px] overflow-y-auto pr-1 scrollbar-thin scrollbar-thumb-muted scrollbar-track-transparent">
                {jobs.map((job) => {
                  const isSelected = selectedJob?.id === job.id
                  const isJobRunning = job.phase === 'uploading' || job.phase === 'processing'
                  const isCompleted = job.phase === 'completed'
                  const isFailed = job.phase === 'failed'
                  const isQueued = job.phase === 'idle'

                  return (
                    <div
                      key={job.id}
                      onClick={() => setSelectedJobId(job.id)}
                      className={cn(
                        'relative p-3.5 rounded-xl border text-left cursor-pointer transition-all flex items-center justify-between gap-4 select-none overflow-hidden',
                        isSelected
                          ? 'border-primary/40 bg-primary/5 shadow-sm ring-1 ring-primary/10'
                          : 'border-border/20 bg-card/35 hover:bg-muted/20 hover:border-border'
                      )}
                    >
                      {/* Glassmorphic progress bar background inside the card itself */}
                      {isJobRunning && (
                        <div
                          className="absolute inset-y-0 left-0 bg-primary/10 transition-all duration-500 ease-out pointer-events-none"
                          style={{ width: `${job.progress}%` }}
                        />
                      )}

                      {/* File Icon & Info Column */}
                      <div className="flex-1 min-w-0 flex items-center gap-3 relative z-10">
                        <div className={cn(
                          'p-2 rounded-lg shrink-0 transition-colors duration-300',
                          isCompleted ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' :
                          isFailed ? 'bg-rose-500/10 text-rose-600 dark:text-rose-400' :
                          isJobRunning ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'
                        )}>
                          <FileText className="w-4 h-4" />
                        </div>

                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-bold truncate text-foreground" title={job.filename}>
                              {job.filename}
                            </span>
                            <span className="text-[9px] text-muted-foreground font-mono bg-muted/65 px-1 py-0.5 rounded">
                              {job.outputFormat}
                            </span>
                          </div>

                          <div className="flex items-center gap-2 mt-1.5">
                            <span className={cn(
                              'text-[10px] font-bold tracking-wide flex items-center gap-1.5',
                              isCompleted && 'text-emerald-600 dark:text-emerald-400',
                              isFailed && 'text-rose-600 dark:text-rose-400',
                              isJobRunning && 'text-primary',
                              isQueued && 'text-muted-foreground'
                            )}>
                              {isJobRunning && <Loader2 className="w-2.5 h-2.5 text-primary animate-spin shrink-0" />}
                              {job.statusText}
                            </span>

                            {isJobRunning && (
                              <>
                                <span className="text-[10px] text-muted-foreground/60 font-mono">•</span>
                                <span className="text-[10px] font-bold font-mono text-foreground">
                                  {Math.round(job.progress)}%
                                </span>
                                {job.eta !== undefined && job.eta > 0 && (
                                  <>
                                    <span className="text-[10px] text-muted-foreground/60 font-mono">•</span>
                                    <span className="text-[10px] font-mono text-muted-foreground">
                                      ETA: {job.eta}s
                                    </span>
                                  </>
                                )}
                              </>
                            )}
                          </div>
                        </div>
                      </div>

                      {/* Actions aligned directly inside the UI card to save space */}
                      <div className="flex items-center gap-1.5 relative z-10" onClick={(e) => e.stopPropagation()}>
                        {isCompleted && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => download(job.id)}
                            className="h-8 text-[10px] font-bold uppercase tracking-wider gap-1.5 rounded-lg border-emerald-500/20 bg-emerald-500/5 hover:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border shadow-sm"
                          >
                            <Download className="w-3.5 h-3.5" />
                            Download
                          </Button>
                        )}
                        
                        {isJobRunning && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => cancel(job.id)}
                            className="h-8 text-[10px] font-bold uppercase tracking-wider text-muted-foreground hover:text-rose-600 dark:hover:text-rose-400 rounded-lg hover:bg-rose-500/10"
                          >
                            Cancel
                          </Button>
                        )}

                        {isJobRunning && job.llmProvider && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => openSwapManual(job.id)}
                            title="Switch model for this running job"
                            className={cn(
                              'h-8 text-[10px] font-bold uppercase tracking-wider gap-1.5 rounded-lg',
                              job.rateLimited
                                ? 'text-amber-600 dark:text-amber-400 hover:bg-amber-500/10'
                                : 'text-muted-foreground hover:text-primary hover:bg-primary/10'
                            )}
                          >
                            <Repeat className="w-3.5 h-3.5" />
                            Switch Model
                          </Button>
                        )}

                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => removeJob(job.id)}
                          className="w-8 h-8 rounded-lg hover:bg-muted text-muted-foreground hover:text-rose-600 dark:hover:text-rose-400 transition-colors"
                          title="Remove from list"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </Button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Inline output preview for the selected completed job.
              Shows parsed markdown with per-image understanding badges (commit 6). */}
          {selectedJob && selectedJob.phase === 'completed' && (
            <div className="glass-card border border-border/30 shadow-sm animate-fade-in">
              <div className="flex items-center justify-between px-4 py-2.5 border-b border-border/20">
                <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">
                  Output Preview
                  <span className="ml-2 text-muted-foreground/60 normal-case font-medium">
                    {selectedJob.filename}
                  </span>
                </h3>
              </div>
              <div className="p-4">
                <OutputViewer
                  content={previewText}
                  onDownload={() => download(selectedJob.id)}
                  imageUnderstanding={selectedJob.imageUnderstanding}
                />
              </div>
            </div>
          )}

          {jobs.length === 0 && (
            <div className="flex flex-col items-center justify-center p-12 border border-dashed border-border/50 rounded-2xl bg-card/10 text-muted-foreground min-h-[200px]">
              <FileText className="w-8 h-8 text-muted-foreground/45 mb-3 stroke-[1.5]" />
              <p className="text-xs font-semibold text-muted-foreground">Queue is empty</p>
              <p className="text-[10px] text-muted-foreground/60 mt-1 max-w-[280px] text-center leading-relaxed">
                Add source files or local paths on the left, then click Convert to start.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>

    {swapJob && (
      <ModelSwapDialog
        open={!!swapJob}
        auto={swapAuto}
        filename={swapJob.filename}
        providerId={swapJob.llmProvider}
        currentModel={swapJob.llmModel}
        onClose={closeSwap}
        onApplied={() => clearRateLimited(swapJob.id)}
      />
    )}
  </div>
  )
}
