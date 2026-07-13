import { useState, useEffect } from 'react'
import { NavLink } from 'react-router-dom'
import {
  Upload,
  Settings,
  History,
  FileText,
  PanelLeftClose,
  PanelLeft,
  Sun,
  Moon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useTheme } from './ThemeContext'
import { healthCheck } from '@/lib/api'

const NAV_ITEMS = [
  { to: '/', label: 'Convert', icon: Upload },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/history', label: 'History', icon: History },
] as const

interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const { theme, toggleTheme } = useTheme()
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let mounted = true

    async function check() {
      try {
        await healthCheck()
        if (mounted) {
          setConnected(true)
        }
      } catch {
        if (mounted) {
          setConnected(false)
        }
      }
    }

    void check()
    const interval = setInterval(() => void check(), 30_000)

    return () => {
      mounted = false
      clearInterval(interval)
    }
  }, [])

  return (
    <aside
      className={cn(
        'glass-card flex flex-col h-full border-r border-border/50 transition-all duration-300 ease-out',
        collapsed ? 'w-16' : 'w-56'
      )}
    >
      {/* Brand */}
      <div className="flex items-center gap-3 px-4 h-16 border-b border-border/30 relative">
        <div className="flex items-center justify-center w-8 h-8 text-primary shrink-0 relative">
          <FileText className="w-5 h-5" />
          {collapsed && (
            <span
              className={cn(
                'absolute bottom-0 right-0 w-2 h-2 rounded-full border border-background',
                connected ? 'bg-emerald-500' : 'bg-red-500'
              )}
              title={connected ? 'Connected' : 'Offline'}
            />
          )}
        </div>
        {!collapsed && (
          <div className="flex flex-col">
            <span className="font-bold text-[15px] tracking-tight text-foreground leading-none">
              Marker UI
            </span>
            <div className="flex items-center gap-1 mt-1">
              <span
                className={cn(
                  'w-1.5 h-1.5 rounded-full',
                  connected ? 'bg-emerald-500' : 'bg-red-500'
                )}
              />
              <span className="text-xs text-muted-foreground/80 font-bold uppercase tracking-wider">
                {connected ? 'connected' : 'offline'}
              </span>
            </div>
          </div>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-4 px-2 space-y-1">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            className={({ isActive }) =>
              cn(
                'relative flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200',
                'hover:bg-muted/70 hover:text-foreground',
                isActive
                  ? 'bg-primary/10 text-primary font-semibold'
                  : 'text-muted-foreground',
                collapsed && 'justify-center px-0'
              )
            }
          >
            {({ isActive }) => (
              <>
                {isActive && (
                  <div className="absolute left-0 top-1/4 bottom-1/4 w-1 rounded-r-md bg-primary" />
                )}
                <item.icon className="w-4.5 h-4.5 shrink-0" />
                {!collapsed && <span>{item.label}</span>}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      {/* Theme Toggle */}
      <div className="px-2 pb-1 pt-2 border-t border-border/20">
        <button
          onClick={toggleTheme}
          className={cn(
            'flex items-center gap-3 w-full px-3 py-2 rounded-lg text-sm text-muted-foreground',
            'hover:bg-muted hover:text-foreground transition-all duration-200',
            collapsed && 'justify-center px-0'
          )}
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {theme === 'dark' ? (
            <Sun className="w-4.5 h-4.5 text-amber-500" />
          ) : (
            <Moon className="w-4.5 h-4.5 text-slate-500" />
          )}
          {!collapsed && (
            <span>{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</span>
          )}
        </button>
      </div>

      {/* Collapse toggle */}
      <div className="p-2 border-t border-border/30">
        <button
          onClick={onToggle}
          className={cn(
            'flex items-center gap-3 w-full px-3 py-2 rounded-lg text-sm text-muted-foreground',
            'hover:bg-muted hover:text-foreground transition-colors duration-200',
            collapsed && 'justify-center px-0'
          )}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? (
            <PanelLeft className="w-4.5 h-4.5" />
          ) : (
            <PanelLeftClose className="w-4.5 h-4.5" />
          )}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  )
}
