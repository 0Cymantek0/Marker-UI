import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { cn } from '@/lib/utils'

interface CopyableValueProps {
  /** Full value written to the clipboard. */
  value: string
  /** Compact display form (caller decides truncation). */
  display?: string
  className?: string
}

/**
 * Mono value with an explicit copy control. The full value is exposed via the
 * button's title so truncated tokens remain inspectable without copying.
 */
export function CopyableValue({ value, display, className }: CopyableValueProps) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    }
  }, [])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard unavailable (permissions / http context): the value is
      // still selectable text, so fail quietly without a false "Copied".
    }
  }

  return (
    <span className={cn('inline-flex items-center gap-1.5 min-w-0', className)}>
      <span className="font-mono text-xs break-all text-foreground/90 select-all" title={value}>
        {display ?? value}
      </span>
      <button
        type="button"
        onClick={() => void handleCopy()}
        title={`Copy ${value}`}
        aria-label={`Copy ${value}`}
        className={cn(
          'shrink-0 rounded-md p-1 transition-colors',
          copied
            ? 'text-emerald-600 dark:text-emerald-400'
            : 'text-muted-foreground hover:text-foreground hover:bg-muted/60'
        )}
      >
        {copied ? <Check className="w-3.5 h-3.5" aria-label="Copied" /> : <Copy className="w-3.5 h-3.5" />}
        <span className="sr-only">{copied ? 'Copied' : 'Copy'}</span>
      </button>
    </span>
  )
}
