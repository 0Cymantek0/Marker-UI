import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const configPath = resolve(__dirname, '../../vite.config.ts')
const configText = readFileSync(configPath, 'utf-8')

describe('vite.config.ts network binding', () => {
  // Regression: Windows `localhost` resolves to IPv6 `::1` first. Vite's
  // default `host` binds only the IPv6 loopback, so the launcher's
  // `http://127.0.0.1:5173/` health check never connects and the launcher
  // hangs forever on step [6/6].
  it('sets server.host to true so both IPv4 and IPv6 loopback bind', () => {
    expect(configText).toMatch(/host:\s*true/)
  })

  // Regression: proxy target used `localhost` which on Windows often resolves
  // to `::1`. The backend binds `127.0.0.1`, so an IPv6 proxy target cannot
  // reach it and API requests fail.
  it('uses 127.0.0.1 for the /api proxy target, not localhost', () => {
    expect(configText).toMatch(/target:\s*`http:\/\/127\.0\.0\.1:\$\{process\.env\.BACKEND_PORT \|\| 8000\}`/)
    expect(configText).not.toMatch(/target:\s*`http:\/\/localhost/)
  })
})
