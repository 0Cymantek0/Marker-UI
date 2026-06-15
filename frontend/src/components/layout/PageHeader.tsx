import { cn } from '@/lib/utils'

interface PageHeaderProps {
  title: string
  description?: string
  children?: React.ReactNode
  className?: string
}

/**
 * Sticky Page Header with Gradual Blur Effect
 * 
 * Implements a "depth" effect using stacked backdrop-filter layers.
 * Each layer fades out at a different point, creating a perfectly smooth
 * curve from heavy blur at the top to zero blur at the bottom, extending
 * seamlessly below the content's border into the margin space.
 */
export function PageHeader({ title, description, children, className }: PageHeaderProps) {
  return (
    <div className={cn("sticky top-0 z-40 w-full mb-8", className)}>
      {/* 
        Gradual Blur Layers
        The container extends 2rem below the actual content height, perfectly 
        filling the mb-8 (2rem) space so static content isn't blurred, but 
        scrolling content blurs as it enters the margin area.
      */}
      <div className="absolute inset-x-0 top-0 h-[calc(100%+2rem)] pointer-events-none -z-10 select-none">
        {/* Layer 1: Base low blur, fades out at the very bottom (100%) */}
        <div 
          className="absolute inset-0 backdrop-blur-[2px]"
          style={{ maskImage: 'linear-gradient(to bottom, black 0%, black 50%, transparent 100%)', WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 50%, transparent 100%)' }}
        />
        {/* Layer 2: Low-mid blur, fades out at 85% */}
        <div 
          className="absolute inset-0 backdrop-blur-[4px]"
          style={{ maskImage: 'linear-gradient(to bottom, black 0%, black 40%, transparent 85%)', WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 40%, transparent 85%)' }}
        />
        {/* Layer 3: Mid blur, fades out at 70% */}
        <div 
          className="absolute inset-0 backdrop-blur-[8px]"
          style={{ maskImage: 'linear-gradient(to bottom, black 0%, black 30%, transparent 70%)', WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 30%, transparent 70%)' }}
        />
        {/* Layer 4: High blur, fades out at 55% */}
        <div 
          className="absolute inset-0 backdrop-blur-[16px]"
          style={{ maskImage: 'linear-gradient(to bottom, black 0%, black 20%, transparent 55%)', WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 20%, transparent 55%)' }}
        />
        {/* Layer 5: Max blur, fades out at 40% (mostly covers the opaque top part) */}
        <div 
          className="absolute inset-0 backdrop-blur-[24px]"
          style={{ maskImage: 'linear-gradient(to bottom, black 0%, black 10%, transparent 40%)', WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 10%, transparent 40%)' }}
        />
        
        {/* Optional background tint to improve legibility, fading out similarly */}
        <div 
          className="absolute inset-0 bg-background/60 dark:bg-background/40"
          style={{ maskImage: 'linear-gradient(to bottom, black 0%, black 60%, transparent 100%)', WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 60%, transparent 100%)' }}
        />
      </div>

      {/* Content Container */}
      <div className="relative px-4 md:px-6 pt-6 pb-5 bg-transparent">
        <div className="max-w-[1400px] mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="flex-1 min-w-0">
            <h2 className="text-2xl md:text-3xl font-extrabold tracking-tight text-foreground transition-all duration-300">
              {title}
            </h2>
            {description && (
              <p className="text-xs md:text-sm text-muted-foreground mt-1.5 max-w-3xl leading-relaxed">
                {description}
              </p>
            )}
          </div>
          {children && (
            <div className="flex items-center gap-3 shrink-0">
              {children}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
