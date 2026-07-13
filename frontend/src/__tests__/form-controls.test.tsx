import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'

describe('shared form controls', () => {
  it('lets full-width selects fill their field instead of keeping a fixed desktop width', () => {
    const { container } = render(
      <Select
        value="local"
        onChange={vi.fn()}
        options={[{ value: 'local', label: 'Local faster-whisper' }]}
        className="w-full"
      />,
    )

    expect(container.firstElementChild).toHaveClass('w-full')
    expect(container.firstElementChild?.className).not.toContain('md:w-44')
  })

  it('keeps inputs at the shared readable size even when callers pass old compact classes', () => {
    render(<Input aria-label="Model" className="h-8 text-xs bg-background/50" />)

    const input = screen.getByLabelText('Model')
    expect(input).toHaveClass('min-h-10')
    expect(input).toHaveClass('text-sm')
    expect(input).not.toHaveClass('text-xs')
  })
})
