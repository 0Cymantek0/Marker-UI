import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Terminal, Copy, Check, Trash2, ArrowDownCircle, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface TerminalLogProps {
  logs: string[]
  phase: string
  onClear?: () => void
  onClose?: () => void
}

export function TerminalLog({ logs, phase, onClear, onClose }: TerminalLogProps) {
  const [copied, setCopied] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Auto scroll to bottom
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [logs])

  const copyLogs = async () => {
    if (logs.length === 0) return
    const text = logs.join('\n')
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const getLogColorClass = (log: string) => {
    if (log.startsWith('[ERROR]')) return 'text-rose-400 font-semibold'
    if (log.startsWith('[WARN]') || log.startsWith('[WARNING]')) return 'text-amber-400'
    if (log.startsWith('[SUCCESS]')) return 'text-emerald-400 font-semibold'
    if (log.startsWith('[SYSTEM]')) return 'text-cyan-400 font-medium'
    return 'text-slate-300'
  }

  const getStatusText = () => {
    switch (phase) {
      case 'idle': return 'READY'
      case 'uploading': return 'UPLOADING'
      case 'processing': return 'CONVERTING'
      case 'completed': return 'FINISHED'
      case 'cancelled': return 'CANCELLED'
      case 'failed': return 'ERROR'
      default: return 'STANDBY'
    }
  }

  const isRunning = phase === 'uploading' || phase === 'processing'

  const terminalElement = (
    <div 
      className={cn(
        "flex flex-col select-text",
        onClose 
          ? "glass-card w-full max-w-3xl h-[450px] max-h-[85vh] bg-zinc-950 text-zinc-100 border border-zinc-800/80 rounded-2xl overflow-hidden shadow-2xl animate-modal-zoom-in" 
          : "glass-card h-[280px] bg-secondary/40 dark:bg-card border-border/80 rounded-xl overflow-hidden shadow-lg"
      )}
      data-testid="terminal-log"
    >
      {/* Header bar */}
      <div className={cn(
        "flex items-center justify-between px-4 py-2 border-b",
        onClose 
          ? "bg-zinc-900 border-zinc-800/80" 
          : "bg-muted/80 dark:bg-secondary/60 border-border/60"
      )}>
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4 text-primary animate-pulse" />
          <span className={cn(
            "text-xs font-mono font-bold tracking-wider",
            onClose ? "text-zinc-300" : "text-muted-foreground"
          )}>CONSOLE LOGS</span>
          <span className={cn(
            'ml-2 px-1.5 py-0.5 rounded text-xs font-mono font-bold tracking-widest',
            phase === 'idle' && (onClose ? 'bg-zinc-800 text-zinc-400' : 'bg-muted dark:bg-secondary text-muted-foreground'),
            phase === 'uploading' && 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 animate-pulse',
            phase === 'processing' && 'bg-primary/20 text-primary animate-pulse',
            phase === 'completed' && 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
            phase === 'cancelled' && 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
            phase === 'failed' && 'bg-rose-500/10 text-rose-600 dark:text-rose-400'
          )}>
            {getStatusText()}
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          {logs.length > 0 && (
            <>
              <Button
                variant="ghost"
                size="icon"
                onClick={copyLogs}
                className={cn(
                  "w-7 h-7 hover:bg-muted dark:hover:bg-secondary text-muted-foreground hover:text-foreground",
                  onClose && "hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200"
                )}
                title="Copy logs"
              >
                {copied ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
              </Button>
              {onClear && !isRunning && (
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={onClear}
                  className={cn(
                    "w-7 h-7 hover:bg-muted dark:hover:bg-secondary text-muted-foreground hover:text-rose-500",
                    onClose && "hover:bg-zinc-800 text-zinc-400 hover:text-rose-400"
                  )}
                  title="Clear console"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </Button>
              )}
            </>
          )}
          {onClose && (
            <Button
              variant="ghost"
              size="icon"
              onClick={onClose}
              className="w-7 h-7 hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200"
              title="Close console"
            >
              <X className="w-4 h-4" />
            </Button>
          )}
        </div>
      </div>

      {/* Terminal lines */}
      <div
        ref={containerRef}
        className={cn(
          "flex-1 p-4 overflow-y-auto font-mono text-xs space-y-1.5 scrollbar-thin scrollbar-track-transparent",
          onClose 
            ? "scrollbar-thumb-zinc-800 bg-zinc-950 text-zinc-300" 
            : "scrollbar-thumb-muted-foreground/30 text-foreground/80"
        )}
      >
        {logs.length === 0 ? (
          <div className={cn(
            "flex flex-col items-center justify-center h-full italic select-none",
            onClose ? "text-zinc-500" : "text-muted-foreground/80"
          )}>
            <ArrowDownCircle className="w-6 h-6 mb-2 stroke-[1.5]" />
            <span>Console standby. Submit a file to begin extraction...</span>
          </div>
        ) : (
          <>
            {logs.map((log, index) => (
              <div key={index} className={cn('leading-relaxed break-all', getLogColorClass(log))}>
                <span className={cn("mr-2 select-none", onClose ? "text-zinc-600" : "text-muted-foreground/60")}>&gt;</span>
                {log}
              </div>
            ))}
            {isRunning && (
              <div className="flex items-center text-primary font-bold">
                <span className={cn("mr-2 select-none", onClose ? "text-zinc-600" : "text-muted-foreground/60")}>&gt;</span>
                <span className="animate-pulse">_</span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )

  if (onClose) {
    return createPortal(
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm animate-overlay-fade-in">
        {terminalElement}
      </div>,
      document.body
    )
  }

  return terminalElement
}
