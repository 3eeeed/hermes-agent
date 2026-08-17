import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { materializeRemoteContextFile } from './context-file-cache'
import {
  assertPublicDownloadUrl,
  fetchPublicDownload,
  nodePublicFetch,
  publicSocketLookup
} from './public-file-download'

const cleanupPaths: string[] = []
const cleanupServers: http.Server[] = []

afterEach(async () => {
  await Promise.all(cleanupServers.splice(0).map(server => new Promise<void>(resolve => server.close(() => resolve()))))

  for (const target of cleanupPaths.splice(0)) {
    fs.rmSync(target, { force: true, recursive: true })
  }
})

async function serve(handler: http.RequestListener): Promise<{ port: number }> {
  const server = http.createServer(handler)
  cleanupServers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()

  assert.ok(address && typeof address === 'object')

  return { port: address.port }
}

const publicLookup = async () => [{ address: '93.184.216.34', family: 4 as const }]

test('rejects literal and DNS-resolved private network destinations', async () => {
  await assert.rejects(() => assertPublicDownloadUrl('http://127.0.0.1/private', publicLookup), /public network/i)
  await assert.rejects(
    () => assertPublicDownloadUrl('http://[::ffff:7f00:1]/private', publicLookup),
    /public network/i
  )
  await assert.rejects(
    () => assertPublicDownloadUrl('https://files.example/report.pdf', async () => [{ address: '10.1.2.3', family: 4 }] as const),
    /public network/i
  )
  await assert.rejects(
    () =>
      assertPublicDownloadUrl('https://files.example/report.pdf', async () => [
        { address: '93.184.216.34', family: 4 },
        { address: '169.254.169.254', family: 4 }
      ] as const),
    /public network/i
  )
})

test('accepts an HTTP URL only when every resolved address is public', async () => {
  const url = await assertPublicDownloadUrl('https://files.example/report.pdf', publicLookup)

  assert.equal(url.toString(), 'https://files.example/report.pdf')
})

test('revalidates redirects before requesting the next destination', async () => {
  const requested: string[] = []

  const fetchImpl = async (url: string | URL) => {
    requested.push(String(url))

    return new Response(null, {
      status: 302,
      headers: { location: 'http://127.0.0.1/private' }
    })
  }

  await assert.rejects(
    () => fetchPublicDownload('https://files.example/report.pdf', { fetchImpl, lookup: publicLookup }),
    /public network/i
  )
  assert.deepEqual(requested, ['https://files.example/report.pdf'])
})

test('blocks a private address at connect time even after a public DNS answer', () => {
  const attempts: unknown[] = []

  const lookup = publicSocketLookup((_hostname, _options, callback) => {
    callback(null, [{ address: '127.0.0.1', family: 4 }] as never)
  })

  lookup('files.example', { all: true }, (error, ...rest) => attempts.push([error, ...rest]))

  const [failure] = attempts[0] as [Error]
  assert.match(failure.message, /public network/i)
})

test('allows a public address at connect time', () => {
  const results: unknown[] = []

  const lookup = publicSocketLookup((_hostname, _options, callback) => {
    callback(null, [{ address: '93.184.216.34', family: 4 }] as never)
  })

  lookup('files.example', { all: true }, (error, addresses) => results.push([error, addresses]))

  assert.deepEqual(results[0], [null, [{ address: '93.184.216.34', family: 4 }]])
})

test('refuses a real connection when the hostname rebinds to loopback', async () => {
  const { port } = await serve((_request, response) => response.end('should never be reachable'))

  await assert.rejects(
    () =>
      nodePublicFetch(`http://rebind.invalid:${port}/private`, {
        lookup: publicLookup,
        socketLookup: publicSocketLookup((_hostname, _options, callback) => {
          callback(null, [{ address: '127.0.0.1', family: 4 }] as never)
        })
      }),
    /public network/i
  )
})

test('downloads real bytes when every resolution stays public', async () => {
  const { port } = await serve((_request, response) => response.end('public payload'))

  const response = await nodePublicFetch(`http://public.invalid:${port}/report.txt`, {
    lookup: publicLookup,
    // Stands in for a genuinely public address: the guard is exercised in the
    // rebinding test above, so here we only assert real bytes flow through.
    socketLookup: (_hostname, _options, callback) => {
      callback(null, [{ address: '127.0.0.1', family: 4 }] as never)
    }
  })

  assert.equal(response.status, 200)
  assert.equal(await response.text(), 'public payload')
})

test('streams a large body to completion without stalling on connection teardown', async () => {
  const chunk = Buffer.alloc(64 * 1024, 0x41)
  const chunks = 24

  const { port } = await serve((_request, response) => {
    response.writeHead(200, { 'content-length': String(chunk.length * chunks) })

    let sent = 0

    const push = () => {
      if (sent === chunks) {
        response.end()

        return
      }

      sent += 1
      response.write(chunk, () => setTimeout(push, 5))
    }

    push()
  })

  const response = await nodePublicFetch(`http://public.invalid:${port}/large.bin`, {
    lookup: publicLookup,
    socketLookup: (_hostname, _options, callback) => {
      callback(null, [{ address: '127.0.0.1', family: 4 }] as never)
    }
  })

  const body = Buffer.from(await response.arrayBuffer())

  assert.equal(body.length, chunk.length * chunks)
}, 15_000)

test('drains redirect-hop bodies instead of abandoning them', async () => {
  const cancelled: string[] = []

  const hop = (name: string, init: ResponseInit) => {
    const stream = new ReadableStream({
      cancel: () => {
        cancelled.push(name)
      },
      pull: controller => {
        controller.enqueue(new Uint8Array(1024))
      }
    })

    return new Response(stream, init)
  }

  let call = 0

  const fetchImpl = async () => {
    call += 1

    return call === 1
      ? hop('redirect', { status: 302, headers: { location: 'https://files.example/final.pdf' } })
      : hop('final', { status: 200 })
  }

  const response = await fetchPublicDownload('https://files.example/report.pdf', {
    fetchImpl,
    lookup: publicLookup
  })

  assert.equal(response.status, 200)
  assert.deepEqual(cancelled, ['redirect'])
})

test('releases the real connection pool when the cache rejects a failing download', async () => {
  const sockets = { closed: 0, opened: 0 }
  const payload = Buffer.alloc(2_000_000, 0x41)

  const { port } = await serve((request, response) => {
    if (request.url?.includes('toobig')) {
      response.writeHead(200, { 'content-length': String(payload.length) })
      response.end(payload)

      return
    }

    response.writeHead(500, { 'content-length': String(payload.length) })
    response.end(payload)
  })

  cleanupServers[cleanupServers.length - 1].on('connection', socket => {
    sockets.opened += 1
    socket.on('close', () => {
      sockets.closed += 1
    })
  })

  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-real-agent-'))
  cleanupPaths.push(cacheRoot)

  for (const name of ['toobig', 'failing']) {
    await assert.rejects(() =>
      materializeRemoteContextFile({
        cacheRoot,
        fetchImpl: (input, init) =>
          nodePublicFetch(String(input), {
            abandonedBodyGraceMs: 200,
            lookup: publicLookup,
            signal: init?.signal,
            socketLookup: (_hostname, _options, callback) => {
              callback(null, [{ address: '127.0.0.1', family: 4 }] as never)
            }
          }),
        maxBytes: 1024,
        suggestedFilename: `${name}.bin`,
        url: `http://public.invalid:${port}/${name}.bin`
      })
    )
  }

  const deadline = Date.now() + 8_000

  while (sockets.closed < sockets.opened && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 50))
  }

  assert.equal(sockets.opened > 0, true)
  assert.equal(
    sockets.closed,
    sockets.opened,
    `rejected downloads leaked connections: opened=${sockets.opened} closed=${sockets.closed}`
  )
  assert.deepEqual(fs.readdirSync(cacheRoot, { recursive: true }).filter(entry => String(entry).endsWith('.bin')), [])
}, 25_000)

test('closes the connection pool when a caller abandons the response body', async () => {
  const sockets = { closed: 0, opened: 0 }
  // A large body keeps the socket busy, which is exactly when an abandoned
  // response leaks the connection.
  const payload = Buffer.alloc(2_000_000, 0x41)

  const { port } = await serve((_request, response) => {
    response.writeHead(200, { 'content-length': String(payload.length) })
    response.end(payload)
  })

  cleanupServers[cleanupServers.length - 1].on('connection', socket => {
    sockets.opened += 1
    socket.on('close', () => {
      sockets.closed += 1
    })
  })

  for (let index = 0; index < 5; index += 1) {
    const response = await nodePublicFetch(`http://public.invalid:${port}/abandon-${index}.bin`, {
      abandonedBodyGraceMs: 200,
      lookup: publicLookup,
      socketLookup: (_hostname, _options, callback) => {
        callback(null, [{ address: '127.0.0.1', family: 4 }] as never)
      }
    })

    // Never read the body: the download layer drops it whenever a size or
    // status check rejects the response.
    void response
  }

  const deadline = Date.now() + 8_000

  while (sockets.closed < sockets.opened && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 50))
  }

  assert.equal(sockets.opened > 0, true)
  assert.equal(
    sockets.closed,
    sockets.opened,
    `abandoned bodies leaked connections: opened=${sockets.opened} closed=${sockets.closed}`
  )
}, 20_000)
