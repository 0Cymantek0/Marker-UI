import { useState, useCallback, useMemo } from 'react'
import { Download, Copy, Check, FileText, Code, Braces, Eye, FileSpreadsheet, Loader2 } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { ImageUnderstandingBadge } from '@/components/features/image-understanding/ImageUnderstandingBadge'
import type { ImageUnderstandingMeta } from '@/lib/api'

type OutputTab = 'markdown' | 'html' | 'json' | 'raw'

const ALL_TABS: { value: OutputTab; label: string; icon: any; formatKey?: string }[] = [
  { value: 'markdown', label: 'Markdown', icon: FileText, formatKey: 'markdown' },
  { value: 'html', label: 'HTML', icon: Code, formatKey: 'html' },
  { value: 'json', label: 'JSON', icon: Braces, formatKey: 'json' },
  { value: 'raw', label: 'Raw Text', icon: Eye },
]

interface OutputViewerProps {
  content: string | null
  formats?: Record<string, string> | null
  availableFormats?: string[]
  onRegenerate?: (format: string) => Promise<void>
  onDownload: (format: string) => void
  imageUnderstanding?: ImageUnderstandingMeta[] | null
  filename?: string
}

export function OutputViewer({
  content,
  formats = null,
  availableFormats = [],
  onRegenerate,
  onDownload,
  imageUnderstanding,
  filename
}: OutputViewerProps) {
  const [activeTab, setActiveTab] = useState<OutputTab>('markdown')
  const [copied, setCopied] = useState(false)
  const [regenerating, setRegenerating] = useState<string | null>(null)

  const metaByFilename = useMemo(() => {
    const m = new Map<string, { meta: ImageUnderstandingMeta; index: number }>()
    ;(imageUnderstanding ?? []).forEach((meta, i) => {
      m.set(meta.image_name, { meta, index: i + 1 })
    })
    return m
  }, [imageUnderstanding])
  const metaTotal = metaByFilename.size

  const isMultiSupported = useMemo(() => {
    if (!filename) return true
    const ext = filename.split(/[?#]/)[0].split('.').pop()?.toLowerCase()
    return ext ? ['.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tiff', '.bmp', '.gif', '.epub'].includes(`.${ext}`) : false
  }, [filename])

  const visibleTabs = useMemo(() => {
    if (isMultiSupported) return ALL_TABS
    return ALL_TABS.filter((t) => t.value === 'markdown' || t.value === 'raw')
  }, [isMultiSupported])

  const activeContent = useMemo(() => {
    if (activeTab === 'raw') return content
    const fmtKey = visibleTabs.find((t) => t.value === activeTab)?.formatKey
    if (fmtKey && formats?.[fmtKey]) return formats[fmtKey]
    return content
  }, [activeTab, formats, content, visibleTabs])

  const copyToClipboard = useCallback(async () => {
    if (!activeContent) return
    await navigator.clipboard.writeText(activeContent)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [activeContent])

  const handleTabClick = useCallback(async (tab: OutputTab) => {
    setActiveTab(tab)
    const fmtKey = visibleTabs.find((t) => t.value === tab)?.formatKey
    if (!fmtKey) return
    if (formats?.[fmtKey]) return
    if (regenerating) return
    if (!onRegenerate) return

    setRegenerating(fmtKey)
    try {
      await onRegenerate(fmtKey)
    } finally {
      setRegenerating(null)
    }
  }, [formats, regenerating, onRegenerate, visibleTabs])

  if (!content && !formats) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center glass-card border border-border/40 min-h-[300px]">
        <div className="flex items-center justify-center w-12 h-12 rounded-full bg-muted text-muted-foreground/40 mb-4 select-none">
          <FileSpreadsheet className="w-6 h-6 stroke-[1.5]" />
        </div>
        <p className="text-sm font-semibold text-muted-foreground">Converted output will appear here</p>
        <p className="text-xs text-muted-foreground/60 mt-1 max-w-[280px] leading-relaxed">
          Upload a source document and press Convert to stream logs and generate your output.
        </p>
      </div>
    )
  }

  const isTabAvailable = (tab: typeof ALL_TABS[number]) => {
    if (!tab.formatKey) return true
    return !!formats?.[tab.formatKey]
  }

  const isTabRegenerating = (tab: typeof ALL_TABS[number]) => {
    return tab.formatKey === regenerating
  }

  return (
    <div className="glass-card border border-border/40 overflow-hidden animate-fade-in shadow-sm flex flex-col h-[400px]">

      {/* Tab bar header */}
      <div className="flex items-center justify-between border-b border-border/30 px-2 bg-muted/20">
        <div className="flex gap-1 py-1">
          {visibleTabs.map((tab) => {
            const isActive = activeTab === tab.value
            const available = isTabAvailable(tab)
            const loading = isTabRegenerating(tab)
            return (
              <button
                key={tab.value}
                type="button"
                onClick={() => handleTabClick(tab.value)}
                disabled={loading}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all duration-200',
                  isActive
                    ? 'bg-card text-foreground shadow-sm border border-border/30'
                    : available
                      ? 'text-muted-foreground hover:text-foreground hover:bg-muted/30'
                      : 'text-muted-foreground/40 hover:text-muted-foreground/60 hover:bg-muted/20'
                )}
              >
                {loading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <tab.icon className="w-3.5 h-3.5" />
                )}
                {tab.label}
              </button>
            )
          })}
        </div>

        <div className="flex items-center gap-1.5 py-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={copyToClipboard}
            className="h-8 px-2.5 rounded-lg text-xs font-semibold hover:bg-muted/50 transition-colors"
          >
            {copied ? (
              <Check className="w-3.5 h-3.5 text-emerald-500 mr-1.5" />
            ) : (
              <Copy className="w-3.5 h-3.5 text-muted-foreground mr-1.5" />
            )}
            <span>{copied ? 'Copied' : 'Copy'}</span>
          </Button>

          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDownload(activeTab)}
            className="h-8 px-2.5 rounded-lg text-xs font-semibold hover:bg-muted/50 transition-colors"
          >
            <Download className="w-3.5 h-3.5 text-muted-foreground mr-1.5" />
            <span>Download</span>
          </Button>
        </div>
      </div>

      {/* Content panel */}
      <div className={cn('flex-1 p-4 overflow-auto font-mono text-xs leading-relaxed text-foreground bg-muted/10 border border-border/40')}>
        {regenerating === ALL_TABS.find((t) => t.value === activeTab)?.formatKey ? (
          <div className="flex flex-col items-center justify-center h-full gap-3">
            <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
            <p className="text-xs text-muted-foreground font-semibold">Regenerating {activeTab} format...</p>
          </div>
        ) : (
          <>
            {activeTab === 'markdown' && (
              <div className="prose prose-sm dark:prose-invert max-w-none">
                {imageUnderstanding && imageUnderstanding.length > 0 && (
                  <div className="not-prose flex flex-wrap items-center gap-x-6 gap-y-3 p-4 mb-5 rounded-2xl bg-slate-50 dark:bg-slate-900/40 border border-slate-200/60 dark:border-slate-800/60 shadow-sm">
                    <span className="text-[10px] font-extrabold tracking-widest text-slate-400 dark:text-slate-500 uppercase w-full mb-0.5">
                      VLM Processed Images ({imageUnderstanding.length})
                    </span>
                    {imageUnderstanding.map((meta, i) => (
                      <div key={meta.image_name} className="flex items-center gap-2 pr-4 border-r border-slate-200 dark:border-slate-800 last:border-0">
                        <span className="text-[10px] font-mono text-muted-foreground max-w-[120px] truncate" title={meta.image_name}>
                          {meta.image_name}
                        </span>
                        <ImageUnderstandingBadge
                          meta={meta}
                          index={i + 1}
                          total={imageUnderstanding.length}
                          inline
                        />
                      </div>
                    ))}
                  </div>
                )}
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    pre: ({ children }) => (
                      <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed bg-transparent p-0 select-text">
                        {children}
                      </pre>
                    ),
                    code: ({ className, children, ...props }: any) => (
                      <code className={cn('font-mono text-xs', className)} {...props}>
                        {children}
                      </code>
                    ),
                    img: ({ src, alt, ...props }: any) => {
                      const filename = String(src ?? '').split('/').pop() ?? ''
                      const entry = metaByFilename.get(filename)
                      return (
                        <span className="relative inline-block align-middle my-1">
                          <img src={src} alt={alt} {...props} />
                          {entry && (
                            <ImageUnderstandingBadge
                              meta={entry.meta}
                              index={entry.index}
                              total={metaTotal}
                            />
                          )}
                        </span>
                      )
                    },
                  }}
                >
                  {activeContent ?? ''}
                </ReactMarkdown>
              </div>
            )}
            {activeTab === 'html' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text">
                {activeContent}
              </pre>
            )}
            {activeTab === 'json' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text">
                {activeContent}
              </pre>
            )}
            {activeTab === 'raw' && (
              <pre className="whitespace-pre-wrap font-mono text-xs select-text text-slate-700 dark:text-slate-300">
                {activeContent}
              </pre>
            )}
          </>
        )}
      </div>
    </div>
  )
}
