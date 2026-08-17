import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { resolveReadableFileForIpc } from './hardening'

export interface MaterializeRemoteContextFileOptions {
  cacheRoot: string
  maxBytes: number
  suggestedFilename?: string
  timeoutMs?: number
  url: string
}

export function safeContextFilename(value: string | undefined): string {
  const basename = path.basename(String(value || 'download').replace(/[\\/]+/g, path.sep))

  const cleaned = Array.from(basename)
    .filter(character => {
      const code = character.charCodeAt(0)

      return code > 31 && code !== 127
    })
    .join('')
    .trim()

  return cleaned && cleaned !== '.' && cleaned !== '..' ? cleaned : 'download'
}

async function cachedFile(pathname: string): Promise<null | string> {
  try {
    const stat = await fs.promises.stat(pathname)

    return stat.isFile() ? pathname : null
  } catch {
    return null
  }
}

export async function materializeLocalContextFile(source: string): Promise<string> {
  const candidate = /^file:/i.test(source) ? fileURLToPath(source) : source
  const { resolvedPath } = await resolveReadableFileForIpc(candidate, { purpose: 'Context menu file' })

  return resolvedPath
}

const remoteMaterializations = new Map<string, Promise<string>>()

export function materializeRemoteContextFile(options: MaterializeRemoteContextFileOptions): Promise<string> {
  const key = JSON.stringify([
    path.resolve(options.cacheRoot),
    options.url,
    options.suggestedFilename || '',
    options.maxBytes,
    options.timeoutMs ?? 30_000
  ])

  const running = remoteMaterializations.get(key)

  if (running) {
    return running
  }

  const pending = materializeRemoteContextFileOnce(options)
  remoteMaterializations.set(key, pending)

  return pending.finally(() => {
    if (remoteMaterializations.get(key) === pending) {
      remoteMaterializations.delete(key)
    }
  })
}

async function materializeRemoteContextFileOnce(options: MaterializeRemoteContextFileOptions): Promise<string> {
  const source = new URL(options.url)

  if (!['http:', 'https:'].includes(source.protocol)) {
    throw new Error('Remote context files must use HTTP or HTTPS')
  }

  const cacheKey = crypto.createHash('sha256').update(source.toString()).digest('hex')
  const cacheDir = path.join(options.cacheRoot, cacheKey)
  const destination = path.join(cacheDir, safeContextFilename(options.suggestedFilename || path.basename(source.pathname)))
  const existing = await cachedFile(destination)

  if (existing) {
    return existing
  }

  await fs.promises.mkdir(cacheDir, { recursive: true })
  const temporary = path.join(cacheDir, `.${path.basename(destination)}.${crypto.randomBytes(6).toString('hex')}.part`)

  const response = await fetch(source, {
    redirect: 'follow',
    signal: AbortSignal.timeout(options.timeoutMs ?? 30_000)
  })

  if (!response.ok || !response.body) {
    throw new Error(`Failed to download context file: HTTP ${response.status}`)
  }

  if (!['http:', 'https:'].includes(new URL(response.url).protocol)) {
    throw new Error('Remote context file redirected to an unsupported protocol')
  }

  const contentLength = Number(response.headers.get('content-length') || 0)

  if (contentLength > options.maxBytes) {
    throw new Error(`Remote context file exceeds ${options.maxBytes} bytes`)
  }

  const handle = await fs.promises.open(temporary, 'wx')
  let written = 0

  try {
    const reader = response.body.getReader()

    while (true) {
      const { done, value } = await reader.read()

      if (done) {
        break
      }

      written += value.byteLength

      if (written > options.maxBytes) {
        await reader.cancel()
        throw new Error(`Remote context file exceeds ${options.maxBytes} bytes`)
      }

      await handle.write(value)
    }

    await handle.sync()
    await handle.close()
    await fs.promises.rename(temporary, destination)

    return destination
  } catch (error) {
    await handle.close().catch(() => undefined)
    await fs.promises.rm(temporary, { force: true }).catch(() => undefined)
    throw error
  }
}
