import { useState, useEffect, useCallback } from 'react'
import { Plus, Trash2, Package, AlertTriangle } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { messageFromUnknownError } from '@/lib/errors'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import {
  getVocabularyPacks,
  saveVocabularyPack,
  deleteVocabularyPack,
  type VocabularyPack,
} from '@/lib/api'

interface VocabularyPackEditorProps {
  selectedPackIds: string[]
  onChangePackIds: (ids: string[]) => void
  disabled?: boolean
  providerSupportsVocab?: boolean
}

export function VocabularyPackEditor({
  selectedPackIds,
  onChangePackIds,
  disabled,
  providerSupportsVocab = true,
}: VocabularyPackEditorProps) {
  const [packs, setPacks] = useState<VocabularyPack[]>([])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [terms, setTerms] = useState('')

  const loadPacks = useCallback(() => {
    getVocabularyPacks()
      .then(setPacks)
      .catch(() => {})
  }, [])

  useEffect(() => { loadPacks() }, [loadPacks])

  const togglePack = (packId: string) => {
    if (selectedPackIds.includes(packId)) {
      onChangePackIds(selectedPackIds.filter((id) => id !== packId))
    } else {
      onChangePackIds([...selectedPackIds, packId])
    }
  }

  const handleCreate = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const trimmed = name.trim()
    if (!trimmed) return

    const parsed = terms
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)

    if (parsed.length === 0) {
      toast.error('Add at least one term.')
      return
    }

    try {
      const created = await saveVocabularyPack({ name: trimmed, terms: parsed })
      toast.success(`Pack "${created.name}" saved.`)
      setName('')
      setTerms('')
      setCreating(false)
      loadPacks()
      onChangePackIds([...selectedPackIds, created.id])
    } catch (err: unknown) {
      toast.error(messageFromUnknownError(err, 'Failed to save vocabulary pack.'))
    }
  }

  const handleDelete = async (packId: string, packName: string) => {
    if (!window.confirm(`Delete vocabulary pack "${packName}"?`)) return
    try {
      await deleteVocabularyPack(packId)
      toast.success(`Pack "${packName}" deleted.`)
      onChangePackIds(selectedPackIds.filter((id) => id !== packId))
      loadPacks()
    } catch (err: unknown) {
      toast.error(messageFromUnknownError(err, 'Failed to delete pack.'))
    }
  }

  return (
    <div className="space-y-2" data-testid="vocabulary-pack-editor">
      <div className="flex items-center justify-between">
        <label className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase">
          Vocabulary Packs
        </label>
        <button
          type="button"
          onClick={() => setCreating(!creating)}
          disabled={disabled}
          className="inline-flex items-center gap-1 text-xs font-bold uppercase tracking-wider text-primary hover:text-foreground transition-colors disabled:opacity-50"
        >
          {creating ? 'Cancel' : <><Plus className="w-3 h-3" /> New Pack</>}
        </button>
      </div>

      {packs.length > 0 && (
        <div className="max-h-32 overflow-y-auto space-y-1 pr-1 scrollbar-thin">
          {packs.map((pack) => {
            const isSelected = selectedPackIds.includes(pack.id)
            return (
              <div
                key={pack.id}
                className={cn(
                  'flex items-center justify-between gap-2 p-2 rounded-lg border transition-colors cursor-pointer',
                  isSelected
                    ? 'border-primary/40 bg-primary/5'
                    : 'border-border/20 bg-card/20 hover:bg-muted/20',
                  disabled && 'opacity-50 pointer-events-none'
                )}
                onClick={() => !disabled && togglePack(pack.id)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <Package className={cn('w-3.5 h-3.5 shrink-0', isSelected ? 'text-primary' : 'text-muted-foreground/60')} />
                  <div className="min-w-0">
                    <span className="text-xs font-semibold text-foreground block truncate">{pack.name}</span>
                    <span className="text-xs text-muted-foreground block truncate">
                      {pack.terms.length} term{pack.terms.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); handleDelete(pack.id, pack.name) }}
                  disabled={disabled}
                  className="p-1 rounded text-muted-foreground hover:text-red-500 hover:bg-red-500/10 transition-colors shrink-0"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            )
          })}
        </div>
      )}

      {packs.length === 0 && !creating && (
        <p className="text-xs text-muted-foreground/60 py-1">
          No saved packs. Create one to reuse domain terms across recordings.
        </p>
      )}

      {creating && (
        <form onSubmit={handleCreate} className="p-2.5 rounded-lg border border-primary/20 bg-primary/5 space-y-2 animate-fade-in">
          <Input
            placeholder="Pack name (e.g. Medical Terms)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={disabled}
            className="h-8 text-xs bg-background/50"
            required
            autoFocus
          />
          <Input
            placeholder="term1, term2, term3, ..."
            value={terms}
            onChange={(e) => setTerms(e.target.value)}
            disabled={disabled}
            className="h-8 text-xs bg-background/50"
          />
          <div className="flex justify-end">
            <Button type="submit" size="sm" className="h-7 text-xs uppercase tracking-wider font-bold">
              Save Pack
            </Button>
          </div>
        </form>
      )}

      {!providerSupportsVocab && selectedPackIds.length > 0 && (
        <div className="text-xs text-amber-600 dark:text-amber-400 p-1.5 rounded border border-amber-500/20 bg-amber-500/5 leading-snug flex items-start gap-1.5">
          <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>Provider can't accept prompts. Selected packs will only be used in correction diagnostics.</span>
        </div>
      )}
    </div>
  )
}
