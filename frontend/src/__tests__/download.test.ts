import { describe, expect, it } from 'vitest'
import { extensionFromBlob, filenameForDownload } from '@/lib/download'

describe('download filename helpers', () => {
  it('uses blob type to correct a stale response filename extension', () => {
    const blob = new Blob(['zip'], { type: 'application/zip' })

    expect(filenameForDownload(blob, 'source.pdf', 'source.md')).toBe('source.zip')
  })

  it('uses blob type when no response filename exists', () => {
    const blob = new Blob(['{}'], { type: 'application/json' })

    expect(filenameForDownload(blob, 'report.docx')).toBe('report.json')
  })

  it('keeps bunch prefix while deriving extension from blob type', () => {
    const blob = new Blob(['<html></html>'], { type: 'text/html' })

    expect(filenameForDownload(blob, 'deck.pptx', null, true)).toBe('marker-deck.html')
  })

  it('recognizes structured json media types', () => {
    const blob = new Blob(['{}'], { type: 'application/manifest+json' })

    expect(extensionFromBlob(blob)).toBe('json')
  })
})
