const BLOB_EXTENSION_RULES: Array<[RegExp, string]> = [
  [/^application\/(?:x-)?zip$/i, 'zip'],
  [/^application\/json$/i, 'json'],
  [/^\w+\/[-+.\w]+?\+json$/i, 'json'],
  [/^text\/html$/i, 'html'],
  [/^text\/markdown$/i, 'md'],
  [/^text\/plain$/i, 'txt'],
]

export function extensionFromBlob(blob: Blob): string | null {
  const mediaType = (blob.type || '').split(';', 1)[0]?.trim().toLowerCase()
  if (!mediaType) return null
  for (const [pattern, extension] of BLOB_EXTENSION_RULES) {
    if (pattern.test(mediaType)) return extension
  }
  return null
}

export function filenameForDownload(
  blob: Blob,
  sourceFilename: string,
  headerFilename?: string | null,
  isBunch = false,
): string {
  const blobExtension = extensionFromBlob(blob)
  if (headerFilename) {
    return blobExtension ? replaceExtension(headerFilename, blobExtension) : headerFilename
  }

  const stem = stemFromFilename(sourceFilename)
  const extension = blobExtension ?? 'md'
  return `${isBunch ? 'marker-' : ''}${stem}.${extension}`
}

function replaceExtension(filename: string, extension: string): string {
  const clean = filename.trim() || 'download'
  const slashIndex = Math.max(clean.lastIndexOf('/'), clean.lastIndexOf('\\'))
  const dirname = slashIndex >= 0 ? clean.slice(0, slashIndex + 1) : ''
  const basename = slashIndex >= 0 ? clean.slice(slashIndex + 1) : clean
  const dotIndex = basename.lastIndexOf('.')
  const stem = dotIndex > 0 ? basename.slice(0, dotIndex) : basename
  return `${dirname}${stem}.${extension}`
}

/** Trigger a browser save of a fetched blob under the given filename. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function stemFromFilename(filename: string): string {
  const basename = filename.split(/[\\/]/).pop() || 'converted'
  const dotIndex = basename.lastIndexOf('.')
  return dotIndex > 0 ? basename.slice(0, dotIndex) : basename
}
