import { describe, expect, it, vi } from 'vitest'
import { extensionFromBlob, filenameForDownload, saveBlob } from '@/lib/download'

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

describe('saveBlob', () => {
  it('triggers an anchor save with the given filename and revokes the object URL', () => {
    const anchor = document.createElement('a')
    const click = vi.fn()
    anchor.click = click
    const createElement = vi.spyOn(document, 'createElement').mockImplementation((tagName, options) => {
      if (tagName.toLowerCase() === 'a') return anchor
      return Document.prototype.createElement.call(document, tagName, options)
    })
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:save-test')
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})

    saveBlob(new Blob(['zip'], { type: 'application/zip' }), 'report.zip')

    expect(anchor.download).toBe('report.zip')
    expect(click).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:save-test')

    createElement.mockRestore()
    createObjectURL.mockRestore()
    revokeObjectURL.mockRestore()
  })
})
