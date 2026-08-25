import { expect, test } from '@playwright/test'
import {
  JOB_FILENAME,
  JOB_ID,
  contextHeading,
  downloadButton,
  expectReadyToExport,
} from './support'

/**
 * Entry surfaces of the integrity page: the picker at /integrity (no params)
 * lists the seeded completed job, picking it deep-links the URL (?job=<id>),
 * the manual "Load by Job ID" form loads state directly, and PageHeader's
 * "Change job" returns to the picker where a re-pick reloads fresh state.
 *
 * Direct deep entry (?job=<id>) is exercised end-to-end by the lifecycle and
 * failure specs; this file owns the picker round-trip only.
 */

test('picker lists the seeded completed job and picking it deep-links the URL', async ({ page }) => {
  await page.goto('/integrity')

  await expect(page.getByRole('heading', { name: 'Load by Job ID' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Recent completed jobs' })).toBeVisible()

  // The seeded job's pick button is named by its filename.
  const pickButton = page.getByRole('button', { name: new RegExp(`${JOB_FILENAME.replace('.', '\\.')}`) })
  await expect(pickButton).toBeVisible()
  await pickButton.click()

  // State loads and the URL becomes the deep link.
  await expectReadyToExport(page)
  await expect(page).toHaveURL(new RegExp(`/integrity\\?job=${JOB_ID}$`))
})

test('manual "Load by Job ID" form loads the job state', async ({ page }) => {
  await page.goto('/integrity')

  await page.getByLabel('Job ID', { exact: true }).fill(JOB_ID)
  await page.getByRole('button', { name: 'Load job state' }).click()

  await expectReadyToExport(page)
  await expect(page).toHaveURL(new RegExp(`/integrity\\?job=${JOB_ID}$`))
})

test('"Change job" returns to the picker and a re-pick reloads state', async ({ page }) => {
  await page.goto(`/integrity?job=${JOB_ID}`)
  await expectReadyToExport(page)

  await page.getByRole('button', { name: 'Change job' }).click()

  // Back on the picker: URL stripped to /integrity, picker surfaces back.
  await expect(page.getByRole('heading', { name: 'Load by Job ID' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Recent completed jobs' })).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`/integrity$`))
  // The job context is gone with the picker shown.
  await expect(contextHeading(page)).toHaveCount(0)
  await expect(downloadButton(page)).toHaveCount(0)

  // Re-picking reloads the state from the server.
  await page.getByRole('button', { name: new RegExp(`${JOB_FILENAME.replace('.', '\\.')}`) }).click()
  await expectReadyToExport(page)
  await expect(contextHeading(page)).toHaveText(JOB_FILENAME)
})
