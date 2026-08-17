import assert from 'node:assert/strict'

import { test } from 'vitest'

import { contextMenuModelForLink } from './file-link-context-menu'

test('a local file link exposes native file actions on macOS', () => {
  const model = contextMenuModelForLink(
    {
      linkURL: 'file:///Users/ahmed/report.pdf',
      suggestedFilename: 'report.pdf'
    },
    'darwin'
  )

  assert.deepEqual(
    new Set(model.items.map(item => item.id)),
    new Set(['open-file', 'copy-link', 'copy-path', 'copy-file', 'reveal-file'])
  )
  assert.equal(model.items.find(item => item.id === 'reveal-file')?.label, 'Show in Finder')
})

test('a web link stays link-only', () => {
  const model = contextMenuModelForLink(
    {
      linkURL: 'https://example.com/docs'
    },
    'darwin'
  )

  assert.equal(model.kind, 'web-link')
  assert.deepEqual(
    new Set(model.items.map(item => item.id)),
    new Set(['open-link', 'copy-link'])
  )
})

test('a remote Hermes media link is treated as a file that needs materialization', () => {
  const source = 'https://files.example.com/reports/final%20report.pdf'

  const model = contextMenuModelForLink(
    {
      linkURL: `http://127.0.0.1:5174/#media:${encodeURIComponent(source)}`,
      suggestedFilename: 'final report.pdf'
    },
    'linux'
  )

  assert.equal(model.kind, 'remote-file')
  assert.equal(model.source, source)
  assert.deepEqual(
    new Set(model.items.map(item => item.id)),
    new Set(['open-file', 'copy-link', 'copy-path', 'copy-file', 'reveal-file'])
  )
  assert.equal(model.items.find(item => item.id === 'reveal-file')?.label, 'Show in File Manager')
})
