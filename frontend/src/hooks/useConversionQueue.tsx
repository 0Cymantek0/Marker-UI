import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react'
import {
  uploadFile,
  getJobEvents,
  getJobStatus,
  getHistory,
  downloadResult,
  cancelJob,
  regenerateFormat,
  type ConversionConfig,
  type JobStatus,
  type ImageUnderstandingMeta,
} from '@/lib/api'
import { filenameForDownload } from '@/lib/download'

export type ConversionPhase =
  | 'idle'
  | 'uploading'
  | 'processing'
  | 'completed'
  | 'cancelled'
  | 'failed'

export interface JobState {
  id: string // client-side unique ID
  filename: string
  file: File | null
  localPath: string
  phase: ConversionPhase
  progress: number
  statusText: string
  jobId: string | null
  error: string | null
  resultBlob: Blob | null
  resultFilename?: string
  // Clean document text from the DB (/status result_text). Used for the inline
  // preview. The download blob may be a ZIP (when images are extracted), so it
  // must NOT be decoded as text for preview — see ConvertPage.
  resultText: string | null
  logs: string[]
  outputFormat: string
  formats: Record<string, string> | null
  availableFormats: string[]
  outputDir?: string
  elapsed?: number
  eta?: number
  isBunch?: boolean
  // Per-image understanding metadata for the badge UI.
  imageUnderstanding?: ImageUnderstandingMeta[] | null
  conversionMetadata?: Record<string, any> | null
  // LLM provider/model this job runs under — lets the model-swap dialog
  // pre-fill and scope a same-provider hot-swap. Empty when not using an LLM.
  llmProvider?: string
  llmModel?: string
  // True once the backend has signalled that key rotation is exhausted and a
  // model swap is worth suggesting. Drives the auto-surfaced swap dialog.
  rateLimited?: boolean
  // User dismissed the auto dialog for this job; don't auto-resurface it.
  swapPromptDismissed?: boolean
}

export interface SourceEngineOverrides {
  fileKeys?: string[]
  fileEngineOverrides?: Record<string, string>
  localPathEngineOverrides?: Record<string, string>
}

interface ConversionContextType {
  jobs: JobState[]
  start: (
    files: File[],
    localPaths: string[],
    config: ConversionConfig,
    outputDir?: string,
    sourceEngineOverrides?: SourceEngineOverrides
  ) => Promise<void>
  cancel: (id: string) => Promise<void>
  download: (id: string, format?: string) => Promise<void>
  clearLogs: (id: string) => void
  removeJob: (id: string) => void
  regenerateJobFormat: (id: string, format: string) => Promise<void>
  // Model-swap prompt controls.
  dismissSwapPrompt: (id: string) => void
  clearRateLimited: (id: string) => void
}

const ConversionContext = createContext<ConversionContextType | null>(null)

export function ConversionProvider({ children }: { children: React.ReactNode }) {
  const [jobs, setJobs] = useState<JobState[]>([])
  const jobsRef = useRef<JobState[]>([])
  const eventSourcesRef = useRef<Record<string, EventSource>>({})
  const hydratedRef = useRef(false)

  useEffect(() => {
    jobsRef.current = jobs
  }, [jobs])

  const updateJob = useCallback((id: string, updater: Partial<JobState> | ((prev: JobState) => JobState)) => {
    setJobs((prevJobs) =>
      prevJobs.map((j) => {
        if (j.id !== id) return j
        const next = typeof updater === 'function' ? updater(j) : { ...j, ...updater }
        return next
      })
    )
  }, [])

  const handleJobCompleted = useCallback((id: string, jobId: string) => {
    updateJob(id, (prev) => ({
      ...prev,
      logs: [...prev.logs, '[SUCCESS] Conversion execution succeeded.', '[SYSTEM] Fetching result package...'],
    }))

    downloadResult(jobId)
      .then(async ({ blob, filename }) => {
        // Fetch status to capture the per-image understanding sidecar (it is
        // persisted server-side only at finalize, so SSE can't carry it) and
        // the clean document text (the blob may be a ZIP, see resultText).
        let imageUnderstanding: ImageUnderstandingMeta[] | null = null
        let resultText: string | null = null
        let conversionMetadata: Record<string, any> | null = null
        let formats: Record<string, string> | null = null
        let availableFormats: string[] | undefined
        try {
          const status = await getJobStatus(jobId)
          imageUnderstanding = status.image_understanding ?? null
          resultText = status.result_text ?? null
          conversionMetadata = status.conversion_metadata ?? null
          formats = status.formats ?? null
          availableFormats = status.available_formats ?? undefined
        } catch {
          // Non-fatal: badges just won't render for this job.
        }
        updateJob(id, (prev) => ({
          ...prev,
          phase: 'completed',
          progress: 100,
          statusText: 'Conversion complete',
          error: null,
          resultBlob: blob,
          resultFilename: filename,
          resultText,
          imageUnderstanding,
          conversionMetadata,
          formats,
          availableFormats: availableFormats ?? prev.availableFormats,
          logs: [...prev.logs, '[SUCCESS] Result package successfully fetched and ready.'],
        }))
      })
      .catch((err) => {
        const msg = err instanceof Error ? err.message : 'Download failed'
        updateJob(id, (prev) => ({
          ...prev,
          phase: 'completed',
          progress: 100,
          statusText: 'Conversion complete',
          error: null,
          resultBlob: null,
          logs: [...prev.logs, `[WARN] Failed to fetch result locally: ${msg}. Click download to try again.`],
        }))
      })
  }, [updateJob])

  const handleJobFailed = useCallback((id: string, error: string) => {
    updateJob(id, (prev) => ({
      ...prev,
      phase: 'failed',
      error: error,
      statusText: 'Conversion failed',
      logs: [...prev.logs, `[ERROR] Conversion task failed: ${error}`],
    }))
  }, [updateJob])

  const handleJobCancelled = useCallback((id: string, message = '[SYSTEM] Job was cancelled.') => {
    updateJob(id, (prev) => ({
      ...prev,
      phase: 'cancelled',
      error: null,
      statusText: 'Cancelled',
      logs: [...prev.logs, message],
    }))
  }, [updateJob])

  const handleJobSSEDisconnected = useCallback((id: string, jobId: string) => {
    updateJob(id, (prev) => ({
      ...prev,
      statusText: 'Connection lost - polling for status...',
      logs: [...prev.logs, '[WARN] SSE socket disconnected. Falling back to polling...'],
    }))

    const pollInterval = setInterval(async () => {
      try {
        const status = await getJobStatus(jobId)
        if (status.status === 'completed') {
          clearInterval(pollInterval)
          const imageUnderstanding = status.image_understanding ?? null
          const resultText = status.result_text ?? null
          const conversionMetadata = status.conversion_metadata ?? null
          const formats = status.formats ?? null
          const availFmts = status.available_formats
          downloadResult(jobId)
            .then(({ blob, filename }) => {
              updateJob(id, (prev) => ({
                ...prev,
                phase: 'completed',
                progress: 100,
                statusText: 'Conversion complete',
                error: null,
                resultBlob: blob,
                resultFilename: filename,
                resultText,
                imageUnderstanding,
                conversionMetadata,
                formats,
                availableFormats: availFmts ?? prev.availableFormats,
                logs: [...prev.logs, '[SUCCESS] SSE disconnected, recovered via polling.'],
              }))
            })
            .catch(() => {
              updateJob(id, (prev) => ({
                ...prev,
                phase: 'completed',
                progress: 100,
                statusText: 'Conversion complete',
                error: null,
                resultBlob: null,
                resultText,
                imageUnderstanding,
                conversionMetadata,
                logs: [...prev.logs, '[WARN] SSE disconnected. Polling recovered but download failed.'],
              }))
            })
        } else if (status.status === 'failed') {
          clearInterval(pollInterval)
          updateJob(id, (prev) => ({
            ...prev,
            phase: 'failed',
            error: status.error_message ?? 'Conversion failed',
            statusText: 'Conversion failed',
            logs: [...prev.logs, `[ERROR] SSE disconnected, polling detected failure: ${status.error_message ?? 'Unknown'}`],
          }))
        } else if (status.status === 'cancelled') {
          clearInterval(pollInterval)
          handleJobCancelled(id, '[SYSTEM] Job was cancelled on the backend.')
        }
      } catch (err: any) {
        const is404 = err instanceof Error && err.message.includes('404')
        if (is404) {
          clearInterval(pollInterval)
          handleJobCancelled(id, '[SYSTEM] Job not found on backend.')
        }
      }
    }, 3000)
  }, [handleJobCancelled, updateJob])

  const attachJobEvents = useCallback((id: string, jobId: string) => {
    if (eventSourcesRef.current[id]) {
      return
    }

    const es = getJobEvents(jobId)
    eventSourcesRef.current[id] = es

    const closeES = () => {
      es.close()
      delete eventSourcesRef.current[id]
    }

    es.addEventListener('progress', (e) => {
      const data = e.data ? JSON.parse(e.data) : {}
      const messageStr = data.message || 'Executing conversion pipelines...'

      if (data.status === 'completed') {
        closeES()
        handleJobCompleted(id, jobId)
        return
      }

      if (data.status === 'failed') {
        closeES()
        handleJobFailed(id, data.error ?? 'Conversion failed')
        return
      }

      if (data.status === 'cancelled') {
        closeES()
        handleJobCancelled(id)
        return
      }

      updateJob(id, (prev) => {
        const nextLogs = [...prev.logs]
        if (data.logs && data.logs.length > 0) {
          data.logs.forEach((log: string) => {
            if (!nextLogs.includes(log)) {
              nextLogs.push(log)
            }
          })
        } else if (messageStr && nextLogs[nextLogs.length - 1] !== `[INFO] ${messageStr}`) {
          nextLogs.push(`[INFO] ${messageStr}`)
        }
        // Backend emits this once key rotation is exhausted / stuck on rate
        // limits — the only point at which suggesting a model swap is useful.
        const rateLimited =
          prev.rateLimited || nextLogs.some((l) => l.includes('model swap suggested'))
        return {
          ...prev,
          progress: Math.max(prev.progress, data.progress ?? prev.progress),
          statusText: messageStr,
          logs: nextLogs,
          elapsed: data.elapsed,
          eta: data.eta,
          rateLimited,
        }
      })
    })

    es.addEventListener('status', (e) => {
      const data = e.data ? JSON.parse(e.data) : {}
      if (data.status === 'completed') {
        closeES()
        handleJobCompleted(id, jobId)
      } else if (data.status === 'failed') {
        closeES()
        handleJobFailed(id, data.error ?? 'Conversion failed')
      } else if (data.status === 'cancelled') {
        closeES()
        handleJobCancelled(id)
      }
    })

    es.onerror = () => {
      closeES()
      handleJobSSEDisconnected(id, jobId)
    }
  }, [handleJobCancelled, handleJobCompleted, handleJobFailed, handleJobSSEDisconnected, updateJob])

  useEffect(() => {
    if (hydratedRef.current) return

    let active = true
    void getHistory(1, 50)
      .then(({ jobs: historyJobs }) => {
        if (!active) return
        hydratedRef.current = true

        const activeJobs = historyJobs.filter((job: JobStatus) =>
          job.status === 'pending' || job.status === 'processing'
        )
        if (activeJobs.length === 0) return

        const recoveredJobs: JobState[] = activeJobs.map((job) => ({
          id: `history-${job.id}`,
          filename: job.filename,
          file: null,
          localPath: '',
          phase: 'processing',
          progress: job.progress ?? 10,
          statusText: job.status === 'pending' ? 'Queued on backend...' : 'Processing document...',
          jobId: job.id,
          error: null,
          resultBlob: null,
          resultText: null,
          logs: [
            '[SYSTEM] Recovered active job from backend history.',
            `[SYSTEM] Re-attaching SSE channel for job: ${job.id}`,
          ],
          outputFormat: job.output_format || 'markdown',
          formats: job.formats ?? null,
          availableFormats: job.available_formats ?? [job.output_format || 'markdown'],
        }))

        const existingBackendIds = new Set(
          jobsRef.current.map((job) => job.jobId).filter(Boolean)
        )
        const missing = recoveredJobs.filter((job) => !existingBackendIds.has(job.jobId))
        if (missing.length === 0) return

        setJobs((prev) => [...prev, ...missing])

        for (const job of missing) {
          if (job.jobId) {
            attachJobEvents(job.id, job.jobId)
          }
        }
      })
      .catch(() => {
        if (active) {
          hydratedRef.current = true
        }
        // History recovery is best-effort; normal uploads still work.
      })

    return () => {
      active = false
    }
  }, [attachJobEvents])

  const runJob = useCallback(async (job: JobState, config: ConversionConfig, outputDir?: string) => {
    updateJob(job.id, {
      phase: job.file ? 'uploading' : 'processing',
      progress: job.file ? 5 : 10,
      statusText: job.file ? 'Uploading file...' : 'Submitting local file path...',
      logs: job.file ? [
        `[SYSTEM] Initiating upload process for file: ${job.filename} (${(job.file.size / 1024 / 1024).toFixed(2)} MB)`,
        '[SYSTEM] Preparing payload headers and checking connection...',
      ] : [
        `[SYSTEM] Initiating conversion for local file: ${job.localPath}`,
        `[SYSTEM] Checking backend file system availability...`,
      ]
    })

    try {
      const response = await uploadFile(
        job.file,
        config,
        job.localPath || undefined,
        outputDir || undefined
      )

      updateJob(job.id, (prev) => ({
        ...prev,
        phase: 'processing',
        jobId: response.job_id,
        progress: 15,
        statusText: 'Processing document...',
        logs: [
          ...prev.logs,
          job.file ? `[SYSTEM] Upload completed successfully.` : `[SYSTEM] Local path accepted by backend.`,
          `[SYSTEM] Job created with ID: ${response.job_id}`,
          `[SYSTEM] Opening Server-Sent Events (SSE) socket channel...`,
          `[SYSTEM] Model loading/retrieval initiated...`,
        ]
      }))

      attachJobEvents(job.id, response.job_id)

    } catch (err) {
      const errMsg = err instanceof Error ? err.message : 'Upload failed'
      updateJob(job.id, (prev) => ({
        ...prev,
        phase: 'failed',
        progress: 0,
        statusText: 'Upload/Submission failed',
        error: errMsg,
        logs: [...prev.logs, `[ERROR] Network error: ${errMsg}`],
      }))
    }
  }, [updateJob, attachJobEvents])

  const start = useCallback(async (
    files: File[],
    localPaths: string[],
    config: ConversionConfig,
    outputDir?: string,
    sourceEngineOverrides?: SourceEngineOverrides
  ) => {
    const newJobs: JobState[] = []
    const jobConfigs: Record<string, ConversionConfig> = {}
    const cleanLocalPaths = localPaths.map((p) => p.trim()).filter((p) => p.length > 0)
    const isBunch = (files.length + cleanLocalPaths.length) > 1
    const configForOverride = (engineOverride?: string): ConversionConfig => {
      const next = { ...config }
      if (engineOverride && engineOverride !== 'auto') {
        next.engine_override = engineOverride
      } else {
        delete next.engine_override
      }
      return next
    }

    // Add files
    for (const [index, f] of files.entries()) {
      const id = 'file-' + Math.random().toString(36).substring(2, 9)
      const sourceKey = sourceEngineOverrides?.fileKeys?.[index] ?? id
      const engineOverride = sourceEngineOverrides?.fileEngineOverrides?.[sourceKey]
      jobConfigs[id] = configForOverride(engineOverride)
      newJobs.push({
        id,
        filename: f.name,
        file: f,
        localPath: '',
        phase: 'idle',
        progress: 0,
        statusText: 'Queued',
        jobId: null,
        error: null,
        resultBlob: null,
        resultText: null,
        logs: [],
        outputFormat: config.output_formats[0] ?? 'markdown',
        formats: null,
        availableFormats: [...config.output_formats],
        outputDir,
        isBunch,
        llmProvider: config.use_llm ? config.llm_provider : undefined,
        llmModel: config.use_llm ? config.llm_model : undefined,
      })
    }

    // Add local paths
    for (const lp of cleanLocalPaths) {
      const id = 'local-' + Math.random().toString(36).substring(2, 9)
      const filename = lp.split(/[/\\]/).pop() || lp
      const engineOverride = sourceEngineOverrides?.localPathEngineOverrides?.[lp]
      jobConfigs[id] = configForOverride(engineOverride)
      newJobs.push({
        id,
        filename,
        file: null,
        localPath: lp,
        phase: 'idle',
        progress: 0,
        statusText: 'Queued',
        jobId: null,
        error: null,
        resultBlob: null,
        resultText: null,
        logs: [],
        outputFormat: config.output_formats[0] ?? 'markdown',
        formats: null,
        availableFormats: [...config.output_formats],
        outputDir,
        isBunch,
        llmProvider: config.use_llm ? config.llm_provider : undefined,
        llmModel: config.use_llm ? config.llm_model : undefined,
      })
    }

    setJobs((prev) => [...prev, ...newJobs])

    // Run each job in background with its own engine override.
    for (const job of newJobs) {
      void runJob(job, jobConfigs[job.id] ?? configForOverride(), outputDir)
    }
  }, [runJob])

  const cancel = useCallback(async (id: string) => {
    // Immediately close EventSource if active to prevent triggering onerror and polling
    if (eventSourcesRef.current[id]) {
      eventSourcesRef.current[id].close()
      delete eventSourcesRef.current[id]
    }

    setJobs((prevJobs) => {
      const job = prevJobs.find((j) => j.id === id)
      if (!job) return prevJobs

      if (job.jobId) {
        cancelJob(job.jobId).catch(() => {})
      }

      return prevJobs.map((j) => {
        if (j.id !== id) return j
        return {
          ...j,
          phase: 'cancelled',
          error: null,
          statusText: 'Cancelled',
          logs: [...j.logs, '[SYSTEM] Cancel request submitted.'],
        }
      })
    })
  }, [])

  const download = useCallback(async (id: string, format?: string) => {
    const job = jobs.find((j) => j.id === id)
    if (!job || !job.jobId) return

    let blob = format ? null : job.resultBlob
    let headerFilename = format ? null : job.resultFilename

    if (!blob) {
      try {
        const result = await downloadResult(job.jobId, format)
        blob = result.blob
        headerFilename = result.filename
      } catch (err) {
        console.error('Failed to download result:', err)
        return
      }
    }

    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url

    a.download = filenameForDownload(blob, job.filename, headerFilename, !!job.isBunch)

    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }, [jobs])

  const clearLogs = useCallback((id: string) => {
    updateJob(id, { logs: [] })
  }, [updateJob])

  const removeJob = useCallback((id: string) => {
    // Close the SSE socket first so its onerror doesn't kick off a polling loop
    // for a job we're about to drop.
    if (eventSourcesRef.current[id]) {
      eventSourcesRef.current[id].close()
      delete eventSourcesRef.current[id]
    }

    setJobs((prev) => {
      return prev.filter((j) => j.id !== id)
    })
  }, [])

  const dismissSwapPrompt = useCallback((id: string) => {
    updateJob(id, { swapPromptDismissed: true })
  }, [updateJob])

  const clearRateLimited = useCallback((id: string) => {
    // After a swap is applied, clear the flag and drop the stale signal log so
    // the dialog doesn't immediately re-detect from history.
    updateJob(id, (prev) => ({
      ...prev,
      rateLimited: false,
      swapPromptDismissed: false,
      logs: prev.logs.filter((l) => !l.includes('model swap suggested')),
    }))
  }, [updateJob])

  const regenerateJobFormat = useCallback(async (id: string, format: string) => {
    const job = jobsRef.current.find((j) => j.id === id)
    if (!job?.jobId) return
    const result = await regenerateFormat(job.jobId, format)
    const status = await getJobStatus(job.jobId)
    updateJob(id, (prev) => ({
      ...prev,
      formats: status.formats ?? prev.formats,
      availableFormats: result.available_formats ?? prev.availableFormats,
      resultText: status.result_text ?? prev.resultText,
    }))
  }, [updateJob])

  return (
    <ConversionContext.Provider value={{ jobs, start, cancel, download, clearLogs, removeJob, regenerateJobFormat, dismissSwapPrompt, clearRateLimited }}>
      {children}
    </ConversionContext.Provider>
  )
}

export function useConversionQueue() {
  const context = useContext(ConversionContext)
  if (!context) {
    throw new Error('useConversionQueue must be used within a ConversionProvider')
  }
  return context
}
