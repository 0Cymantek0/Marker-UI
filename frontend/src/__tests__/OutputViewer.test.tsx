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

  it.each([
    ['http image', 'http://tracker.example/pixel.png', true],
    ['https image', 'https://tracker.example/pixel.png', true],
    ['protocol-relative image', '//tracker.example/pixel.png', true],
    ['data image', 'data:image/png;base64,AAAA', false],
    ['file image', 'file:///C:/secret.png', false],
    ['absolute path image', '/private/secret.png', true],
    ['path traversal image', '../secret.png', true],
    ['encoded path traversal image', 'images/%2e%2e/secret.png', true],
    ['javascript image', 'javascript:alert(1)', false],
  ])('blocks unsafe markdown image src: %s', (_label, src, rendererKeepsSrc) => {
    const md = `![tracker](${src})`

    render(<OutputViewer content={md} onDownload={vi.fn()} />)

    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByRole('note', { name: /external image blocked for privacy/i })).toBeInTheDocument()
    expect(screen.getByText('External image blocked')).toBeInTheDocument()
    if (rendererKeepsSrc) {
      expect(screen.getByText(src)).toBeInTheDocument()
    }
  })

  it('allows safe nested relative markdown images', () => {
    const md = '![safe](assets/page_1_Picture_0.webp?cache=1#figure)'

    render(<OutputViewer content={md} onDownload={vi.fn()} />)

    expect(screen.getByRole('img', { name: /safe/i })).toHaveAttribute(
      'src',
      'assets/page_1_Picture_0.webp?cache=1#figure',
    )
    expect(screen.queryByRole('note', { name: /external image blocked for privacy/i })).not.toBeInTheDocument()
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

  it('hides html json and chunks tabs for native files without cached output or regenerate handler', () => {
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

    // HTML/JSON/Chunks tabs need cached output or regeneration.
    expect(screen.queryByRole('button', { name: /html/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /json/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /chunks/i })).not.toBeInTheDocument()
  })

  it('allows native files to regenerate derived chunks but not marker-only html/json', () => {
    const onRegenerate = vi.fn()
    render(
      <OutputViewer
        content="# Hello"
        onDownload={vi.fn()}
        onRegenerate={onRegenerate}
        filename="document.docx"
        formats={{ markdown: '# Hello' }}
      />
    )

    expect(screen.getByRole('button', { name: /chunks/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /html/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /json/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /chunks/i }))
    expect(onRegenerate).toHaveBeenCalledWith('chunks')
  })

  it('shows backend-provided formats even when filename is not marker multi-format supported', () => {
    render(
      <OutputViewer
        content="# Hello"
        formats={{ markdown: '# Hello', html: '<h1>Hello</h1>', chunks: '{"chunks":[]}' }}
        availableFormats={['markdown', 'html', 'chunks']}
        onDownload={vi.fn()}
        filename="document.docx"
      />
    )

    expect(screen.getByRole('button', { name: /html/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /chunks/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /json/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /chunks/i }))
    expect(screen.getByText('{"chunks":[]}')).toBeInTheDocument()
  })

  it('does not show regeneratable format tabs without a regenerate handler', () => {
    render(
      <OutputViewer
        content="# Hello"
        formats={{ markdown: '# Hello' }}
        onDownload={vi.fn()}
        filename="document.pdf"
      />
    )

    expect(screen.queryByRole('button', { name: /html/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /json/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /chunks/i })).not.toBeInTheDocument()
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

  it('calls onRegenerate when the Chunks tab is clicked', () => {
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

    fireEvent.click(screen.getByRole('button', { name: /chunks/i }))
    expect(onRegenerate).toHaveBeenCalledWith('chunks')
  })

  it('renders an audio inspection tab when audio metadata is available', () => {
    render(
      <OutputViewer
        content="# Audio"
        onDownload={vi.fn()}
        filename="voice.wav"
        audioMetadata={{
          transcript: {
            provider: 'local_faster_whisper',
            model: 'tiny.en',
            segments: [
              {
                segment_id: 'voice_seg_0001',
                start_ms: 0,
                end_ms: 1200,
                speaker: 'speaker_0',
                text: 'hello audio world',
                confidence: 0.92,
                warnings: [],
              },
            ],
          },
          quality: { review_required: false },
          speakers: {
            timeline: [{ speaker: 'speaker_0', display_label: 'Speaker 0', segment_count: 1 }],
          },
          vocabulary: { requested_count: 1, detected_count: 1, detected: ['Marker'] },
        }}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /audio/i }))

    expect(screen.getByText('Confidence Timeline')).toBeInTheDocument()
    expect(screen.getByText('hello audio world')).toBeInTheDocument()
    expect(screen.getByText(/local_faster_whisper/i)).toBeInTheDocument()
    expect(screen.getByText(/Hits: Marker/i)).toBeInTheDocument()
  })

  it('keeps the audio inspection tab usable when backend metadata is partial or malformed', () => {
    render(
      <OutputViewer
        content="# Audio"
        onDownload={vi.fn()}
        filename="voice.wav"
        audioMetadata={{
          transcript: {
            provider: 'local_faster_whisper',
            segments: 'not-an-array',
            warnings: ['low signal'],
          },
          quality: { review_required: true, low_confidence_count: '2' },
          speakers: { timeline: 'not-an-array' },
          vocabulary: { detected: ['Marker'], requested_count: '0' },
        }}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /audio/i }))

    expect(screen.getByText('Audio review suggested')).toBeInTheDocument()
    expect(screen.getByText('No audio segments in metadata.')).toBeInTheDocument()
    expect(screen.getByText('required')).toBeInTheDocument()
  })
})
