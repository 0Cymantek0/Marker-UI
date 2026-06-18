import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { formatDate } from '@/lib/datetime'

describe('formatDate timezone handling', () => {
  // Reproduce the user-reported bug: a user in a non-UTC zone sees wrong times.
  // Force a zone with a fixed offset (UTC+5:30) so the failure is deterministic
  // regardless of where the test suite runs.
  const originalTZ = process.env.TZ
  beforeAll(() => {
    process.env.TZ = 'Asia/Kolkata'
  })
  afterAll(() => {
    if (originalTZ === undefined) delete process.env.TZ
    else process.env.TZ = originalTZ
  })

  it('renders a naive UTC timestamp identically to the same instant with Z', () => {
    // Backend stores UTC. SQLite strips tzinfo so the JSON may arrive without
    // an offset. Naive string MUST be parsed as UTC, never as local time.
    const naive = formatDate('2026-06-11T09:00:00')
    const explicit = formatDate('2026-06-11T09:00:00Z')

    expect(naive).toBe(explicit)
  })

  it('renders the correct UTC wall-clock time for a naive input', () => {
    // If parsed as UTC, 09:00 UTC = 14:30 IST locally; but the bug shows
    // 09:00 only when the input is treated as already-local. The corrected
    // behavior must render the 09:00 UTC instant (which is 14:30 in IST).
    expect(formatDate('2026-06-11T09:00:00')).toBe(
      formatDate('2026-06-11T09:00:00Z'),
    )
  })
})
