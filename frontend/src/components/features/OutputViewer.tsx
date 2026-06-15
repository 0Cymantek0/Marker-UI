import { useState, useCallback } from 'react'
import { Download, Copy, Check, FileText, Code, Braces, Eye, FileSpreadsheet } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

type OutputTab = 'markdown' | 'html' | 'json' | 'raw'

interface OutputViewerProps {
  content: string | null
  onDownload: () => void
}

const TABS: { value: OutputTab; label: string; icon: any }[] = [
  { value: 'markdown', label: 'Markdown', icon: FileText },
  { value: 'html', label: 'HTML', icon: Code },
  { value: 'json', label: 'JSON', icon: Braces },
  { value: 'raw', label: 'Raw Text', icon: Eye },
]

export function OutputViewer({ content, onDownload }: OutputViewerProps) {
  const [activeTab, setActiveTab] = useState<OutputTab>('markdown')
  const [copied, setCopied] = useState(false)

  const copyToClipboard = useCallback(async () => {
    if (!content) return
    await navigator.clipboard.writeText(content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [content])

  if (!content) {
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

  const getTabClass = () => {
    return 'text-foreground bg-muted/10 border border-border/40'
  }

  return (
    <div className="glass-card border border-border/40 overflow-hidden animate-fade-in shadow-sm flex flex-col h-[400px]">
      
      {/* Tab bar header */}
      <div className="flex items-center justify-between border-b border-border/30 px-2 bg-muted/20">
        <div className="flex gap-1 py-1">
          {TABS.map((tab) => {
            const isActive = activeTab === tab.value
            return (
              <button
                key={tab.value}
                type="button"
                onClick={() => setActiveTab(tab.value)}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all duration-200',
                  isActive
                    ? 'bg-card text-foreground shadow-sm border border-border/30'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted/30'
                )}
              >
                <tab.icon className="w-3.5 h-3.5" />
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
            onClick={onDownload}
            className="h-8 px-2.5 rounded-lg text-xs font-semibold hover:bg-muted/50 transition-colors"
          >
            <Download className="w-3.5 h-3.5 text-muted-foreground mr-1.5" />
            <span>Download</span>
          </Button>
        </div>
      </div>

      {/* Content panel */}
      <div className={cn('flex-1 p-4 overflow-auto font-mono text-xs leading-relaxed', getTabClass())}>
        {activeTab === 'markdown' && (
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                // Code blocks keep the monospace look the raw tabs use.
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
              }}
            >
              {content}
            </ReactMarkdown>
          </div>
        )}
        {activeTab === 'html' && (
          <pre className="whitespace-pre-wrap font-mono text-xs select-text">
            {content}
          </pre>
        )}
        {activeTab === 'json' && (
          <pre className="whitespace-pre-wrap font-mono text-xs select-text">
            {content}
          </pre>
        )}
        {activeTab === 'raw' && (
          <pre className="whitespace-pre-wrap font-mono text-xs select-text text-slate-700 dark:text-slate-300">
            {content}
          </pre>
        )}
      </div>
    </div>
  )
}
