import { useState, useEffect } from 'react'
import { CheckCircle2, AlertTriangle, Loader2, TestTube } from 'lucide-react'
import { cn } from '@/lib/utils'

interface TestConnectionButtonProps {
  apiKey: string
  onTest: (key: string) => Promise<{ success: boolean; message: string }>
  disabled?: boolean
}

export function TestConnectionButton({
  apiKey,
  onTest,
  disabled
}: TestConnectionButtonProps) {
  const [state, setState] = useState<'idle' | 'loading' | 'success' | 'error'>('idle')

  // Reset state back to idle if the api key changes
  useEffect(() => {
    setState('idle')
  }, [apiKey])

  const handleClick = async (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (state === 'loading') return
    setState('loading')
    
    const res = await onTest(apiKey)
    if (res.success) {
      setState('success')
      // Auto reset to idle after 4 seconds
      setTimeout(() => {
        setState((prev) => (prev === 'success' ? 'idle' : prev))
      }, 4000)
    } else {
      setState('error')
      // Auto reset to idle after 4 seconds
      setTimeout(() => {
        setState((prev) => (prev === 'error' ? 'idle' : prev))
      }, 4000)
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={disabled || state === 'loading'}
      className={cn(
        "h-9 px-3.5 rounded-lg text-xs font-bold uppercase tracking-wider border transition-all duration-300 flex items-center justify-center gap-1.5 shrink-0 select-none shadow-sm min-w-[76px]",
        state === 'idle' && "bg-secondary/40 border-border/60 text-foreground hover:bg-secondary/80 hover:border-border active:scale-[0.98]",
        state === 'loading' && "bg-secondary/20 border-border/40 text-muted-foreground cursor-wait animate-pulse-soft",
        state === 'success' && "bg-emerald-500/90 hover:bg-emerald-500 border-emerald-600 text-white animate-pop shadow-emerald-500/10",
        state === 'error' && "bg-rose-500/90 hover:bg-rose-500 border-rose-600 text-white animate-shake shadow-rose-500/10",
        disabled && "opacity-50 cursor-not-allowed pointer-events-none"
      )}
    >
      {state === 'idle' && (
        <>
          <TestTube className="w-3.5 h-3.5 text-muted-foreground/80 transition-colors group-hover:text-foreground" />
          <span>Test</span>
        </>
      )}
      {state === 'loading' && (
        <>
          <Loader2 className="w-3.5 h-3.5 animate-spin text-primary" />
          <span>Testing</span>
        </>
      )}
      {state === 'success' && (
        <>
          <CheckCircle2 className="w-3.5 h-3.5" />
          <span>Passed</span>
        </>
      )}
      {state === 'error' && (
        <>
          <AlertTriangle className="w-3.5 h-3.5" />
          <span>Failed</span>
        </>
      )}
    </button>
  )
}
