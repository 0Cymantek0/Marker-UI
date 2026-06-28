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

  it('renders an image-understanding badge when metadata matches the image filename', () => {
    const md = '![chart](_page_0_Picture_1.jpeg)'
    const meta = [
      {
        image_name: '_page_0_Picture_1.jpeg',
        image_type: 'chart_bar',
        confidence: 0.92,
        model: 'gpt-4o',
        omitted: false,
      },
    ]

    render(<OutputViewer content={md} onDownload={vi.fn()} imageUnderstanding={meta} />)

    const badges = screen.getAllByRole('button', { name: /chart_bar converted via vlm/i })
    expect(badges[0]).toBeInTheDocument()
    expect(badges[0]).toHaveAttribute('aria-label', expect.stringContaining('Confidence 92%'))
  })

  it('renders no badge for images without matching metadata', () => {
    const md = '![orphan](_page_2_Figure_0.jpeg)'

    render(<OutputViewer content={md} onDownload={vi.fn()} imageUnderstanding={[]} />)

    expect(screen.queryByRole('button', { name: /converted via vlm/i })).not.toBeInTheDocument()
    expect(screen.getByRole('img')).toHaveAttribute('src', '_page_2_Figure_0.jpeg')
  })

  it('clicking a badge opens the detail modal with type and confidence', () => {
    const md = '![photo](img.jpeg)'
    const meta = [
      {
        image_name: 'img.jpeg',
        image_type: 'photo',
        confidence: 0.8,
        model: 'gpt-4o',
        omitted: false,
      },
    ]

    render(<OutputViewer content={md} onDownload={vi.fn()} imageUnderstanding={meta} />)

    const badges = screen.getAllByRole('button', { name: /photo converted via vlm/i })
    fireEvent.click(badges[0]!)

    expect(screen.getByRole('heading', { name: /image understanding/i })).toBeInTheDocument()
    expect(screen.getAllByText('img.jpeg').length).toBeGreaterThanOrEqual(1)
    // Confidence shows in the modal (the tooltip also renders it, so use getAll).
    expect(screen.getAllByText('80%').length).toBeGreaterThanOrEqual(1)
  })

  it('hides html and json tabs when filename is not multi-format supported', () => {
    render(
      <OutputViewer
        content="# Hello"
        onDownload={vi.fn()}
        filename="document.docx"
      />
    )

    // Markdown and Raw Text should be visible
    expect(screen.getByRole('button', { name: /markdown/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /raw text/i })).toBeInTheDocument()

    // HTML and JSON tabs should NOT be in the document
    expect(screen.queryByRole('button', { name: /html/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /json/i })).not.toBeInTheDocument()
  })

  it('calls onRegenerate when a non-cached format tab is clicked', () => {
    const onRegenerate = vi.fn()
    render(
      <OutputViewer
        content="# Hello"
        onDownload={vi.fn()}
        onRegenerate={onRegenerate}
        filename="document.pdf"
        formats={{ markdown: '# Hello' }}
      />
    )

    // Click HTML tab
    fireEvent.click(screen.getByRole('button', { name: /html/i }))
    expect(onRegenerate).toHaveBeenCalledWith('html')
  })
})
