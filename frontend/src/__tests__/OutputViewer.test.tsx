import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import '@testing-library/jest-dom'
import { OutputViewer } from '@/components/features/OutputViewer'

describe('OutputViewer component', () => {
  it('renders the empty state when content is null', () => {
    render(<OutputViewer content={null} onDownload={vi.fn()} />)
    expect(screen.getByText('Converted output will appear here')).toBeInTheDocument()
  })

  it('renders parsed markdown for the markdown tab (heading + table + code + image)', () => {
    const md = [
      '# Report Title',
      '',
      '![chart](_page_0_Picture_1.jpeg)',
      '',
      '| Category | Revenue |',
      '|---|---|',
      '| Q1 | $1.2M |',
      '',
      '```mermaid',
      'graph TD',
      '    A-->B',
      '```',
    ].join('\n')

    render(<OutputViewer content={md} onDownload={vi.fn()} />)

    // Heading renders as an actual heading element, not raw text in a <pre>.
    expect(screen.getByRole('heading', { name: /report title/i })).toBeInTheDocument()
    // Table parsed into a real <table>.
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('Q1')).toBeInTheDocument()
    // Image token renders an <img>.
    expect(screen.getByRole('img')).toHaveAttribute('src', '_page_0_Picture_1.jpeg')
  })

  it('switches to raw text rendering on the Raw Text tab', () => {
    render(<OutputViewer content="# Hello" onDownload={vi.fn()} />)

    // Default markdown tab parses the heading.
    expect(screen.getByRole('heading', { name: /hello/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /raw text/i }))

    // Raw tab shows the literal source in a <pre>; no heading element.
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
    expect(screen.getByText('# Hello')).toBeInTheDocument()
  })

  it('invokes onDownload when the Download button is clicked', () => {
    const onDownload = vi.fn()
    render(<OutputViewer content="# Hi" onDownload={onDownload} />)

    fireEvent.click(screen.getByRole('button', { name: /download/i }))
    expect(onDownload).toHaveBeenCalledTimes(1)
  })
})
