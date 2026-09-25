import { atom } from 'nanostores'

import { keyedTimeouts } from '@/lib/keyed-timeouts'

/** Where a Delegate turn stands, as announced by the backend's `status.update` kind=delegate. */
export type DelegatePhase = 'planning' | 'worker' | 'review' | 'done' | 'interrupted'

export interface DelegateProgress {
  attempt: number
  changedCount: number
  lane: string
  maxAttempts: number
  phase: DelegatePhase
  updatedAt: number
}

export const $delegateBySession = atom<Record<string, DelegateProgress>>({})

const PHASES = new Set<DelegatePhase>(['planning', 'worker', 'review', 'done', 'interrupted'])
// A finished turn disappears on its own; an interrupted one stays until the next turn or dismiss,
// because the user needs to know the worker's edits may be partial.
const DONE_LINGER_MS = 4_000
const clearTimers = keyedTimeouts()

const num = (value: unknown, fallback = 0) => (typeof value === 'number' && Number.isFinite(value) ? value : fallback)

export function applyDelegateStatus(sid: string, payload: Record<string, unknown> | undefined) {
  const phase = payload?.phase

  if (!sid || typeof phase !== 'string' || !PHASES.has(phase as DelegatePhase)) {
    return
  }

  const progress: DelegateProgress = {
    attempt: num(payload?.attempt),
    changedCount: num(payload?.changed_count),
    lane: typeof payload?.lane === 'string' ? payload.lane : '',
    maxAttempts: num(payload?.max_attempts, 3),
    phase: phase as DelegatePhase,
    updatedAt: Date.now()
  }

  clearTimers.cancel(sid)
  $delegateBySession.set({ ...$delegateBySession.get(), [sid]: progress })

  if (progress.phase === 'done') {
    clearTimers.schedule(sid, DONE_LINGER_MS, () => clearDelegateStatus(sid))
  }
}

export function clearDelegateStatus(sid: string) {
  clearTimers.cancel(sid)

  const map = $delegateBySession.get()

  if (!(sid in map)) {
    return
  }

  const { [sid]: _drop, ...rest } = map
  $delegateBySession.set(rest)
}
