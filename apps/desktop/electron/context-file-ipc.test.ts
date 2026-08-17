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

const descriptor = JSON.stringify({ name: 'report.pdf', remote: false, source: '/srv/report.pdf' })

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
