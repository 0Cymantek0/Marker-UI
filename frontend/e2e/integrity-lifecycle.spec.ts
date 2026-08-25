import { expect, test } from '@playwright/test'
import {
  JOB_FILENAME,
  JOB_ID,
  contextHeading,
  downloadButton,
  downloadVerifiedAndAssert,
  expectReadyToExport,
  fetchCurrentToken,
  regenerateAsOf,
  staleBanner,
  verifiedExportResult,
} from './support'

/**
 * THE core proof: the full as-of lifecycle in a real browser against the real
 * backend — current export verified; the stale-after-load race (page pinned
 * token T1, server rotates to T2 behind its back) rejected with a reconciling
 * stale banner and zero false success; the stale-before-load bookmark case
 * detected immediately on load; and the recovery loop proving no cached
 * optimism survives a second rotation after a successful export.
 *
 * State discipline: every rotation reads the CURRENT token from the API
 * first; tests run serially (workers: 1) against the shared seeded job.
 */

test('current path: fresh load shows revision context and exports a verified markdown download', async ({ page, request }) => {
  // The page adopts the server's current token; prove the UI surfaces it.
  const currentToken = await fetchCurrentToken(request)
  await page.goto(`/integrity?job=${JOB_ID}`)

  await expectReadyToExport(page)
  // Revision context card: filename heading + full state token in title attr.
  await expect(contextHeading(page)).toBeVisible()
  await expect(page.locator(`[title="${currentToken}"]`).first()).toBeVisible()
  // No stale banner on a current load.
  await expect(staleBanner(page)).toHaveCount(0)

  // Verified export: real download event, .md extension, real bytes, success panel.
  await downloadVerifiedAndAssert(page)
  await expect(downloadButton(page)).toBeEnabled()
})

test('stale-after-load race: server rotates the pinned token, export is rejected and reconciled, never faked', async ({ page, request }) => {
  let downloads = 0
  page.on('download', () => {
    downloads += 1
  })

  await page.goto(`/integrity?job=${JOB_ID}`)
  await expectReadyToExport(page)

  // Second actor rotates server state behind the loaded page's back.
  const pinnedToken = await fetchCurrentToken(request)
  await regenerateAsOf(request, pinnedToken)
  const rotatedToken = await fetchCurrentToken(request)
  expect(rotatedToken).not.toBe(pinnedToken)

  // Export attempt against the now-stale pinned token: 409 stale_state.
  await downloadButton(page).click()

  // Reconciling stale banner with both tokens for comparison; no success UI.
  const banner = staleBanner(page)
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('This revision moved on the server')
  await expect(banner.locator(`[title="${pinnedToken}"]`)).toBeVisible()
  await expect(banner.locator(`[title="${rotatedToken}"]`)).toBeVisible()
  await expect(downloadButton(page)).toBeDisabled()
  await expect(verifiedExportResult(page)).toHaveCount(0)
  expect(downloads, 'stale rejection must not fire any browser download').toBe(0)

  // Reconciliation adopts the server's current state; export now succeeds.
  await banner.getByRole('button', { name: 'Refresh current state' }).click()
  await expect(banner).toHaveCount(0)
  await expectReadyToExport(page)
  await downloadVerifiedAndAssert(page)
  expect(downloads, 'exactly one real download after reconciliation').toBe(1)
})

test('stale-before-load: a bookmarked stale token is detected immediately on load, then recovers', async ({ page, request }) => {
  // Bookmark captured T1; server has since moved to T2.
  const bookmarkedToken = await fetchCurrentToken(request)
  await regenerateAsOf(request, bookmarkedToken)
  const currentToken = await fetchCurrentToken(request)
  expect(currentToken).not.toBe(bookmarkedToken)

  await page.goto(`/integrity?job=${JOB_ID}&as_of=${encodeURIComponent(bookmarkedToken)}`)

  // Stale banner shows immediately: the pinned bookmark ≠ server truth.
  const banner = staleBanner(page)
  await expect(banner).toBeVisible()
  await expect(banner.locator(`[title="${bookmarkedToken}"]`)).toBeVisible()
  await expect(banner.locator(`[title="${currentToken}"]`)).toBeVisible()
  await expect(downloadButton(page)).toBeDisabled()
  await expect(verifiedExportResult(page)).toHaveCount(0)

  // Refresh adopts the server token; a verified export then succeeds.
  await banner.getByRole('button', { name: 'Refresh current state' }).click()
  await expect(banner).toHaveCount(0)
  await expectReadyToExport(page)
  await downloadVerifiedAndAssert(page)
})

test('recovery loop repeats: after a verified export, another rotation re-stales the page — no cached optimism', async ({ page, request }) => {
  await page.goto(`/integrity?job=${JOB_ID}`)
  await expectReadyToExport(page)

  // First export succeeds against the current state.
  await downloadVerifiedAndAssert(page)

  // The server moves on again AFTER a successful export.
  const pinnedToken = await fetchCurrentToken(request)
  await regenerateAsOf(request, pinnedToken)

  // Attempting another export must hit the stale wall again — the earlier
  // success must not license an optimistic download.
  await downloadButton(page).click()
  const banner = staleBanner(page)
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('This revision moved on the server')
  await expect(banner.locator(`[title="${pinnedToken}"]`)).toBeVisible()
  await expect(downloadButton(page)).toBeDisabled()
  // The prior success panel is withdrawn while stale (never shown alongside).
  await expect(verifiedExportResult(page)).toHaveCount(0)

  // Second reconciliation also recovers to a verified export.
  await banner.getByRole('button', { name: 'Refresh current state' }).click()
  await expect(banner).toHaveCount(0)
  await expectReadyToExport(page)
  await downloadVerifiedAndAssert(page)

  // Context still names the seeded file throughout.
  await expect(contextHeading(page)).toHaveText(JOB_FILENAME)
})
