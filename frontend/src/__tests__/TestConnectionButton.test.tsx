import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { TestConnectionButton } from '@/components/features/settings/TestConnectionButton'
import '@testing-library/jest-dom'

describe('TestConnectionButton component', () => {
  it('renders in idle state by default', () => {
    const onTest = vi.fn()
    render(<TestConnectionButton apiKey="test-key" onTest={onTest} />)
    
    expect(screen.getByText('Test')).toBeInTheDocument()
    expect(screen.queryByText('Testing')).not.toBeInTheDocument()
    expect(screen.queryByText('Passed')).not.toBeInTheDocument()
    expect(screen.queryByText('Failed')).not.toBeInTheDocument()
  })

  it('transitions to loading state on click and calls onTest', async () => {
    const onTest = vi.fn().mockReturnValue(new Promise(() => {})) // never resolves
    render(<TestConnectionButton apiKey="test-key" onTest={onTest} />)
    
    const button = screen.getByRole('button')
    await act(async () => {
      fireEvent.click(button)
    })
    
    expect(onTest).toHaveBeenCalledWith('test-key')
    expect(screen.getByText('Testing')).toBeInTheDocument()
    expect(button).toBeDisabled()
  })

  it('transitions to success state when onTest succeeds', async () => {
    const onTest = vi.fn().mockResolvedValue({ success: true, message: 'Connected!' })
    render(<TestConnectionButton apiKey="test-key" onTest={onTest} />)
    
    const button = screen.getByRole('button')
    
    await act(async () => {
      fireEvent.click(button)
    })

    // Allow promise resolution to flush
    await act(async () => {
      await Promise.resolve()
    })
    
    expect(screen.getByText('Passed')).toBeInTheDocument()
    expect(button).toHaveClass('bg-emerald-500/90')
  })

  it('transitions to error state when onTest fails', async () => {
    const onTest = vi.fn().mockResolvedValue({ success: false, message: 'Failed!' })
    render(<TestConnectionButton apiKey="test-key" onTest={onTest} />)
    
    const button = screen.getByRole('button')
    
    await act(async () => {
      fireEvent.click(button)
    })

    // Allow promise resolution to flush
    await act(async () => {
      await Promise.resolve()
    })
    
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(button).toHaveClass('bg-rose-500/90')
  })

  it('resets to idle immediately if apiKey changes during non-idle state', async () => {
    const onTest = vi.fn().mockResolvedValue({ success: true, message: 'Connected!' })
    const { rerender } = render(<TestConnectionButton apiKey="key-1" onTest={onTest} />)
    
    const button = screen.getByRole('button')
    await act(async () => {
      fireEvent.click(button)
    })

    await act(async () => {
      await Promise.resolve()
    })
    
    expect(screen.getByText('Passed')).toBeInTheDocument()

    // Rerender with a different apiKey
    await act(async () => {
      rerender(<TestConnectionButton apiKey="key-2" onTest={onTest} />)
    })

    expect(screen.getByText('Test')).toBeInTheDocument()
  })
})
