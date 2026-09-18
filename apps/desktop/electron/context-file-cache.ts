import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { resolveReadableFileForIpc } from './hardening'

export interface MaterializeRemoteContextFileOptions {
  cacheKey?: string
  cacheMaxAgeMs?: number
  cacheMaxBytes?: number
  cacheRoot: string
  fetchImpl?: (input: string | URL, init?: RequestInit) => Promise<Response>
  maxBytes: number
  suggestedFilename?: string
  timeoutMs?: number
  url: string
}

export interface ContextFileCachePruneOptions {
  keep?: string
  maxBytes: number
  retentionMs: number
}

interface CachedEntry {
  mtimeMs: number
  pathname: string
  size: number
}

export async function pruneContextFileCache(
  cacheRoot: string,
  options: ContextFileCachePruneOptions
): Promise<void> {
  const root = path.resolve(cacheRoot)
  const running = cachePrunes.get(root)

  // Serialize pruning per cache root: concurrent sweeps race on the same
  // entries and can each delete what the other just counted.
  const pending = (running ?? Promise.resolve())
    .catch(() => undefined)
    .then(() => pruneContextFileCacheOnce(root, options))

  cachePrunes.set(root, pending)

  return pending.finally(() => {
    if (cachePrunes.get(root) === pending) {
      cachePrunes.delete(root)
    }
  })
}

const cachePrunes = new Map<string, Promise<void>>()

async function pruneContextFileCacheOnce(
  cacheRoot: string,
  options: ContextFileCachePruneOptions
): Promise<void> {
  let directories: fs.Dirent[]

  try {
    directories = await fs.promises.readdir(cacheRoot, { withFileTypes: true })
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return
    }

    throw error
  }

  const entries: CachedEntry[] = []
  const now = Date.now()

  for (const directory of directories) {
    const directoryPath = path.join(cacheRoot, directory.name)

    if (directory.isSymbolicLink() || !directory.isDirectory()) {
      await fs.promises.rm(directoryPath, { force: true, recursive: directory.isDirectory() }).catch(() => undefined)

      continue
    }

    const children = await fs.promises.readdir(directoryPath, { withFileTypes: true }).catch(() => [])

    for (const child of children) {
      const pathname = path.join(directoryPath, child.name)
      const stat = await fs.promises.lstat(pathname).catch(() => null)

      if (child.name.endsWith('.part')) {
        if (stat && now - stat.mtimeMs > options.retentionMs) {
          await fs.promises.unlink(pathname).catch(() => undefined)
        }

        continue
      }

      if (!stat || stat.isSymbolicLink() || !stat.isFile()) {
        await fs.promises.rm(pathname, { force: true, recursive: Boolean(stat?.isDirectory()) }).catch(() => undefined)

        continue
      }

      if (now - stat.mtimeMs > options.retentionMs) {
        await fs.promises.unlink(pathname).catch(() => undefined)

        continue
      }

      entries.push({ mtimeMs: stat.mtimeMs, pathname, size: stat.size })
    }
  }

  let totalBytes = entries.reduce((total, entry) => total + entry.size, 0)
  const keep = options.keep ? path.resolve(options.keep) : ''

  for (const entry of entries.sort((left, right) => left.mtimeMs - right.mtimeMs)) {
    if (totalBytes <= options.maxBytes) {
      break
    }

    // Never evict the file the caller is about to hand to the clipboard or
    // shell, even when it alone exceeds the budget.
    if (keep && path.resolve(entry.pathname) === keep) {
      continue
    }

    await fs.promises.unlink(entry.pathname).catch(() => undefined)
    totalBytes -= entry.size
  }

  await Promise.all(
    directories
      .filter(directory => directory.isDirectory() && !directory.isSymbolicLink())
      .map(directory => fs.promises.rmdir(path.join(cacheRoot, directory.name)).catch(() => undefined))
  )
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

async function cachedFile(pathname: string, maxAgeMs: number): Promise<null | string> {
  try {
    const stat = await fs.promises.lstat(pathname)

    if (stat.isSymbolicLink() || !stat.isFile() || Date.now() - stat.mtimeMs > maxAgeMs) {
      await fs.promises.unlink(pathname)

      return null
    }

    await fs.promises.chmod(pathname, 0o600)

    return pathname
  } catch {
    return null
  }
}

async function ensurePrivateDirectory(pathname: string): Promise<void> {
  await fs.promises.mkdir(pathname, { mode: 0o700, recursive: true })
  const stat = await fs.promises.lstat(pathname)

  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error('Context file cache path must be a private directory')
  }

  await fs.promises.chmod(pathname, 0o700)
}

export async function materializeLocalContextFile(source: string): Promise<string> {
  const candidate = /^file:/i.test(source) ? fileURLToPath(source) : source
  const { resolvedPath } = await resolveReadableFileForIpc(candidate, { purpose: 'Context menu file' })

  return resolvedPath
}

const remoteMaterializations = new Map<string, Promise<string>>()

function remoteContextCachePlan(options: MaterializeRemoteContextFileOptions): {
  cacheDir: string
  destination: string
  source: URL
} {
  const source = new URL(options.url)

  if (!['http:', 'https:'].includes(source.protocol)) {
    throw new Error('Remote context files must use HTTP or HTTPS')
  }

  const cacheKey = crypto
    .createHash('sha256')
    .update(options.cacheKey || source.toString())
    .digest('hex')

  const cacheDir = path.join(path.resolve(options.cacheRoot), cacheKey)
  const destination = path.join(cacheDir, safeContextFilename(options.suggestedFilename || path.basename(source.pathname)))

  return { cacheDir, destination, source }
}

export function materializeRemoteContextFile(options: MaterializeRemoteContextFileOptions): Promise<string> {
  let key: string

  try {
    key = remoteContextCachePlan(options).destination
  } catch (error) {
    return Promise.reject(error)
  }

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
  const { cacheDir, destination, source } = remoteContextCachePlan(options)

  await ensurePrivateDirectory(options.cacheRoot)
  await ensurePrivateDirectory(cacheDir)
  const existing = await cachedFile(destination, options.cacheMaxAgeMs ?? 300_000)

  if (existing) {
    return existing
  }

  const temporary = path.join(cacheDir, `.${path.basename(destination)}.${crypto.randomBytes(6).toString('hex')}.part`)

  const fetchImpl = options.fetchImpl || fetch

  const response = await fetchImpl(source, {
    redirect: 'error',
    signal: AbortSignal.timeout(options.timeoutMs ?? 30_000)
  })

  if (!response.ok || !response.body) {
    await response.body?.cancel().catch(() => undefined)
    throw new Error(`Failed to download context file: HTTP ${response.status}`)
  }

  const contentLength = Number(response.headers.get('content-length') || 0)

  if (contentLength > options.maxBytes) {
    await response.body.cancel().catch(() => undefined)
    throw new Error(`Remote context file exceeds ${options.maxBytes} bytes`)
  }

  const handle = await fs.promises.open(temporary, 'wx', 0o600)
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
    await fs.promises.chmod(destination, 0o600)

    // Enforce the budget after publication: pruning beforehand cannot account
    // for files that concurrent downloads are about to add, so the cache could
    // otherwise grow past its limit without bound.
    await pruneContextFileCache(options.cacheRoot, {
      keep: destination,
      maxBytes: options.cacheMaxBytes ?? 1_073_741_824,
      retentionMs: 86_400_000
    })

    return destination
  } catch (error) {
    await handle.close().catch(() => undefined)
    await fs.promises.rm(temporary, { force: true }).catch(() => undefined)
    throw error
  }
}
