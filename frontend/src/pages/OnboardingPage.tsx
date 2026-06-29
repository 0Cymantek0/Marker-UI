import { useEffect, useState } from 'react'
import {
  getModelsStatus,
  getHybridOcrStatus,
  setupHybridOcrModels,
  cancelModelsDownload,
  retryModelsDownload,
  ModelTrackerStatus,
  HybridOcrStatus,
} from '@/lib/api'
import { CanvasConfetti } from '@/components/ui/CanvasConfetti'
import { EngineConsole } from '@/components/features/onboarding/EngineConsole'
import { DeveloperConsole } from '@/components/features/onboarding/DeveloperConsole'
import { PipelineVisualizer } from '@/components/features/onboarding/PipelineVisualizer'
import { Loader2, AlertTriangle } from 'lucide-react'
import { toast } from 'sonner'

interface OnboardingPageProps {
  onComplete: () => void
}

export function OnboardingPage({ onComplete }: OnboardingPageProps) {
  const [status, setStatus] = useState<ModelTrackerStatus | null>(null)
  const [isCancelling, setIsCancelling] = useState(false)
  const [isRetrying, setIsRetrying] = useState(false)
  const [showConfetti, setShowConfetti] = useState(false)
  const [firstLoadCheck, setFirstLoadCheck] = useState(true)
  const [hybridStatus, setHybridStatus] = useState<HybridOcrStatus | null>(null)
  const [isSettingUpHybrid, setIsSettingUpHybrid] = useState(false)

  const ensureHybridOcrModels = async () => {
    const current = await getHybridOcrStatus()
    setHybridStatus(current)
    const missing = Object.values(current.engines).some((engine) => !engine.model_present)
    if (!missing) return true

    setIsSettingUpHybrid(true)
    try {
      const result = await setupHybridOcrModels('all')
      setHybridStatus(result.status)
      const stillMissing = Object.values(result.status.engines).some((engine) => !engine.model_present)
      return !stillMissing
    } finally {
      setIsSettingUpHybrid(false)
    }
  }

  useEffect(() => {
    let active = true
    let pollTimer: NodeJS.Timeout | null = null
    let redirectTimer: NodeJS.Timeout | null = null

    const pollStatus = async () => {
      try {
        const data = await getModelsStatus()
        if (!active) return

        setStatus(data)

        if (firstLoadCheck) {
          setFirstLoadCheck(false)
          if (data.initialized || data.overall.status === 'completed') {
            const hybridReady = await ensureHybridOcrModels()
            if (!hybridReady) return
            onComplete()
            return
          }
        } else {
          if (data.initialized) {
            const hybridReady = await ensureHybridOcrModels()
            if (!hybridReady) return
            setShowConfetti(true)
            redirectTimer = setTimeout(() => {
              if (active) onComplete()
            }, 3000)
            return
          }
        }
      } catch (err) {
        console.error('Failed to fetch model status:', err)
      }

      if (active) {
        pollTimer = setTimeout(pollStatus, 1500)
      }
    }

    void pollStatus()

    return () => {
      active = false
      if (pollTimer) clearTimeout(pollTimer)
      if (redirectTimer) clearTimeout(redirectTimer)
    }
  }, [onComplete, firstLoadCheck])

  const handleCancel = async () => {
    setIsCancelling(true)
    try {
      await cancelModelsDownload()
      toast.success('Cancellation request sent')
    } catch (err: any) {
      toast.error(err.message || 'Failed to cancel')
    } finally {
      setIsCancelling(false)
    }
  }

  const handleRetry = async () => {
    setIsRetrying(true)
    try {
      await retryModelsDownload()
      toast.success('Download restarted successfully')
    } catch (err: any) {
      toast.error(err.message || 'Failed to restart')
    } finally {
      setIsRetrying(false)
    }
  }

  if (!status) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-background text-foreground p-6 select-none font-sans">
        <Loader2 className="h-8 w-8 text-primary/40 animate-spin mb-4" />
        <p className="text-muted-foreground font-medium text-xs uppercase tracking-wider">
          Connecting to Marker engine...
        </p>
      </div>
    )
  }

  const isFailed = status.overall.status === 'failed'

  return (
    <div className="relative min-h-screen bg-background text-foreground flex items-center justify-center p-4 md:p-8 overflow-hidden font-sans">
      {showConfetti && <CanvasConfetti />}

      {/* Ambient background glows - subtle neutral ones */}
      <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-primary/5 rounded-full blur-[100px] -z-10 animate-pulse duration-[8000ms]" />
      <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-muted/10 rounded-full blur-[100px] -z-10 animate-pulse duration-[6000ms]" />

      <div className="w-full max-w-7xl 2xl:max-w-8xl space-y-8 relative py-6 md:py-10 px-4 md:px-6">
        {/* Failed / Interrupt Alert banner */}
        {isFailed && (
          <div className="p-4 bg-rose-500/10 border border-rose-500/20 rounded-xl flex items-start gap-3 text-rose-400 animate-fade-in select-none">
            <AlertTriangle className="h-5 w-5 mt-0.5 shrink-0" />
            <div className="space-y-1">
              <h4 className="font-bold text-sm">Setup Interrupted</h4>
              <p className="text-xs text-rose-400/90 leading-relaxed">
                {status.error || 'The setup process was stopped. Verify internet connection and retry.'}
              </p>
            </div>
          </div>
        )}

        {/* Responsive Dual-Panel grid */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 lg:gap-16 xl:gap-24 items-start">
          {/* Left Column (Console / Control) */}
          <div className="lg:col-span-5 space-y-8">
            <EngineConsole
              status={status}
              isCancelling={isCancelling}
              isRetrying={isRetrying}
              handleCancel={handleCancel}
              handleRetry={handleRetry}
              onComplete={onComplete}
              hybridStatus={hybridStatus}
              isSettingUpHybrid={isSettingUpHybrid}
            />
            
            <DeveloperConsole status={status} />
          </div>

          {/* Right Column (Pipeline Visualizer) */}
          <div className="lg:col-span-7 py-2">
            <PipelineVisualizer models={status.models} overallStatus={status.overall.status} />
          </div>

        </div>
      </div>
    </div>
  )
}
