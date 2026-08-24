import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import '@testing-library/jest-dom'
import { AsOfStatus } from '@/components/features/as-of/AsOfStatus'
import type { AsOfContract } from '@/lib/api'

const complete: AsOfContract = {
  schema_version: 'marker.operational.as_of.v1',
  state_token: 'sha256:abc',
  completeness: 'complete',
}

describe('AsOfStatus component', () => {
  it('renders nothing when asOf is null', () => {
    const { container } = render(<AsOfStatus asOf={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a Current badge and announces state for a fresh complete envelope', () => {
    render(<AsOfStatus asOf={complete} />)

    expect(screen.getByText('Current')).toBeInTheDocument()
    const status = screen.getByRole('status')
    expect(status).toBeInTheDocument()
    expect(status).toHaveTextContent('Output state: current, complete.')
  })

  it('renders a Stale badge, announces the server change, and fires onRefresh', () => {
    const onRefresh = vi.fn()
    render(<AsOfStatus asOf={complete} stale onRefresh={onRefresh} />)

    expect(screen.getByText('Stale')).toBeInTheDocument()
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('result changed on the server')
    expect(screen.getByRole('button', { name: /refresh/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /refresh/i }))
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['incomplete', 'Incomplete', 'incomplete'],
    ['failed', 'Failed', 'failed'],
    ['cancelled', 'Cancelled', 'cancelled'],
  ])('renders the %s label for completeness=%s', (completeness, label) => {
    render(
      <AsOfStatus
        asOf={{ ...complete, completeness: completeness as AsOfContract['completeness'] }}
      />
    )
    expect(screen.getByText(label)).toBeInTheDocument()
  })
})
