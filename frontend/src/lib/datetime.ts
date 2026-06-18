/**
 * Timestamp formatting helpers.
 *
 * Backend stores UTC. SQLite returns naive datetimes that — after the
 * JobStatusResponse serializer — carry an explicit `Z` / `+00:00` offset.
 * Any offset-less string that slips through (legacy rows, fixtures) is
 * treated as UTC so it never gets misparsed as local time.
 */
export function formatDate(dateStr: string): string {
  const hasOffset = /Z$|[+-]\d{2}:?\d{2}$/.test(dateStr)
  const d = new Date(hasOffset ? dateStr : `${dateStr}Z`)
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
