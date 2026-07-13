import { useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { Toaster } from 'sonner'
import { Sidebar } from './Sidebar'
import { ThemeProvider, useTheme } from './ThemeContext'
import { ConversionProvider } from '@/hooks/useConversionQueue'

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <ThemeProvider>
      <ConversionProvider>
        <AppContent collapsed={collapsed} setCollapsed={setCollapsed} />
      </ConversionProvider>
    </ThemeProvider>
  )
}

function AppContent({
  collapsed,
  setCollapsed,
}: {
  collapsed: boolean
  setCollapsed: React.Dispatch<React.SetStateAction<boolean>>
}) {
  const { theme } = useTheme()
  const [isNarrowViewport, setIsNarrowViewport] = useState(false)

  useEffect(() => {
    const mediaQuery = window.matchMedia('(max-width: 767px)')
    const syncViewport = () => setIsNarrowViewport(mediaQuery.matches)

    syncViewport()
    mediaQuery.addEventListener('change', syncViewport)
    return () => mediaQuery.removeEventListener('change', syncViewport)
  }, [])

  const sidebarCollapsed = collapsed || isNarrowViewport

  return (
    <div className="flex h-screen overflow-hidden bg-background text-foreground transition-colors duration-300">
      {/* Sidebar */}
      <Sidebar
        collapsed={sidebarCollapsed}
        onToggle={() => setCollapsed((c) => !c)}
      />

      {/* Main area */}
      <div className="flex flex-col flex-1 min-w-0">
        <main className="flex-1 overflow-auto bg-background/50 dark:bg-background/20">
          <Outlet />
        </main>
      </div>

      <Toaster theme={theme} position="bottom-right" />
    </div>
  )
}
