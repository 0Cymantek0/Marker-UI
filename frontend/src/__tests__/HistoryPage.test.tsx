import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { HistoryPage } from '@/pages/HistoryPage'
import * as api from '@/lib/api'
import '@testing-library/jest-dom'

vi.mock('@/lib/api', () => ({
  getHistory: vi.fn(),
  deleteJob: vi.fn(),
  downloadResult: vi.fn(),
  getJobStatus: vi.fn(),
}))

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
  }

  beforeEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
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

    expect(api.downloadResult).toHaveBeenCalledWith('job-123')
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
})
