import { afterEach, describe, expect, it, vi } from 'vitest'

import { $delegateBySession, applyDelegateStatus, clearDelegateStatus } from './delegate-status'

describe('delegate status', () => {
  afterEach(() => {
    clearDelegateStatus('s1')
    vi.useRealTimers()
  })

  it('tracks the phase the backend announces and ignores unknown payloads', () => {
    applyDelegateStatus('s1', { phase: 'worker', attempt: 2, max_attempts: 3, lane: 'code', changed_count: 1 })
    expect($delegateBySession.get().s1).toMatchObject({ phase: 'worker', attempt: 2, maxAttempts: 3, lane: 'code' })

    applyDelegateStatus('s1', { phase: 'bogus' })
    expect($delegateBySession.get().s1?.phase).toBe('worker')
  })

  it('a finished turn clears itself; an interrupted one stays until dismissed', () => {
    vi.useFakeTimers()
    applyDelegateStatus('s1', { phase: 'done' })
    vi.advanceTimersByTime(5_000)
    expect($delegateBySession.get().s1).toBeUndefined()

    applyDelegateStatus('s1', { phase: 'interrupted' })
    vi.advanceTimersByTime(60_000)
    expect($delegateBySession.get().s1?.phase).toBe('interrupted')
  })
})
