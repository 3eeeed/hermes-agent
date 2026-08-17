import assert from 'node:assert/strict'

import { test } from 'vitest'

import { contextFileActionErrorMessage } from './context-file-actions'

test('summarizes a failed file action without leaking internal detail', () => {
  const message = contextFileActionErrorMessage(
    'copy-file',
    new Error('connect ECONNREFUSED 10.1.2.3:8443 while fetching https://gw/api/files/download?token=secret')
  )

  assert.equal(message.includes('secret'), false)
  assert.equal(message.includes('10.1.2.3'), false)
  assert.match(message, /Copy File/)
})

test('explains a blocked private network destination in plain terms', () => {
  const message = contextFileActionErrorMessage(
    'open-file',
    new Error('File download URL must resolve only to public network addresses')
  )

  assert.match(message, /Open File/)
  assert.match(message, /not on a public network|blocked/i)
})
