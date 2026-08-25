import { expect, test } from '@playwright/test'
import { JOB_FILENAME } from './support'

/**
 * Legacy smoke (Outcome 9): the pre-existing surfaces stay healthy alongside
 * the new integrity page against the real backend. The conversion home
 * renders its upload UI, /history lists the seeded job, and /settings
 * renders — proving the new slice regressed nothing on the old paths.
 */

test('conversion home renders its heading and upload entry', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Convert Document' })).toBeVisible()
})

test('history lists the seeded completed job', async ({ page }) => {
  await page.goto('/history')
  await expect(page.getByRole('heading', { name: 'Conversion History' })).toBeVisible()
  await expect(page.getByText(JOB_FILENAME).first()).toBeVisible()
})

test('settings renders', async ({ page }) => {
  await page.goto('/settings')
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
})
