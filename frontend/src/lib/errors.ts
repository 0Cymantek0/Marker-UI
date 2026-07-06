export function messageFromUnknownError(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message
  if (typeof error === 'string' && error.trim()) return error
  if (typeof error !== 'object' || error === null || Array.isArray(error)) return fallback

  const record = error as Record<string, unknown>
  const detail = record.detail
  if (typeof detail === 'string' && detail.trim()) return detail

  const message = record.message
  if (typeof message === 'string' && message.trim()) return message

  return fallback
}
