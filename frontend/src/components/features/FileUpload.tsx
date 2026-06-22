import { useCallback, useRef, useState } from 'react'
import { UploadCloud, FileText, X, FileImage, FileCode, FileSpreadsheet, FolderOpen, Files, Link, ChevronDown, ChevronUp, Loader2, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'
import { Select, type SelectOption } from '@/components/ui/select'
import { browseFiles, browseFolder } from '@/lib/api'
import type { ConverterPlanResponse } from '@/lib/api'
import { RoutingAnalysis } from './conversion/RoutingAnalysis'
import { toast } from 'sonner'

const ACCEPTED_EXTENSIONS = '.pdf,.docx,.xlsx,.pptx,.epub,.html,.htm,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tiff'

interface FileUploadProps {
  onFilesSelect: (files: File[]) => void
  selectedFiles: File[]
  onRemoveFile: (index: number) => void
  onClearAll: () => void
  fileEngineControls?: SourceEngineControl[]
  localPathEngineControls?: SourceEngineControl[]
  localPaths: string
  onLocalPathsChange: (paths: string) => void
  outputDir: string
  onOutputDirChange: (dir: string) => void
  disabled?: boolean
}

interface SourceEngineControl {
  key: string
  value: string
  options: SelectOption[]
  status: string
  title?: string
  onChange: (value: string) => void
  plan?: ConverterPlanResponse | null
  loading?: boolean
  error?: string | null
}

function formatSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`
}

function getFileIcon(name: string) {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  const imageExts = ['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tiff']
  const spreadsheetExts = ['xlsx', 'xls', 'csv']
  const codeExts = ['html', 'htm', 'json', 'xml', 'js', 'ts']

  if (imageExts.includes(ext)) return FileImage
  if (spreadsheetExts.includes(ext)) return FileSpreadsheet
  if (codeExts.includes(ext)) return FileCode
  return FileText
}

function PlanDetails({ control }: { control: SourceEngineControl }) {
  if (control.loading) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground rounded-lg border border-border/30 bg-card/30 p-3">
        <Loader2 className="w-3.5 h-3.5 animate-spin text-primary" />
        Checking route...
      </div>
    )
  }
  if (control.error) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-amber-700 dark:text-amber-400 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
        <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
        {control.error}
      </div>
    )
  }
  return control.plan ? <RoutingAnalysis plan={control.plan} /> : null
}

export function FileUpload({
  onFilesSelect,
  selectedFiles,
  onRemoveFile,
  onClearAll,
  fileEngineControls,
  localPathEngineControls,
  localPaths,
  onLocalPathsChange,
  outputDir,
  onOutputDirChange,
  disabled
}: FileUploadProps) {
  const [activeTab, setActiveTab] = useState<'upload' | 'local'>('upload')
  const [isDragOver, setIsDragOver] = useState(false)
  const [expandedPlans, setExpandedPlans] = useState<Record<string, boolean>>({})
  const inputRef = useRef<HTMLInputElement>(null)

  const toggleExpandPlan = (key: string) => {
    setExpandedPlans((prev) => ({
      ...prev,
      [key]: !prev[key],
    }))
  }

  const handleBrowseLocalFiles = async () => {
    try {
      const res = await browseFiles()
      if (res.paths && res.paths.length > 0) {
        const newPaths = res.paths.join('\n')
        onLocalPathsChange(localPaths ? `${localPaths}\n${newPaths}` : newPaths)
        toast.success(`Added ${res.paths.length} file path(s)`)
      }
    } catch (err) {
      console.error('Failed to browse files:', err)
      toast.error('Failed to open file browser')
    }
  }

  const handleBrowseOutputDir = async () => {
    try {
      const res = await browseFolder()
      if (res.path) {
        onOutputDirChange(res.path)
        toast.success('Output folder updated')
      }
    } catch (err) {
      console.error('Failed to browse folder:', err)
      toast.error('Failed to open folder browser')
    }
  }

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      if (disabled) return
      setIsDragOver(false)
      const droppedFiles = Array.from(e.dataTransfer.files)
      if (droppedFiles.length > 0) {
        onFilesSelect(droppedFiles)
      }
    },
    [onFilesSelect, disabled]
  )

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (disabled) return
      const selected = Array.from(e.target.files || [])
      if (selected.length > 0) {
        onFilesSelect(selected)
      }
      e.target.value = ''
    },
    [onFilesSelect, disabled]
  )

  return (
    <div className="space-y-5">
      {/* Tabs */}
      <div className="flex bg-muted/65 p-1 rounded-xl">
        <button
          type="button"
          disabled={disabled}
          onClick={() => setActiveTab('upload')}
          className={cn(
            'flex-1 flex items-center justify-center gap-2 py-2 text-xs font-semibold uppercase tracking-wider rounded-lg transition-all',
            activeTab === 'upload'
              ? 'bg-background text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground'
          )}
        >
          <Files className="w-3.5 h-3.5" />
          Upload Files
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => setActiveTab('local')}
          className={cn(
            'flex-1 flex items-center justify-center gap-2 py-2 text-xs font-semibold uppercase tracking-wider rounded-lg transition-all',
            activeTab === 'local'
              ? 'bg-background text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground'
          )}
        >
          <Link className="w-3.5 h-3.5" />
          Local Paths
        </button>
      </div>

      {activeTab === 'upload' ? (
        <div className="space-y-4">
          {/* Drag & Drop Zone */}
          <div
            onDragOver={(e) => {
              e.preventDefault()
              if (disabled) return
              setIsDragOver(true)
            }}
            onDragLeave={() => setIsDragOver(false)}
            onDrop={handleDrop}
            onClick={() => {
              if (!disabled) inputRef.current?.click()
            }}
            className={cn(
              'relative flex flex-col items-center justify-center gap-4 p-8 rounded-2xl border-2 border-dashed cursor-pointer transition-all duration-300 ease-out select-none',
              isDragOver
                ? 'border-primary bg-primary/5 scale-[1.01] shadow-md shadow-primary/5'
                : 'border-border/60 hover:border-primary/50 hover:bg-muted/20',
              disabled && 'opacity-50 pointer-events-none'
            )}
          >
            <input
              ref={inputRef}
              type="file"
              multiple
              accept={ACCEPTED_EXTENSIONS}
              onChange={handleChange}
              className="hidden"
            />

            <div className={cn(
              'flex items-center justify-center w-12 h-12 rounded-xl transition-all duration-300 shadow-sm border border-transparent',
              isDragOver
                ? 'bg-primary/20 text-primary scale-110 border-primary/20'
                : 'bg-muted/80 text-muted-foreground hover:bg-muted'
            )}>
              <UploadCloud className={cn('w-5.5 h-5.5 transition-transform duration-300', isDragOver && 'animate-bounce')} />
            </div>

            <div className="text-center space-y-1">
              <p className="text-xs font-bold text-foreground">
                {isDragOver ? 'Release to upload your files' : 'Drag & drop files here or click to browse'}
              </p>
              <p className="text-[10px] text-muted-foreground leading-relaxed">
                PDF, DOCX, XLSX, PPTX, EPUB, HTML, or images (select multiple)
              </p>
            </div>
          </div>

          {/* Selected Files List */}
          {selectedFiles.length > 0 && (
            <div className="space-y-2 max-h-[200px] overflow-y-auto pr-1">
              <div className="flex items-center justify-between text-[10px] font-bold text-muted-foreground tracking-wider uppercase">
                <span>Selected Files ({selectedFiles.length})</span>
                <button
                  type="button"
                  onClick={onClearAll}
                  className="hover:text-destructive transition-colors"
                >
                  Clear All
                </button>
              </div>

              <div className="space-y-1.5">
                {selectedFiles.map((file, idx) => {
                  const Icon = getFileIcon(file.name)
                  const engineControl = fileEngineControls?.[idx]
                  const key = engineControl?.key ?? `upload-${idx}`
                  const isExpanded = expandedPlans[key]
                  const hasPlanDetails = !!engineControl && (!!engineControl.plan || !!engineControl.loading || !!engineControl.error)

                  return (
                    <div key={idx} className="space-y-1 animate-fade-in">
                      <div
                        className="flex items-center gap-2.5 p-2 rounded-xl border border-border/40 bg-card/50 text-left"
                      >
                        <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-primary/10 text-primary shrink-0">
                          <Icon className="w-4 h-4" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-semibold truncate text-foreground">{file.name}</p>
                          <p className="text-[10px] text-muted-foreground">{formatSize(file.size)}</p>
                          {engineControl && (
                            <p className="text-[9px] text-muted-foreground truncate" title={engineControl.title}>
                              {engineControl.status}
                            </p>
                          )}
                        </div>
                        {engineControl && (
                          <div className="w-32 shrink-0" onClick={(e) => e.stopPropagation()}>
                            <Select
                              value={engineControl.value}
                              options={engineControl.options}
                              onChange={engineControl.onChange}
                              disabled={disabled}
                              className="w-32 md:w-32"
                            />
                          </div>
                        )}
                        {hasPlanDetails && (
                          <button
                            type="button"
                            onClick={() => toggleExpandPlan(key)}
                            className="p-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-all"
                            aria-label={isExpanded ? "Collapse plan" : "Expand plan"}
                          >
                            {isExpanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => onRemoveFile(idx)}
                          disabled={disabled}
                          className="p-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-all disabled:opacity-50"
                          aria-label="Remove file"
                        >
                          <X className="w-3.5 h-3.5" />
                        </button>
                      </div>

                      {isExpanded && engineControl && (
                        <div className="pl-4 pr-1 pb-1 animate-slide-down">
                          <PlanDetails control={engineControl} />
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      ) : (
        /* Local paths text area */
        <div className="space-y-2 animate-fade-in">
          <div className="flex items-center justify-between">
            <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block">
              Absolute Local File Paths (One Per Line)
            </label>
            <button
              type="button"
              onClick={handleBrowseLocalFiles}
              disabled={disabled}
              className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-primary hover:text-primary/80 transition-all hover:translate-x-0.5 active:scale-95"
            >
              <FolderOpen className="w-3 h-3" />
              Browse Files...
            </button>
          </div>
          <textarea
            disabled={disabled}
            value={localPaths}
            onChange={(e) => onLocalPathsChange(e.target.value)}
            placeholder="e.g. C:\path\to\document.pdf&#10;e.g. C:\path\to\report.docx"
            className="w-full h-[120px] bg-background/50 border border-border/80 rounded-xl p-3 text-xs placeholder:text-muted-foreground/50 focus:outline-none transition-all font-mono leading-relaxed"
          />
          <p className="text-[10px] text-muted-foreground/80 leading-normal">
            * Backend reads files directly from your computer. Outputs will save to the same folder as the input file unless a custom folder is specified below.
          </p>
          {localPathEngineControls && localPathEngineControls.length > 0 && (
            <div className="space-y-1.5 pt-1">
              {localPathEngineControls.map((control) => {
                const isExpanded = expandedPlans[control.key]
                const hasPlanDetails = !!control.plan || !!control.loading || !!control.error
                return (
                  <div key={control.key} className="space-y-1">
                    <div
                      className="flex items-center gap-2 p-2 rounded-xl border border-border/40 bg-card/50"
                    >
                      <FileText className="w-3.5 h-3.5 text-primary shrink-0" />
                      <div className="flex-1 min-w-0">
                        <p className="text-[10px] font-semibold text-foreground truncate" title={control.title}>
                          {control.title}
                        </p>
                        <p className="text-[9px] text-muted-foreground truncate">
                          {control.status}
                        </p>
                      </div>
                      <Select
                        value={control.value}
                        options={control.options}
                        onChange={control.onChange}
                        disabled={disabled}
                        className="w-32 md:w-32 shrink-0"
                      />
                      {hasPlanDetails && (
                        <button
                          type="button"
                          onClick={() => toggleExpandPlan(control.key)}
                          className="p-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-all"
                          aria-label={isExpanded ? "Collapse plan" : "Expand plan"}
                        >
                          {isExpanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                        </button>
                      )}
                    </div>
                    {isExpanded && (
                      <div className="pl-4 pr-1 pb-1 animate-slide-down">
                        <PlanDetails control={control} />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* Output folder settings */}
      <div className="space-y-2 pt-2 border-t border-border/10">
        <div className="flex items-center gap-1.5">
          <FolderOpen className="w-3.5 h-3.5 text-primary" />
          <label className="text-[10px] font-bold tracking-widest text-muted-foreground/80 uppercase block mt-0.5">
            Output Folder Path (Optional)
          </label>
        </div>
        <div className="flex gap-2">
          <Input
            disabled={disabled}
            value={outputDir}
            onChange={(e) => onOutputDirChange(e.target.value)}
            placeholder="e.g. C:\path\to\output_folder"
            className="bg-background/50 text-xs flex-1 rounded-xl"
          />
          <button
            type="button"
            onClick={handleBrowseOutputDir}
            disabled={disabled}
            className="px-3 text-xs font-bold uppercase tracking-wider border border-border/80 hover:bg-muted/40 rounded-xl transition-all flex items-center gap-1.5 shrink-0 bg-background/50 shadow-sm active:scale-95"
          >
            <FolderOpen className="w-3.5 h-3.5" />
            Browse...
          </button>
        </div>
        <p className="text-[9px] text-muted-foreground/75 leading-normal">
          * Leave blank to save to the default directory (or same folder as the input file when using local paths).
        </p>
      </div>
    </div>
  )
}
