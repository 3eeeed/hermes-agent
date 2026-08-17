import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { afterEach, test } from 'vitest'

import {
  materializeLocalContextFile,
  materializeRemoteContextFile,
  pruneContextFileCache,
  safeContextFilename
} from './context-file-cache'

const cleanupPaths: string[] = []
const cleanupServers: http.Server[] = []

afterEach(async () => {
  await Promise.all(cleanupServers.splice(0).map(server => new Promise<void>(resolve => server.close(() => resolve()))))

  for (const target of cleanupPaths.splice(0)) {
    fs.rmSync(target, { force: true, recursive: true })
  }
})

async function serve(handler: http.RequestListener): Promise<{ server: http.Server; url: string }> {
  const server = http.createServer(handler)
  cleanupServers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()

  assert.ok(address && typeof address === 'object')

  return { server, url: `http://127.0.0.1:${address.port}` }
}

test('resolves a local file URL through the hardened file resolver', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-local-'))
  cleanupPaths.push(root)
  const source = path.join(root, 'review package.pdf')
  fs.writeFileSync(source, 'review')

  const resolved = await materializeLocalContextFile(pathToFileURL(source).toString())

  assert.equal(resolved, source)
})

test('sanitizes a suggested filename to one cache-local path segment', () => {
  assert.equal(safeContextFilename('../../secret.pdf'), 'secret.pdf')
  assert.equal(safeContextFilename('folder\\review.pdf'), 'review.pdf')
  assert.equal(safeContextFilename('..'), 'download')
})

test('rejects non-HTTP sources before writing to the cache', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  await assert.rejects(
    materializeRemoteContextFile({
      cacheRoot,
      maxBytes: 1024,
      suggestedFilename: 'payload.txt',
      url: 'data:text/plain,not-a-remote-file'
    }),
    /must use HTTP or HTTPS/
  )
  assert.deepEqual(fs.readdirSync(cacheRoot), [])
})

test('uses a non-secret cache identity and an injected trusted downloader', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)
  let calls = 0

  const cached = await materializeRemoteContextFile({
    cacheKey: 'gateway:remote-work:/srv/private.pdf',
    cacheRoot,
    fetchImpl: async () => {
      calls += 1

      return new Response('trusted gateway bytes', { status: 200 })
    },
    maxBytes: 1024,
    suggestedFilename: 'private.pdf',
    url: 'https://gateway.invalid/api/files/download?path=%2Fsrv%2Fprivate.pdf'
  })

  const expectedKey = (await import('node:crypto'))
    .createHash('sha256')
    .update('gateway:remote-work:/srv/private.pdf')
    .digest('hex')

  assert.equal(path.basename(path.dirname(cached)), expectedKey)
  assert.equal(calls, 1)
})

test('writes cached gateway content with private directory and file permissions', async () => {
  const { url } = await serve((_request, response) => response.end('private content'))
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  const cached = await materializeRemoteContextFile({
    cacheRoot,
    maxBytes: 1024,
    suggestedFilename: 'private.txt',
    url: `${url}/private.txt`
  })

  assert.equal(fs.statSync(path.dirname(cached)).mode & 0o777, 0o700)
  assert.equal(fs.statSync(cached).mode & 0o777, 0o600)
})

test('refreshes a cached file after its freshness window expires', async () => {
  let requests = 0

  const { url } = await serve((_request, response) => {
    requests += 1
    response.end(`version-${requests}`)
  })

  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  const options = {
    cacheMaxAgeMs: 100,
    cacheRoot,
    maxBytes: 1024,
    suggestedFilename: 'changing.txt',
    url: `${url}/changing.txt`
  }

  const cached = await materializeRemoteContextFile(options)
  const stale = new Date(Date.now() - 1_000)
  fs.utimesSync(cached, stale, stale)
  const refreshed = await materializeRemoteContextFile(options)

  assert.equal(refreshed, cached)
  assert.equal(requests, 2)
  assert.equal(fs.readFileSync(refreshed, 'utf8'), 'version-2')
})

test('removes abandoned partial downloads after the retention window', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)
  const cacheDir = path.join(cacheRoot, 'abandoned')
  fs.mkdirSync(cacheDir)
  const partial = path.join(cacheDir, '.report.pdf.deadbeef.part')
  fs.writeFileSync(partial, 'partial')
  const stale = new Date(Date.now() - 1_000)
  fs.utimesSync(partial, stale, stale)

  await pruneContextFileCache(cacheRoot, { maxBytes: 1024, retentionMs: 100 })

  assert.equal(fs.existsSync(partial), false)
})

test('keeps the cache within its byte budget after concurrent downloads publish', async () => {
  const payload = Buffer.alloc(400, 0x41)
  const { url } = await serve((_request, response) => response.end(payload))
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  await Promise.all(
    ['a', 'b', 'c', 'd'].map(name =>
      materializeRemoteContextFile({
        cacheMaxBytes: 900,
        cacheRoot,
        maxBytes: 1024,
        suggestedFilename: `${name}.bin`,
        url: `${url}/${name}.bin`
      })
    )
  )

  const total = fs
    .readdirSync(cacheRoot, { recursive: true })
    .map(entry => path.join(cacheRoot, String(entry)))
    .filter(entry => fs.statSync(entry).isFile())
    .reduce((sum, entry) => sum + fs.statSync(entry).size, 0)

  assert.ok(total <= 900, `cache grew to ${total} bytes, above the 900 byte budget`)
})

test('prunes the oldest cached file when the cache exceeds its byte budget', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)
  const olderDir = path.join(cacheRoot, 'older')
  const newerDir = path.join(cacheRoot, 'newer')
  fs.mkdirSync(olderDir)
  fs.mkdirSync(newerDir)
  const older = path.join(olderDir, 'older.bin')
  const newer = path.join(newerDir, 'newer.bin')
  fs.writeFileSync(older, Buffer.alloc(600))
  fs.writeFileSync(newer, Buffer.alloc(600))
  const oldTime = new Date(Date.now() - 60_000)
  fs.utimesSync(older, oldTime, oldTime)

  await pruneContextFileCache(cacheRoot, { maxBytes: 700, retentionMs: 86_400_000 })

  assert.equal(fs.existsSync(older), false)
  assert.equal(fs.existsSync(newer), true)
})

test('materializes a remote file once and reuses the cached copy', async () => {
  let requests = 0
  const payload = Buffer.from('factory review package')

  const { url } = await serve((_request, response) => {
    requests += 1
    response.writeHead(200, {
      'content-disposition': 'attachment; filename="review.pdf"',
      'content-length': payload.length,
      'content-type': 'application/pdf'
    })
    response.end(payload)
  })

  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  const first = await materializeRemoteContextFile({
    cacheRoot,
    maxBytes: 1024,
    suggestedFilename: 'review.pdf',
    url: `${url}/review.pdf`
  })

  const second = await materializeRemoteContextFile({
    cacheRoot,
    maxBytes: 1024,
    suggestedFilename: 'review.pdf',
    url: `${url}/review.pdf`
  })

  assert.equal(second, first)
  assert.equal(requests, 1)
  assert.ok(path.resolve(first).startsWith(`${path.resolve(cacheRoot)}${path.sep}`))
  assert.deepEqual(fs.readFileSync(first), payload)
})

test('coalesces concurrent writes by destination even when request options differ', async () => {
  let requests = 0

  const { url } = await serve((_request, response) => {
    requests += 1
    setTimeout(() => response.end('shared destination'), 20)
  })

  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  const base = {
    cacheKey: 'gateway:remote-work:/srv/shared.zip',
    cacheRoot,
    maxBytes: 1024,
    suggestedFilename: 'shared.zip',
    url: `${url}/shared.zip`
  }

  const [first, second] = await Promise.all([
    materializeRemoteContextFile({ ...base, timeoutMs: 1_000 }),
    materializeRemoteContextFile({ ...base, timeoutMs: 2_000 })
  ])

  assert.equal(first, second)
  assert.equal(requests, 1)
})

test('coalesces concurrent requests for the same remote file', async () => {
  let requests = 0
  const payload = Buffer.from('one transfer')

  const { url } = await serve((_request, response) => {
    requests += 1
    setTimeout(() => {
      response.writeHead(200, { 'content-length': payload.length })
      response.end(payload)
    }, 20)
  })

  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  const options = {
    cacheRoot,
    maxBytes: 1024,
    suggestedFilename: 'shared.zip',
    url: `${url}/shared.zip`
  }

  const [first, second] = await Promise.all([
    materializeRemoteContextFile(options),
    materializeRemoteContextFile(options)
  ])

  assert.equal(second, first)
  assert.equal(requests, 1)
})

test('rejects an oversized streamed file without leaving a partial file', async () => {
  const payload = Buffer.alloc(2048, 0x42)

  const { url } = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/octet-stream' })
    response.write(payload.subarray(0, 1024))
    response.end(payload.subarray(1024))
  })

  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-file-'))
  cleanupPaths.push(cacheRoot)

  await assert.rejects(
    materializeRemoteContextFile({
      cacheRoot,
      maxBytes: 1024,
      suggestedFilename: 'oversized.bin',
      url: `${url}/oversized.bin`
    }),
    /exceeds 1024 bytes/
  )

  const entries = fs.readdirSync(cacheRoot, { recursive: true }).map(String)
  assert.equal(entries.some(entry => entry.endsWith('.part') || entry.endsWith('oversized.bin')), false)
})
