import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Outlet } from 'react-router-dom'
import '@testing-library/jest-dom'
import App from '@/App'

vi.mock('@/components/layout/AppLayout', () => ({
  AppLayout: () => <Outlet />,
}))

vi.mock('@/pages/ConvertPage', () => ({
  ConvertPage: () => <div>Convert page mock</div>,
}))

vi.mock('@/pages/SettingsPage', () => ({
  SettingsPage: () => <div>Settings page mock</div>,
}))

vi.mock('@/pages/HistoryPage', () => ({
  HistoryPage: () => <div>History page mock</div>,
}))

vi.mock('@/pages/OnboardingPage', () => ({
  OnboardingPage: ({ onComplete }: { onComplete: () => void }) => (
    <button type="button" onClick={onComplete}>
      Complete onboarding
    </button>
  ),
}))

describe('App routing', () => {
  it('returns to convert page when onboarding completes', () => {
    render(
      <MemoryRouter initialEntries={["/onboarding"]}>
        <App />
      </MemoryRouter>
    )

    fireEvent.click(screen.getByRole('button', { name: /complete onboarding/i }))

    expect(screen.getByText('Convert page mock')).toBeInTheDocument()
  })
})
