import * as React from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, Check } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface SelectOption {
  value: string
  label: string
}

interface SelectProps {
  value: string
  onChange: (value: string) => void
  options: SelectOption[]
  className?: string
  disabled?: boolean
}

export function Select({ value, onChange, options, className, disabled }: SelectProps) {
  const [isOpen, setIsOpen] = React.useState(false)
  const containerRef = React.useRef<HTMLDivElement>(null)
  const dropdownRef = React.useRef<HTMLDivElement>(null)
  const [coords, setCoords] = React.useState<{ top: number; left: number; width: number } | null>(null)

  const selectedOption = options.find((opt) => opt.value === value)

  React.useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      const target = event.target as Node
      if (
        containerRef.current && !containerRef.current.contains(target) &&
        dropdownRef.current && !dropdownRef.current.contains(target)
      ) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  React.useLayoutEffect(() => {
    if (!isOpen) {
      setCoords(null)
      return
    }

    function updatePosition() {
      if (containerRef.current) {
        const rect = containerRef.current.getBoundingClientRect()
        setCoords({
          top: rect.bottom + window.scrollY,
          left: rect.left + window.scrollX,
          width: rect.width,
        })
      }
    }

    updatePosition()

    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)

    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [isOpen])

  return (
    <div ref={containerRef} className={cn('relative w-full min-w-0', isOpen && 'z-30', className)}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setIsOpen(!isOpen)}
        className={cn(
          'flex h-10 min-h-10 items-center justify-between w-full px-3 py-2 bg-background/40 hover:bg-muted/30 border border-border/50 rounded-lg text-sm font-semibold text-foreground transition-all duration-200 focus:outline-none text-left',
          isOpen && 'bg-muted/20',
          disabled && 'opacity-50 cursor-not-allowed pointer-events-none'
        )}
      >
        <span className="truncate">{selectedOption?.label || 'Select option...'}</span>
        <ChevronDown 
          className={cn(
            'w-3.5 h-3.5 text-muted-foreground transition-transform duration-200 shrink-0 ml-2', 
            isOpen && 'transform rotate-180 text-foreground'
          )} 
        />
      </button>

      {isOpen && coords && !disabled && createPortal(
        <div
          ref={dropdownRef}
          style={{
            position: 'absolute',
            top: `${coords.top + 6}px`,
            left: `${coords.left}px`,
            minWidth: `${coords.width}px`,
          }}
          className="z-50 w-max max-w-[min(22rem,80vw)] origin-top-right rounded-lg border border-border bg-background shadow-lg py-1 max-h-60 overflow-y-auto focus:outline-none animate-in fade-in slide-in-from-top-1 duration-100"
        >
          {options.map((option) => {
            const isSelected = option.value === value
            return (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  onChange(option.value)
                  setIsOpen(false)
                }}
                className={cn(
                  'flex items-center justify-between w-full gap-2 px-3 py-2 text-sm text-left transition-colors duration-150',
                  isSelected
                    ? 'bg-primary/10 text-primary font-bold'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted/60'
                )}
              >
                <span className="whitespace-nowrap">{option.label}</span>
                {isSelected && <Check className="w-3.5 h-3.5 text-primary shrink-0" />}
              </button>
            )
          })}
        </div>,
        document.body
      )}
    </div>
  )
}
