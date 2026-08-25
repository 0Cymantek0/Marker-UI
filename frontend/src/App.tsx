import { Routes, Route, useNavigate } from 'react-router-dom'
import { AppLayout } from '@/components/layout/AppLayout'
import { ConvertPage } from '@/pages/ConvertPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { HistoryPage } from '@/pages/HistoryPage'
import { IntegrityPage } from '@/pages/IntegrityPage'
import { OnboardingPage } from '@/pages/OnboardingPage'

function OnboardingRoute() {
  const navigate = useNavigate()
  return <OnboardingPage onComplete={() => navigate('/')} />
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<ConvertPage />} />
        <Route path="/onboarding" element={<OnboardingRoute />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/history" element={<HistoryPage />} />
        <Route path="/integrity" element={<IntegrityPage />} />
      </Route>
    </Routes>
  )
}
