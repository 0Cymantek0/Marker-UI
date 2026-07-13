import { Terminal, ShieldCheck } from 'lucide-react'
import type { ModelTrackerStatus } from '@/lib/api'

interface DeveloperConsoleProps {
  status: ModelTrackerStatus
}

function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const dm = decimals < 0 ? 0 : decimals
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i]
}

export function DeveloperConsole({ status }: DeveloperConsoleProps) {
  const isDownloading = status.overall.status === 'downloading'
  const isCompleted = status.overall.status === 'completed' || status.initialized

  // Collect all files currently downloading
  const activeFiles = Object.entries(status.models)
    .filter(([_, m]) => m.status === 'downloading')
    .flatMap(([_, m]) =>
      Object.entries(m.files)
        .filter(([_, f]) => f.status === 'downloading')
        .map(([filename, f]) => ({
          filename,
          downloaded: f.downloaded_bytes,
          total: f.total_bytes,
        }))
    )

  return (
    <div className="w-full bg-secondary/30 border border-border/40 rounded-xl overflow-hidden font-mono text-xs md:text-sm">
      {/* Terminal Header Bar */}
      <div className="flex items-center justify-between px-4 py-3 bg-secondary/50 border-b border-border/40 select-none">
        <div className="flex items-center gap-1.5">
          {/* Mock Window Dots */}
          <div className="w-2.5 h-2.5 rounded-full bg-border/40" />
          <div className="w-2.5 h-2.5 rounded-full bg-border/40" />
          <div className="w-2.5 h-2.5 rounded-full bg-border/40" />
        </div>
        <span className="text-xs text-muted-foreground/65 font-bold uppercase tracking-widest flex items-center gap-2">
          <Terminal className="w-3.5 h-3.5 text-muted-foreground/40" />
          engine-diagnostics.sh
        </span>
        <div className="w-12" /> {/* spacer for center alignment */}
      </div>

      {/* Terminal Content Screen */}
      <div className="p-5 space-y-3 min-h-[180px] max-h-[240px] overflow-y-auto text-foreground/75">
        <div className="flex items-center gap-2 text-muted-foreground/40">
          <span>$</span>
          <span>./marker-engine --init --verbose</span>
        </div>

        <div className="text-foreground/80">
          [SYSTEM] Initializing Marker AI Conversion Engine...
        </div>

        {status.error && (
          <div className="text-destructive animate-pulse">
            [ERROR] Setup halted: {status.error}
          </div>
        )}

        {isDownloading && activeFiles.length > 0 && (
          <div className="space-y-1">
            <div className="text-foreground/40">[DOWNLOAD] Fetching remote weights:</div>
            {activeFiles.map((file) => (
              <div
                key={file.filename}
                className="pl-4 flex items-center justify-between text-muted-foreground hover:text-foreground transition-colors"
              >
                <span className="truncate max-w-[55%] font-mono opacity-80">
                  &gt; {file.filename}
                </span>
                <span className="shrink-0 text-foreground font-mono text-xs bg-secondary/60 border border-border/40 px-2 py-0.5 rounded">
                  {formatBytes(file.downloaded)}
                  {file.total > 0 && ` / ${formatBytes(file.total)}`}
                </span>
              </div>
            ))}
          </div>
        )}

        {isCompleted && (
          <div className="space-y-1.5 animate-fade-in">
            <div className="text-primary flex items-center gap-1">
              <ShieldCheck className="w-4 h-4 shrink-0" />
              <span>[SUCCESS] All weights verification completed.</span>
            </div>
            <div className="text-muted-foreground/60 pl-5">
              Engine ready. Directing to workspace...
            </div>
          </div>
        )}

        {!isDownloading && !isCompleted && !status.error && (
          <div className="text-muted-foreground/40">[SYSTEM] Awaiting setup instruction...</div>
        )}
      </div>
    </div>
  )
}
