import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  contextFileSourceLink,
  contextMenuModelForContextFile,
  contextMenuModelForLink,
  contextMenuTargetModels
} from './file-link-context-menu'

test('turns a local source path into a copyable file link', () => {
  assert.equal(contextFileSourceLink('/tmp/factory review.pdf'), 'file:///tmp/factory%20review.pdf')
  assert.equal(contextFileSourceLink('https://files.example/report.pdf'), 'https://files.example/report.pdf')
})

test('turns a Windows drive path into a portable file link', () => {
  assert.equal(
    contextFileSourceLink('C:\\Users\\Ahmed\\Factory Review.pdf'),
    'file:///C:/Users/Ahmed/Factory%20Review.pdf'
  )
})

test('an internal gateway descriptor carries only source identity into the privileged model', () => {
  const model = contextMenuModelForContextFile(
    JSON.stringify({
      kind: 'gateway',
      name: 'report.pdf',
      profile: 'remote-work',
      source: '/srv/reports/report.pdf'
    }),
    'darwin'
  )

  assert.equal(model?.kind, 'remote-file')
  assert.equal(model?.remoteKind, 'gateway')
  assert.equal(model?.profile, 'remote-work')
  assert.equal(model?.source, '/srv/reports/report.pdf')
  assert.equal(model?.items.some(item => item.id === 'copy-file'), true)
})

test('rejects a DOM descriptor that injects a privileged download URL', () => {
  const model = contextMenuModelForContextFile(
    JSON.stringify({
      downloadUrl: 'http://127.0.0.1/private',
      kind: 'gateway',
      name: 'report.pdf',
      profile: 'remote-work',
      source: '/srv/reports/report.pdf'
    }),
    'darwin'
  )

  assert.equal(model, null)
})

test('rejects non-file protocols in local and gateway DOM descriptors', () => {
  for (const kind of ['local', 'gateway']) {
    const model = contextMenuModelForContextFile(
      JSON.stringify({ kind, name: 'payload', source: 'javascript:alert(1)' }),
      'darwin'
    )

    assert.equal(model, null)
  }
})

test('preserves ordinary link actions when a nested element also describes a file', () => {
  const descriptorModel = contextMenuModelForContextFile(
    JSON.stringify({ kind: 'local', name: 'diagram.png', source: '/tmp/diagram.png' }),
    'darwin'
  )

  const webLinkModel = contextMenuModelForLink({ linkURL: 'https://example.com/full-report' }, 'darwin')
  const targets = contextMenuTargetModels(descriptorModel, webLinkModel)

  assert.equal(targets.contextFileModel?.kind, 'local-file')
  assert.equal(targets.ordinaryLinkModel?.kind, 'web-link')
})

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

test('non-file protocols stay link-only', () => {
  const model = contextMenuModelForLink(
    {
      linkURL: 'mailto:engineering@example.com',
      suggestedFilename: ''
    },
    'darwin'
  )

  assert.equal(model.kind, 'web-link')
  assert.deepEqual(
    new Set(model.items.map(item => item.id)),
    new Set(['open-link', 'copy-link'])
  )
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
  assert.equal(model.remoteKind, 'external')
  assert.deepEqual(
    new Set(model.items.map(item => item.id)),
    new Set(['open-file', 'copy-link', 'copy-path', 'copy-file', 'reveal-file'])
  )
  assert.equal(model.items.find(item => item.id === 'reveal-file')?.label, 'Show in File Manager')
})

test('Windows labels the reveal action for Explorer', () => {
  const model = contextMenuModelForLink(
    {
      linkURL: 'file:///C:/Users/Ahmed/review.pdf',
      suggestedFilename: 'review.pdf'
    },
    'win32'
  )

  assert.equal(model.items.find(item => item.id === 'reveal-file')?.label, 'Show in Explorer')
})
