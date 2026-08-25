import { expect, test } from '@playwright/test'
import { JOB_ID, contextHeading, downloadButton, expectReadyToExport } from './support'

/**
 * Conservative failure: when the backend is unreachable at load time, the
 * page must render a role=alert error (no context card, no download action,
 * nothing exported), and Retry must recover against the live backend once
 * connectivity returns.
 */

test('unreachable backend renders a conservative error; Retry recovers once the API answers', async ({ page }) => {
  // Sever every API call before any navigation.
  await page.route('**/api/**', (route) => route.abort())
  await page.goto(`/integrity?job=${JOB_ID}`)

  const alert = page.getByRole('alert', { name: 'Operation failed' })
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('could not be loaded from the server')
  await expect(alert).toContainText('Nothing has been exported')

  // No context card, no export action — nothing to act on.
  await expect(contextHeading(page)).toHaveCount(0)
  await expect(downloadButton(page)).toHaveCount(0)

  // Connectivity restored: Retry re-fetches the authoritative state.
  await page.unroute('**/api/**')
  await alert.getByRole('button', { name: 'Retry' }).click()

  await expect(alert).toHaveCount(0)
  await expectReadyToExport(page)
})
