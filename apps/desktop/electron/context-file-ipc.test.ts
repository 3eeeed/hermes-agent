import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type ContextFileIpcDeps, handleContextFileAction, osClipboardFormat } from './context-file-ipc'

function deps(overrides: Partial<ContextFileIpcDeps> = {}): ContextFileIpcDeps & { copied: string[] } {
  const copied: string[] = []

  return {
    copied,
    copyFileBuffer: () => {},
    copyText: value => {
      copied.push(value)
    },
    log: () => {},
    materialize: async () => '/tmp/cache/report.pdf',
    openPath: async () => '',
    platform: 'linux',
    revealFile: () => {},
    ...overrides
  }
}

const descriptor = JSON.stringify({ kind: 'local', name: 'report.pdf', source: '/srv/report.pdf' })

test('runs a validated file action for a DOM descriptor', async () => {
  const runtime = deps()

  assert.deepEqual(await handleContextFileAction({ action: 'copy-path', descriptor }, runtime), { ok: true })
  assert.deepEqual(runtime.copied, ['/tmp/cache/report.pdf'])
})

test('refuses actions the menu never offers and targets that are not files', async () => {
  const runtime = deps()

  assert.equal((await handleContextFileAction({ action: 'open-link', descriptor }, runtime)).ok, false)
  assert.equal(
    (await handleContextFileAction({ action: 'open-file', linkUrl: 'https://example.com/report.pdf' }, runtime)).ok,
    false
  )
  assert.equal((await handleContextFileAction({ action: 'open-file', descriptor: '{"bad":1}' }, runtime)).ok, false)
  assert.deepEqual(runtime.copied, [])
})

test('reports an opener failure without leaking its detail', async () => {
  const logged: string[] = []

  const result = await handleContextFileAction(
    { action: 'open-file', descriptor },
    deps({ log: message => logged.push(message), openPath: async () => 'no handler for /srv/secret' })
  )

  assert.equal(result.ok, false)
  assert.equal(JSON.stringify(result).includes('/srv/secret'), false)
  assert.equal(logged.length, 1)
})

test('names raw OS clipboard formats the way Electron 44 expects', () => {
  assert.equal(osClipboardFormat('text/uri-list'), 'electron application/osclipboard;format="text/uri-list"')
})

test('downloads a gateway file through the routed gateway client, never a DOM URL', async () => {
  const fs = await import('node:fs')
  const os = await import('node:os')
  const path = await import('node:path')
  const { createContextFileMaterializer } = await import('./context-file-ipc')
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-gateway-'))
  const requests: unknown[] = []

  try {
    const materialize = createContextFileMaterializer({
      cacheRoot,
      publicFetch: async () => {
        throw new Error('a gateway file must not use the public fetch')
      },
      saveGatewayFileTo: async (request, destination) => {
        requests.push(request)
        fs.writeFileSync(destination, 'gateway bytes')
      }
    })

    const model = {
      connectionId: 'studio-ssh',
      items: [],
      kind: 'remote-file' as const,
      name: 'report.pdf',
      profile: 'remote-work',
      remoteKind: 'gateway' as const,
      source: '/srv/report.pdf'
    }

    const first = await materialize(model)
    const second = await materialize(model)

    assert.equal(second, first)
    assert.equal(fs.readFileSync(first, 'utf8'), 'gateway bytes')
    assert.ok(path.resolve(first).startsWith(`${path.resolve(cacheRoot)}${path.sep}`))
    assert.deepEqual(requests, [
      { connectionId: 'studio-ssh', path: '/srv/report.pdf', profile: 'remote-work', suggestedName: 'report.pdf' }
    ])
  } finally {
    fs.rmSync(cacheRoot, { force: true, recursive: true })
  }
})

test('a failed gateway download leaves nothing in the cache', async () => {
  const fs = await import('node:fs')
  const os = await import('node:os')
  const path = await import('node:path')
  const { createContextFileMaterializer } = await import('./context-file-ipc')
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-context-gateway-'))

  try {
    const materialize = createContextFileMaterializer({
      cacheRoot,
      publicFetch: async () => new Response(''),
      saveGatewayFileTo: async (_request, destination) => {
        fs.writeFileSync(destination, 'partial')
        throw new Error('404: missing')
      }
    })

    await assert.rejects(
      materialize({ items: [], kind: 'remote-file', name: 'gone.pdf', remoteKind: 'gateway', source: '/srv/gone.pdf' }),
      /404/
    )
    assert.deepEqual(
      fs.readdirSync(cacheRoot, { recursive: true }).filter(entry => /gone\.pdf/.test(String(entry))),
      []
    )
  } finally {
    fs.rmSync(cacheRoot, { force: true, recursive: true })
  }
})
