import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import '@testing-library/jest-dom'
import { FileUpload } from '@/components/features/FileUpload'
import type { InputFormatCapability } from '@/lib/api'

const baseProps = {
  onFilesSelect: vi.fn(),
  selectedFiles: [],
  onRemoveFile: vi.fn(),
  onClearAll: vi.fn(),
  localPaths: '',
  onLocalPathsChange: vi.fn(),
  outputDir: '',
  onOutputDirChange: vi.fn(),
}

describe('FileUpload', () => {
  it('derives upload accept extensions and visible format text from backend capabilities', () => {
    const inputFormats: InputFormatCapability[] = [
      {
        extensions: ['docx', '.docm'],
        engine: 'office_docx',
        label: 'Backend Word Engine',
        category: 'office',
        needs_marker_models: false,
        needs_gpu: false,
        upload_allowed: true,
        url_allowed: true,
      },
      {
        extensions: ['.foo'],
        engine: 'custom_foo',
        label: 'Custom Foo Documents',
        category: 'custom',
        needs_marker_models: false,
        needs_gpu: false,
        upload_allowed: true,
        url_allowed: true,
      },
      {
        extensions: ['.remote'],
        engine: 'remote_only',
        label: 'Remote Only',
        category: 'custom',
        needs_marker_models: false,
        needs_gpu: false,
        upload_allowed: false,
        url_allowed: true,
      },
    ]

    const { container } = render(<FileUpload {...baseProps} inputFormats={inputFormats} />)

    expect(container.querySelector('input[type="file"]')).toHaveAttribute('accept', '.docx,.docm,.foo')
    expect(screen.getByText('Supported: Backend Word Engine and Custom Foo Documents (select multiple)')).toBeInTheDocument()
    expect(screen.queryByText(/Remote Only/)).not.toBeInTheDocument()
  })

  it('falls back to broad upload support text when capabilities are unavailable', () => {
    const { container } = render(<FileUpload {...baseProps} inputFormats={null} />)

    const input = container.querySelector('input[type="file"]')
    expect(input).toHaveAttribute('accept', expect.stringContaining('.pdf'))
    expect(input).toHaveAttribute('accept', expect.stringContaining('.wav'))
    expect(screen.getByText(/Supported: PDF, Word, spreadsheets/i)).toBeInTheDocument()
  })
})
