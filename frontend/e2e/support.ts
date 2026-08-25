import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test'

/**
 * Shared support for the integrity E2E specs. Talks to the REAL backend
 * directly from Node (bypassing the vite proxy the page uses) to read the
 * authoritative state token and to rotate it via regenerate — the exact
 * server-side mutations a second actor (another tab, another user) would
 * perform between the page's load and its export attempt.
 */

export const JOB_ID = 'job-e2e-seeded'
/** Display filename the API surfaces for the seeded job (its original_name:
 *  the status/history envelopes report `doc-e2e-seeded.pdf`, not the stored
 *  upload name `job-e2e-seeded.pdf` — the UI names rows and exports off it). */
export const JOB_FILENAME = 'doc-e2e-seeded.pdf'
export const BACKEND_BASE = 'http://127.0.0.1:8917'

interface StatusBody {
  as_of?: { state_token?: string }
}

/** The job's CURRENT server state token. Always read fresh — never reuse a
 *  token captured earlier: specs run serially against shared mutable state. */
export async function fetchCurrentToken(request: APIRequestContext): Promise<string> {
  const res = await request.get(`${BACKEND_BASE}/api/convert/status/${JOB_ID}`)
  expect(res.status()).toBe(200)
  const body = (await res.json()) as StatusBody
  const token = body.as_of?.state_token
  expect(typeof token, 'status envelope must carry as_of.state_token').toBe('string')
  return token as string
}

/** Rotate the job's state (regenerate re-renders markdown → new digest → new
 *  derived token). `asOf` MUST be the current token or the server rejects. */
export async function regenerateAsOf(
  request: APIRequestContext,
  asOf: string,
): Promise<void> {
  const res = await request.post(
    `${BACKEND_BASE}/api/convert/${JOB_ID}/regenerate?format=markdown&as_of=${encodeURIComponent(asOf)}`,
  )
  expect(res.status(), `regenerate must succeed against current token (${res.status()}: ${await res.text()})`).toBe(200)
}

// ─── Page-object locators (role/text per the committed UI surface) ──────────

export const contextHeading = (page: Page): Locator =>
  page.getByRole('heading', { name: JOB_FILENAME })

export const downloadButton = (page: Page): Locator =>
  page.getByRole('button', { name: 'Download (verified)' })

export const staleBanner = (page: Page): Locator =>
  page.getByRole('alert', { name: 'Stale state' })

export const refreshButton = (page: Page): Locator =>
  page.getByRole('button', { name: 'Refresh current state' })

export const verifiedExportResult = (page: Page): Locator =>
  page.getByTestId('verified-export-result')

/** Wait until the page shows the revision-context card for the seeded job and
 *  an actionable (enabled) verified-export button. */
export async function expectReadyToExport(page: Page): Promise<void> {
  await expect(contextHeading(page)).toBeVisible()
  await expect(downloadButton(page)).toBeEnabled()
}

/** Click Download (verified) and prove a real browser download fired, saving
 *  markdown bytes: the suggested filename must carry the content-type-derived
 *  .md extension, and the saved temp file must be real stub-render output. */
export async function downloadVerifiedAndAssert(page: Page): Promise<string> {
  const downloadPromise = page.waitForEvent('download')
  await downloadButton(page).click()
  const download = await downloadPromise
  const filename = download.suggestedFilename()
  expect(
    filename.endsWith('.md'),
    `saved filename must use the markdown content-type extension, got: ${filename}`,
  ).toBe(true)
  const path = await download.path()
  expect(path, 'download must be persisted to disk by the browser').not.toBeNull()
  const content = (await import('node:fs')).readFileSync(path as string, 'utf8')
  expect(content, 'downloaded bytes must be real rendered markdown').toMatch(/# E2E (stub render \d+|seeded output v1)/)
  await expect(verifiedExportResult(page)).toBeVisible()
  await expect(verifiedExportResult(page)).toContainText('Verified export downloaded')
  await expect(verifiedExportResult(page)).toContainText(`Saved as ${filename}`)
  return filename
}
