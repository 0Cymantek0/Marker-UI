import { defineConfig } from '@playwright/test'

/**
 * Real-browser E2E suite for the review-integrity vertical slice.
 *
 * Boots the REAL backend (backend/e2e/launch.py: real FastAPI app, real
 * SQLite, real as-of enforcement; only the render seam stubbed) and the REAL
 * vite dev server proxying /api to it, then drives Chromium through the full
 * current → stale → rejected → reconciled → recovered lifecycle over HTTP.
 *
 * Serialization is mandatory: every spec shares the single seeded job and
 * rotates its state token via POST regenerate, so workers MUST stay at 1 and
 * fullyParallel off. Specs always read the CURRENT token from the API before
 * regenerating — never assume a token from a previous test.
 */
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5199',
    // Real saved-file assertions: download events must materialize on disk.
    acceptDownloads: true,
  },
  webServer: [
    {
      // Real FastAPI app on a scratch SQLite DB seeded with completed job
      // `job-e2e-seeded` (markdown). Fresh scratch dir per process start.
      command: 'python backend/e2e/launch.py',
      cwd: '..',
      env: { MARKER_E2E_PORT: '8917' },
      url: 'http://127.0.0.1:8917/api/health',
      timeout: 180_000,
      reuseExistingServer: false,
      stdout: 'ignore',
    },
    {
      // Real vite dev server; /api proxied to the E2E backend via BACKEND_PORT.
      command: 'pnpm dev --port 5199 --strictPort',
      cwd: '.',
      env: { BACKEND_PORT: '8917' },
      url: 'http://localhost:5199',
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: 'ignore',
    },
  ],
})
