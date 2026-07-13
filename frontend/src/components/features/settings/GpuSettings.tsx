import { Cpu } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { GPUStatus, GPUWorkerMode, GPUWorkersResolved } from '@/lib/api'

interface GpuSettingsProps {
  gpuEnabled: boolean
  gpuStatus: GPUStatus | null
  isPollingGpu: boolean
  onToggleGpu: (enabled: boolean) => void
  gpuWorkers: GPUWorkersResolved | null
  workerMode: GPUWorkerMode
  workerCount: number
  isSavingWorkers: boolean
  onWorkerModeChange: (mode: GPUWorkerMode) => void
  onWorkerCountChange: (count: number) => void
  onSaveWorkerScaling: () => void
}

export function GpuSettings({
  gpuEnabled,
  gpuStatus,
  isPollingGpu,
  onToggleGpu,
  gpuWorkers,
  workerMode,
  workerCount,
  isSavingWorkers,
  onWorkerModeChange,
  onWorkerCountChange,
  onSaveWorkerScaling,
}: GpuSettingsProps) {
  return (
    <>
      <div className="space-y-4 pt-6 border-t border-border/20">
        <div className="flex items-center justify-between">
          <div className="space-y-1">
            <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-2">
              <Cpu className="w-4 h-4 text-primary" />
              GPU Acceleration
            </h3>
            <p className="text-xs text-muted-foreground leading-relaxed max-w-3xl">
              Accelerate layout detection, OCR, and table extraction using your system's NVIDIA GPU (via CUDA).
            </p>
          </div>
          <div className="flex items-center gap-3">
            {gpuStatus?.status === 'ready' && gpuStatus.cuda_available && (
              <Badge variant="success" className="px-2.5 py-1 text-xs font-bold uppercase tracking-wider">
                Ready
              </Badge>
            )}
            {gpuStatus?.status === 'ready' && !gpuStatus.cuda_available && (
              <Badge variant="warning" className="px-2.5 py-1 text-xs font-bold uppercase tracking-wider">
                Restart Required
              </Badge>
            )}
            {(gpuStatus?.status === 'installing' || (gpuStatus?.status === 'not_installed' && gpuEnabled)) && (
              <Badge variant="processing" className="px-2.5 py-1 text-xs font-bold uppercase tracking-wider">
                Installing {gpuStatus.progress}%
              </Badge>
            )}
            {gpuStatus?.status === 'failed' && (
              <Badge variant="destructive" className="px-2.5 py-1 text-xs font-bold uppercase tracking-wider">
                Verification Failed
              </Badge>
            )}
            <button
              type="button"
              onClick={() => onToggleGpu(!gpuEnabled)}
              disabled={isPollingGpu}
              className={cn(
                'relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                gpuEnabled ? 'bg-primary' : 'bg-muted border-border/20',
                isPollingGpu && 'opacity-50 cursor-not-allowed'
              )}
            >
              <span
                className={cn(
                  'pointer-events-none inline-block h-5 w-5 transform rounded-full shadow ring-0 transition duration-200 ease-in-out',
                  gpuEnabled ? 'bg-primary-foreground translate-x-5' : 'bg-white translate-x-0'
                )}
              />
            </button>
          </div>
        </div>

        {gpuEnabled && gpuStatus && (
          <div className="space-y-4 p-4 rounded-xl border border-border/50 bg-card/45 animate-fade-in">
            <div className="flex flex-col items-center justify-center py-6 bg-black/20 rounded-xl border border-border/10 space-y-3.5 shadow-inner">
              <div className="flex items-baseline justify-center select-none">
                <span
                  style={{
                    backgroundImage: `linear-gradient(90deg, hsl(var(--primary)) ${gpuStatus.progress}%, hsl(var(--muted-foreground) / 0.25) ${gpuStatus.progress}%)`,
                    WebkitBackgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                    display: 'inline-block',
                  }}
                  className="font-black tracking-wider text-xl md:text-2xl uppercase transition-all duration-300 bg-clip-text text-transparent"
                >
                  GPU ACCELERATION
                </span>
              </div>

              <div className="text-center space-y-1">
                <p className="text-xs text-muted-foreground font-semibold">
                  {gpuStatus.status === 'not_installed' && 'Setting up GPU Acceleration backend...'}
                  {gpuStatus.status === 'installing' && 'Downloading & Installing Backend Components...'}
                  {gpuStatus.status === 'ready' && 'GPU Acceleration backend components are ready.'}
                  {gpuStatus.status === 'failed' && 'Installation failed.'}
                </p>

                {gpuStatus.status === 'ready' && gpuStatus.cuda_available && (
                  <span className="text-xs text-emerald-500/80 font-extrabold uppercase tracking-widest select-none">
                    GPU Acceleration is active and running
                  </span>
                )}
              </div>
            </div>

            {gpuStatus.status === 'failed' && gpuStatus.error_message && (
              <div className="text-xs text-destructive bg-destructive/10 p-3 rounded-lg border border-destructive/20 leading-relaxed">
                <strong className="font-semibold">Error:</strong> {gpuStatus.error_message}
                <button
                  type="button"
                  onClick={() => onToggleGpu(true)}
                  className="ml-3 underline text-primary hover:text-primary/80 font-semibold uppercase tracking-wider text-xs"
                >
                  Retry Installation
                </button>
              </div>
            )}

            {gpuStatus.logs && gpuStatus.logs.length > 0 && (
              <div className="space-y-1.5">
                <div className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">Installation Logs</div>
                <div className="h-40 overflow-y-auto p-3 bg-black/60 rounded-lg text-xs font-mono text-emerald-400 border border-border/10 space-y-1 select-text scrollbar-thin font-semibold text-left">
                  {gpuStatus.logs.map((log, i) => (
                    <div key={i} className="whitespace-pre-wrap leading-relaxed">{log}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="space-y-4 pt-6 border-t border-border/20">
        <div className="space-y-1">
          <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-2">
            <Cpu className="w-4 h-4 text-primary" />
            Multi-GPU Scaling
          </h3>
          <p className="text-xs text-muted-foreground leading-relaxed max-w-3xl">
            Run one conversion worker per GPU to process documents in parallel. Auto-detect uses every GPU automatically; manual lets you cap the count for a shared machine.
          </p>
        </div>

        <div className="p-4 rounded-xl border border-border/50 bg-card/45 space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-muted-foreground font-semibold uppercase tracking-wider">Detected GPUs:</span>
            <Badge variant={gpuWorkers && gpuWorkers.detected > 0 ? 'success' : 'secondary'} className="px-2.5 py-1 text-xs font-bold uppercase tracking-wider">
              {gpuWorkers ? gpuWorkers.detected : '-'}
            </Badge>
            {gpuWorkers && (
              <span className="text-muted-foreground/80">
                Effective workers: <strong className="text-foreground">{gpuWorkers.effective}</strong>
                {' '}Active backend: <strong className="text-foreground">{gpuWorkers.active}</strong>
              </span>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {([
              { value: 'auto', label: 'Auto (recommended)', desc: 'One worker per detected GPU. Zero config.' },
              { value: 'manual', label: 'Manual', desc: 'Cap the worker count. Clamped to the detected GPU count.' },
            ] as { value: GPUWorkerMode; label: string; desc: string }[]).map((opt) => {
              const selected = workerMode === opt.value
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => onWorkerModeChange(opt.value)}
                  className={cn(
                    'p-3 rounded-xl border text-left transition-all',
                    selected ? 'border-primary/60 bg-primary/10 text-foreground' : 'border-border/40 bg-card/35 text-muted-foreground hover:bg-muted/30 hover:text-foreground'
                  )}
                >
                  <span className="block text-xs font-semibold text-foreground">{opt.label}</span>
                  <span className="block text-xs text-muted-foreground mt-0.5 leading-normal">{opt.desc}</span>
                </button>
              )
            })}
          </div>

          {workerMode === 'manual' && gpuWorkers && (
            <div className="space-y-1.5 animate-fade-in">
              <div className="flex items-center justify-between">
                <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">
                  Worker Count
                </label>
                <span className="text-xs font-bold tabular-nums text-foreground">
                  {Math.min(workerCount, Math.max(1, gpuWorkers.detected))} / {Math.max(1, gpuWorkers.detected)} GPUs
                </span>
              </div>
              <input
                type="range"
                min={1}
                max={Math.max(1, gpuWorkers.detected)}
                step={1}
                value={Math.min(workerCount, Math.max(1, gpuWorkers.detected))}
                onChange={(e) => onWorkerCountChange(Number(e.target.value))}
                className="w-full h-6 appearance-none bg-transparent accent-primary cursor-pointer [&::-webkit-slider-runnable-track]:h-1.5 [&::-webkit-slider-runnable-track]:rounded-full [&::-webkit-slider-runnable-track]:bg-muted"
              />
              <p className="text-xs text-muted-foreground leading-normal">
                Each worker pins to one GPU and loads its own copy of the marker models (uses more VRAM).
              </p>
            </div>
          )}

          {gpuWorkers?.restart_required && (
            <p className="text-xs text-amber-600 dark:text-amber-400 leading-normal">
              Changing the worker count restarts the pool; restart the server to apply.
            </p>
          )}
          <div className="flex justify-end">
            <button
              type="button"
              onClick={onSaveWorkerScaling}
              disabled={isSavingWorkers || (workerMode === 'manual' && !!gpuWorkers && gpuWorkers.detected <= 0)}
              className="text-xs font-bold uppercase tracking-wider px-4 rounded-lg shadow-sm h-9 bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isSavingWorkers ? 'Saving...' : 'Apply'}
            </button>
          </div>
        </div>
      </div>
    </>
  )
}
