import assert from 'node:assert/strict'
import http from 'node:http'

import { afterEach, test } from 'vitest'

import {
  assertPublicDownloadUrl,
  fetchPublicDownload,
  nodePublicFetch,
  publicSocketLookup
} from './public-file-download'

const cleanupServers: http.Server[] = []

afterEach(async () => {
  await Promise.all(cleanupServers.splice(0).map(server => new Promise<void>(resolve => server.close(() => resolve()))))
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
