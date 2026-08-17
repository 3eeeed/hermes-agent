import assert from 'node:assert/strict'

import { test } from 'vitest'

import { runContextFileAction } from './context-file-actions'
import type { FileLinkContextMenuModel } from './file-link-context-menu'

const remoteModel: FileLinkContextMenuModel = {
  downloadUrl: 'https://gateway.example/download?token=secret',
  items: [],
  kind: 'remote-file',
  name: 'report.pdf',
  source: '/srv/reports/report.pdf'
}

test('copy-link copies the public source link without materializing the remote file', async () => {
  const copied: string[] = []
  let materializations = 0

  await runContextFileAction('copy-link', remoteModel, {
    copyFile: async () => {},
    copyText: value => copied.push(value),
    materialize: async () => {
      materializations += 1

      return '/tmp/report.pdf'
    },
    openFile: async () => {},
    revealFile: () => {}
  })

  assert.deepEqual(copied, ['file:///srv/reports/report.pdf'])
  assert.equal(materializations, 0)
  assert.equal(copied[0]?.includes('secret'), false)
})

test('copy-path materializes the file and copies its local path', async () => {
  const copied: string[] = []
  let materializations = 0

  await runContextFileAction('copy-path', remoteModel, {
    copyFile: async () => {},
    copyText: value => copied.push(value),
    materialize: async () => {
      materializations += 1

      return '/tmp/hermes-context/report.pdf'
    },
    openFile: async () => {},
    revealFile: () => {}
  })

  assert.deepEqual(copied, ['/tmp/hermes-context/report.pdf'])
  assert.equal(materializations, 1)
})

test('copy-file materializes the file and passes it to the native file clipboard', async () => {
  const copiedFiles: string[] = []

  await runContextFileAction('copy-file', remoteModel, {
    copyFile: async filePath => {
      copiedFiles.push(filePath)
    },
    copyText: () => {},
    materialize: async () => '/tmp/hermes-context/report.pdf',
    openFile: async () => {},
    revealFile: () => {}
  })

  assert.deepEqual(copiedFiles, ['/tmp/hermes-context/report.pdf'])
})

test('open-file materializes the file before opening it', async () => {
  const openedFiles: string[] = []

  await runContextFileAction('open-file', remoteModel, {
    copyFile: async () => {},
    copyText: () => {},
    materialize: async () => '/tmp/hermes-context/report.pdf',
    openFile: async filePath => {
      openedFiles.push(filePath)
    },
    revealFile: () => {}
  })

  assert.deepEqual(openedFiles, ['/tmp/hermes-context/report.pdf'])
})

test('reveal-file materializes the file before revealing it', async () => {
  const revealedFiles: string[] = []

  await runContextFileAction('reveal-file', remoteModel, {
    copyFile: async () => {},
    copyText: () => {},
    materialize: async () => '/tmp/hermes-context/report.pdf',
    openFile: async () => {},
    revealFile: filePath => {
      revealedFiles.push(filePath)
    }
  })

  assert.deepEqual(revealedFiles, ['/tmp/hermes-context/report.pdf'])
})
