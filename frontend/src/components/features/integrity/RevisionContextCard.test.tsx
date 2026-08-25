import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import '@testing-library/jest-dom'
import { RevisionContextCard } from './RevisionContextCard'
import type { AsOfContract, JobStatus } from '@/lib/api'

const asOf: AsOfContract = {
  schema_version: 'marker.operational.as_of.v1',
  state_token: 'tok-currentserver456',
  completeness: 'complete',
  result_digest: 'sha256:result-digest-abcdef',
  source_revision_id: 'rev-src-123456',
  config_digest: 'sha256:config-digest-abcdef',
  artifacts_purged: true,
}

const job: JobStatus = {
  id: 'job-card-1',
  job_id: 'job-card-1',
  filename: 'card.pdf',
  status: 'completed',
  progress: 100,
  output_format: 'markdown',
  converter: 'PdfConverter',
  created_at: '2026-06-11T09:00:00Z',
  completed_at: null,
  error_message: null,
  result_text: null,
}

describe('RevisionContextCard', () => {
  beforeEach(() => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
  })

  it('renders every envelope field with human labels and truncation tooltips', () => {
    render(<RevisionContextCard job={job} asOf={asOf} pinnedToken={null} />)

    expect(screen.getByText('card.pdf')).toBeInTheDocument()
    // The acting token shows truncated text but keeps the full value in title.
    const tokenSpan = screen.getByTitle('tok-currentserver456')
    expect(tokenSpan).toHaveTextContent('tok-currents')
    expect(screen.getByTitle('sha256:result-digest-abcdef')).toBeInTheDocument()
    expect(screen.getByTitle('rev-src-123456')).toBeInTheDocument()
    expect(screen.getByTitle('sha256:config-digest-abcdef')).toBeInTheDocument()
    // Purged flag is stated in text, not colour alone.
    expect(screen.getByText('Yes — artifacts were purged')).toBeInTheDocument()
    expect(screen.getByText('marker.operational.as_of.v1')).toBeInTheDocument()
    expect(screen.getByText('complete')).toBeInTheDocument()
    expect(screen.getByTitle('job-card-1')).toBeInTheDocument()
  })

  it('shows the pinned acting token instead of the server token when they differ', () => {
    render(<RevisionContextCard job={job} asOf={asOf} pinnedToken="tok-oldbookmark123" />)

    const tokenRow = screen.getByText('State token (acting)').closest('div')!.parentElement!
    expect(within(tokenRow).getByTitle('tok-oldbookmark123')).toBeInTheDocument()
    expect(within(tokenRow).queryByTitle('tok-currentserver456')).not.toBeInTheDocument()
  })

  it('copies the full token to the clipboard on demand', async () => {
    render(<RevisionContextCard job={job} asOf={asOf} pinnedToken={null} />)

    const copyToken = screen.getByRole('button', { name: /copy tok-currentserver456/i })
    fireEvent.click(copyToken)

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('tok-currentserver456')
  })

  it('marks envelope values the server omitted as not provided', () => {
    render(
      <RevisionContextCard
        job={job}
        asOf={{ schema_version: 'v1', state_token: 'tok-x', completeness: 'incomplete' }}
        pinnedToken={null}
      />
    )

    expect(screen.getAllByText('Not provided by server')).toHaveLength(3)
    expect(screen.getByText('incomplete')).toBeInTheDocument()
  })
})
