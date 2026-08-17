import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createContextMenuSequencer } from './context-menu-sequencer'

test('drops a stale context-menu build when a newer right-click already resolved', async () => {
  const shown: string[] = []
  const sequencer = createContextMenuSequencer()

  const slow = sequencer.run(async isCurrent => {
    await new Promise(resolve => setTimeout(resolve, 20))

    if (isCurrent()) {
      shown.push('slow')
    }
  })

  const fast = sequencer.run(async isCurrent => {
    if (isCurrent()) {
      shown.push('fast')
    }
  })

  await Promise.all([slow, fast])

  assert.deepEqual(shown, ['fast'])
})

test('shows the menu when no newer right-click arrived', async () => {
  const shown: string[] = []
  const sequencer = createContextMenuSequencer()

  await sequencer.run(async isCurrent => {
    if (isCurrent()) {
      shown.push('only')
    }
  })

  assert.deepEqual(shown, ['only'])
})
