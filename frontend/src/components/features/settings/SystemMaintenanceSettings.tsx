import type { MouseEvent, RefObject } from 'react'
import { AlertTriangle, Loader2, RotateCcw, Wrench } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface SystemMaintenanceSettingsProps {
  isSelfHealing: boolean
  isResetting: boolean
  deleteUserDataCheck: boolean
  isConfirmingReset: boolean
  clickCoords: { x: number; y: number }
  transitionEnabled: boolean
  resetCardRef: RefObject<HTMLDivElement | null>
  onSelfHeal: () => void
  onDeleteUserDataChange: (checked: boolean) => void
  onConfirmResetStart: (event: MouseEvent<HTMLButtonElement>) => void
  onResetBack: (event: MouseEvent<HTMLButtonElement>) => void
  onReset: () => void
}

export function SystemMaintenanceSettings({
  isSelfHealing,
  isResetting,
  deleteUserDataCheck,
  isConfirmingReset,
  clickCoords,
  transitionEnabled,
  resetCardRef,
  onSelfHeal,
  onDeleteUserDataChange,
  onConfirmResetStart,
  onResetBack,
  onReset,
}: SystemMaintenanceSettingsProps) {
  return (
    <div className="space-y-4 pt-6 border-t border-border/20">
      <div className="space-y-1 text-left">
        <h3 className="text-xs font-bold tracking-widest text-muted-foreground/80 uppercase flex items-center gap-2">
          <Wrench className="w-4 h-4 text-primary" />
          System Maintenance
        </h3>
        <p className="text-xs text-muted-foreground leading-relaxed max-w-3xl">
          Verify engine files, self-heal missing model components, or reset the local environment to a clean state.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-left">
        <div className="border border-border/60 rounded-xl p-5 flex flex-col justify-between transition-all bg-card/25 shadow-sm">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <div className="p-2 rounded-lg bg-muted border border-border/40 text-muted-foreground">
                <Wrench className="w-4 h-4" />
              </div>
              <h4 className="font-extrabold text-sm text-foreground">Self-Healing & Verification</h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Thoroughly inspect all downloaded model files and components. If any parts are corrupted or missing, the system will automatically download them to restore functionality.
            </p>
          </div>
          <div className="mt-5 pt-3 border-t border-border/10">
            <Button
              variant="outline"
              disabled={isSelfHealing || isResetting}
              onClick={onSelfHeal}
              className="w-full text-xs font-bold uppercase tracking-wider h-8 rounded-lg border-border/50 hover:bg-muted/40 gap-1.5"
            >
              {isSelfHealing ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin mr-1 text-primary" />
                  Healing...
                </>
              ) : (
                <>
                  <Wrench className="w-3.5 h-3.5 mr-1 text-muted-foreground" />
                  Verify & Self-Heal
                </>
              )}
            </Button>
          </div>
        </div>

        <div
          ref={resetCardRef}
          className="relative overflow-hidden border border-border/60 rounded-xl p-5 flex flex-col justify-between transition-all bg-card/25 shadow-sm min-h-[190px]"
        >
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <div className="p-2 rounded-lg bg-muted border border-border/40 text-muted-foreground">
                <RotateCcw className="w-4 h-4" />
              </div>
              <h4 className="font-extrabold text-sm text-foreground">Reset Local Environment</h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Delete downloaded model weights to clean up storage and restart the engine onboarding. Your API keys, LLM providers, and job history are preserved by default.
            </p>

            <div className="flex items-center gap-3 pt-2 select-none">
              <input
                type="checkbox"
                id="delete-user-data-checkbox"
                checked={deleteUserDataCheck}
                onChange={(e) => onDeleteUserDataChange(e.target.checked)}
                className="rounded border-2 border-border/80 bg-secondary/80 text-primary focus:ring-primary focus:ring-offset-background h-4.5 w-4.5 cursor-pointer transition-all hover:border-primary"
              />
              <label
                htmlFor="delete-user-data-checkbox"
                className="text-xs font-bold text-foreground/95 uppercase tracking-wider cursor-pointer select-none"
              >
                Also delete user data (history, settings, credentials)
              </label>
            </div>
          </div>

          <div className="mt-4 pt-3 border-t border-border/10">
            <Button
              variant="outline"
              disabled={isSelfHealing || isResetting}
              onClick={onConfirmResetStart}
              className="w-full text-xs font-bold uppercase tracking-wider h-8 rounded-lg border-rose-500/30 hover:border-rose-500 hover:bg-rose-500/10 text-muted-foreground hover:text-rose-500 transition-colors gap-1.5"
            >
              <RotateCcw className="w-3.5 h-3.5 mr-1 text-muted-foreground hover:text-rose-500" />
              Reset Environment
            </Button>
          </div>

          <div
            className={cn(
              'absolute inset-0 bg-gradient-to-br from-red-950 to-rose-900 border border-red-500/40 rounded-xl z-10 flex flex-col justify-between p-5',
              transitionEnabled ? 'transition-all duration-400 ease-out' : 'transition-none'
            )}
            style={{
              clipPath: isConfirmingReset
                ? `circle(150% at ${clickCoords.x}px ${clickCoords.y}px)`
                : `circle(0% at ${clickCoords.x}px ${clickCoords.y}px)`,
              pointerEvents: isConfirmingReset ? 'auto' : 'none',
            }}
          >
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <div className="p-2 rounded-lg bg-red-900/60 border border-red-500/30 text-white">
                  <AlertTriangle className="w-4 h-4 shrink-0" />
                </div>
                <h4 className="font-extrabold text-sm text-white uppercase tracking-wider">Confirm System Reset</h4>
              </div>
              <p className="text-xs text-red-200/95 leading-relaxed font-semibold">
                {deleteUserDataCheck
                  ? 'DANGER: You are about to permanently delete all downloaded models AND ALL user database tables, LLM settings, API keys, and job history. This cannot be undone!'
                  : 'You are about to delete all downloaded model weights from local storage. Your settings, API keys, and history will be preserved.'}
              </p>
            </div>

            <div className="flex items-center gap-3 mt-4 pt-3 border-t border-red-500/20">
              <Button
                variant="ghost"
                onClick={onResetBack}
                className="flex-1 text-xs font-bold uppercase tracking-wider h-8 rounded-lg text-red-200 hover:text-white hover:bg-white/10"
              >
                Go Back
              </Button>
              <Button
                disabled={isResetting}
                onClick={onReset}
                className="flex-1 text-xs font-bold uppercase tracking-wider h-8 rounded-lg bg-white text-red-700 hover:bg-red-50 transition-colors shadow-md border-0"
              >
                {isResetting ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin mr-1 text-red-700" />
                    Resetting...
                  </>
                ) : (
                  'Confirm Reset'
                )}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
