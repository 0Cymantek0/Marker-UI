import { describe, expect, it } from 'vitest'
import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const tinyTextClass = /text-\[(?:9|10|11)px\]/g

async function collectSourceFiles(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true })
  const files = await Promise.all(
    entries.map((entry) => {
      const fullPath = path.join(dir, entry.name)
      if (entry.isDirectory()) return collectSourceFiles(fullPath)
      if (/\.(ts|tsx)$/.test(entry.name)) return [fullPath]
      return []
    }),
  )
  return files.flat()
}

describe('typography scale', () => {
  it('keeps frontend text at a readable minimum size', async () => {
    const files = await collectSourceFiles(srcDir)
    const offenders: string[] = []

    for (const file of files) {
      const source = await readFile(file, 'utf8')
      if (tinyTextClass.test(source)) {
        offenders.push(path.relative(srcDir, file))
      }
      tinyTextClass.lastIndex = 0
    }

    expect(offenders).toEqual([])
  })
})
