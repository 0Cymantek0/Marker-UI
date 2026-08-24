import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { HistoryPage } from '@/pages/HistoryPage'
import { ApiError } from '@/lib/api'
import * as api from '@/lib/api'
import '@testing-library/jest-dom'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    getHistory: vi.fn(),
    deleteJob: vi.fn(),
    downloadResult: vi.fn(),
    getJobStatus: vi.fn(),
  }
})

describe('HistoryPage component delete confirmation flow', () => {
  const mockJob = {
    id: 'job-123',
    job_id: 'job-123',
    filename: 'test_file.pdf',
    status: 'completed' as const,
    progress: 100,
    output_format: 'markdown',
    converter: 'PdfConverter',
    created_at: '2026-06-11T09:00:00Z',
    completed_at: '2026-06-11T09:01:00Z',
    error_message: null,
    result_text: 'sample output',
    as_of: {
      schema_version: 'marker.operational.as_of.v1',
      state_token: 'sha256:old',
      completeness: 'complete' as const,
    },
  }

  beforeEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.clearAllMocks()
    vi.mocked(api.getHistory).mockResolvedValue({
      jobs: [mockJob],
      total: 1,
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('requires clicking the delete button twice to perform a deletion', async () => {
    render(<HistoryPage />)

    // Wait for the history items to load and render
    const deleteBtn = await screen.findByTitle('Delete entry')
    expect(deleteBtn).toBeInTheDocument()

    // First click: should trigger confirm state (title changes to Confirm delete)
    await act(async () => {
      fireEvent.click(deleteBtn)
    })

    expect(api.deleteJob).not.toHaveBeenCalled()
    expect(deleteBtn).toHaveAttribute('title', 'Confirm delete')

    // Second click: should perform the actual deletion
    vi.mocked(api.deleteJob).mockResolvedValue()

    await act(async () => {
      fireEvent.click(deleteBtn)
    })

    expect(api.deleteJob).toHaveBeenCalledWith('job-123')
    expect(api.getHistory).toHaveBeenCalledTimes(2) // Initial load + refresh after delete
  })

  it('resets delete confirmation state after 3 seconds of inactivity', async () => {
    render(<HistoryPage />)

    const deleteBtn = await screen.findByTitle('Delete entry')
    expect(deleteBtn).toBeInTheDocument()

    // Enable fake timers after findByTitle resolves
    vi.useFakeTimers()

    // Trigger confirmation state
    await act(async () => {
      fireEvent.click(deleteBtn)
    })
    expect(deleteBtn).toHaveAttribute('title', 'Confirm delete')

    // Fast-forward 3 seconds
    await act(async () => {
      vi.advanceTimersByTime(3000)
    })

    expect(deleteBtn).toHaveAttribute('title', 'Delete entry')
  })

  it('resets confirmation when toggle expand is clicked', async () => {
    render(<HistoryPage />)

    const deleteBtn = await screen.findByTitle('Delete entry')
    
    // Trigger confirmation
    await act(async () => {
      fireEvent.click(deleteBtn)
    })
    expect(deleteBtn).toHaveAttribute('title', 'Confirm delete')

    // Click on the row header to expand/collapse (using the file name element)
    const rowHeader = screen.getByText('test_file.pdf')
    await act(async () => {
      fireEvent.click(rowHeader)
    })

    // Delete button should reset back to Delete entry
    expect(deleteBtn).toHaveAttribute('title', 'Delete entry')
  })

  it('uses blob content type to correct stale download filename extensions', async () => {
    vi.mocked(api.downloadResult).mockResolvedValue({
      blob: new Blob(['zip'], { type: 'application/zip' }),
      filename: 'test_file.md',
    })
    const anchor = document.createElement('a')
    const click = vi.fn()
    anchor.click = click
    const createElement = vi.spyOn(document, 'createElement').mockImplementation((tagName, options) => {
      if (tagName.toLowerCase() === 'a') return anchor
      return Document.prototype.createElement.call(document, tagName, options)
    })
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:history-download')
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})

    render(<HistoryPage />)

    const downloadBtn = await screen.findByTitle(/Download Result/i)
    await act(async () => {
      fireEvent.click(downloadBtn)
    })

    expect(api.downloadResult).toHaveBeenCalledWith('job-123', undefined, 'sha256:old')
    expect(anchor.download).toBe('test_file.zip')
    expect(click).toHaveBeenCalled()

    createElement.mockRestore()
    createObjectURL.mockRestore()
    revokeObjectURL.mockRestore()
  })

  it('sends pending status when queued filter is selected', async () => {
    render(<HistoryPage />)

    await screen.findByText('test_file.pdf')

    fireEvent.click(screen.getByRole('button', { name: /All Statuses/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Queued' }))

    await waitFor(() => {
      expect(api.getHistory).toHaveBeenLastCalledWith(1, 10, undefined, 'pending', 'all')
    })
  })

  it('shows the output state in the details panel and downloads with the as_of token', async () => {
    render(<HistoryPage />)

    await screen.findByText('test_file.pdf')
    // Expand the row to reveal the details panel.
    fireEvent.click(screen.getByText('test_file.pdf'))

    expect(screen.getByRole('status')).toHaveTextContent('Output state: current, complete.')
    expect(api.downloadResult).not.toHaveBeenCalled()

    const downloadBtn = screen.getByTitle(/Download Result/i)
    await act(async () => {
      fireEvent.click(downloadBtn)
    })

    expect(api.downloadResult).toHaveBeenCalledWith('job-123', undefined, 'sha256:old')
  })

  it('flags stale, patches as_of from the 409 payload, and retries with the new token', async () => {
    vi.mocked(api.downloadResult)
      .mockRejectedValueOnce(
        new ApiError('Download failed (409): stale_state', {
          status: 409,
          code: 'stale_state',
          currentAsOf: {
            schema_version: 'marker.operational.as_of.v1',
            state_token: 'sha256:new',
            completeness: 'complete',
          },
        })
      )
      .mockResolvedValueOnce({
        blob: new Blob(['zip'], { type: 'application/zip' }),
        filename: 'test_file.md',
      })

    render(<HistoryPage />)

    await screen.findByText('test_file.pdf')
    fireEvent.click(screen.getByText('test_file.pdf'))

    const downloadBtn = screen.getByTitle(/Download Result/i)
    await act(async () => {
      fireEvent.click(downloadBtn)
    })

    // First attempt rejected as stale -> stale notice + retry button appear.
    expect(screen.getByRole('status')).toHaveTextContent('result changed on the server')
    expect(screen.getByRole('button', { name: /retry download/i })).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /retry download/i }))
    })

    // Second attempt uses the refreshed token from the 409 payload.
    expect(api.downloadResult).toHaveBeenLastCalledWith('job-123', undefined, 'sha256:new')
  })
})
