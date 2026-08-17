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
