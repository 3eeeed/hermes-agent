/**
 * "Move here" — taking over a chat another device owns, from the error card.
 *
 * The composer clears the draft and its attachment chips BEFORE the send is
 * dispatched (use-composer-submit), so by the time the gateway refuses with
 * SESSION_NOT_OWNED the user's message exists nowhere but here. That makes this
 * store the preservation mechanism, and gives it three duties:
 *
 *  - hold the refused send (raw text AND the attachment objects already staged
 *    into the session workspace) until the user decides,
 *  - claim with the EXACT owner the refusal named, so a stale click loses
 *    safely instead of stealing,
 *  - resend once — and on any failure keep everything, so nothing the user
 *    typed is lost and no new chat is silently minted in its place.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from './composer'
import {
  $pendingSessionMoves,
  armRefusedSendFromTurn,
  canMoveSessionHere,
  clearSessionMoveDeps,
  completeInFlightSend,
  forgetInFlightSend,
  forgetPendingSessionMove,
  moveSessionHere,
  noteSessionMoveErrorMessage,
  pendingSessionMove,
  pendingSessionMoves,
  rememberInFlightSend,
  rememberRefusedSend,
  type SessionMoveDeps,
  setSessionMoveDeps
} from './session-move'

const OWNERSHIP = {
  ageSeconds: 3540,
  busy: false,
  generation: 4,
  handoffSupported: true,
  leaseId: 'lease-macbook',
  sessionId: '20260919_162101_617cd0',
  surface: 'desktop'
}

const IMAGE: ComposerAttachment = {
  id: 'img-1',
  kind: 'image',
  label: 'screenshot.png',
  path: '/tmp/screenshot.png',
  refText: '@file:.hermes/workspace/screenshot.png'
}

/** A submit payload as the pipeline built it: every field the gateway digests,
 *  and no session target — see the payload suite at the bottom of this file. */
const PAYLOAD = {
  surface: 'hud',
  text: '@file:.hermes/workspace/screenshot.png\n\nlook at this',
  title_preview: 'Q3 report'
}

function refusedSend(overrides: Record<string, unknown> = {}) {
  rememberRefusedSend({
    attachments: [IMAGE],
    ownership: OWNERSHIP,
    runtimeId: 'imac-runtime',
    storedSessionId: '20260919_162101_617cd0',
    text: 'look at this',
    ...overrides
  })
}

/** A wiring as a hook would build it — not installed yet, so a test can control
 *  the install/clear order the way two overlapping hooks would. */
function wiring(overrides: Partial<SessionMoveDeps> = {}): SessionMoveDeps & {
  claim: ReturnType<typeof vi.fn>
  resend: ReturnType<typeof vi.fn>
} {
  return {
    claim: vi.fn(async () => ({ generation: 5, status: 'claimed' })),
    resend: vi.fn(async () => true),
    ...overrides
  } as never
}

function deps(overrides: Partial<SessionMoveDeps> = {}): {
  claim: ReturnType<typeof vi.fn>
  resend: ReturnType<typeof vi.fn>
} {
  const wired = wiring(overrides)

  setSessionMoveDeps(wired)

  return wired
}

beforeEach(() => {
  $pendingSessionMoves.set({})
  clearSessionMoveDeps()
  // The in-flight hold is module state keyed by runtime id — a leak from one test
  // would let the next one arm a send it never made.
  forgetInFlightSend('imac-runtime')
})

describe('the refused send is preserved', () => {
  it('keeps the raw text and the staged attachments, keyed by stored session', () => {
    refusedSend()

    const pending = pendingSessionMove('20260919_162101_617cd0')

    expect(pending?.text).toBe('look at this')
    expect(pending?.attachments).toEqual([IMAGE])
    expect(pending?.ownership?.leaseId).toBe('lease-macbook')
  })

  it('has nothing pending for a chat that was never refused', () => {
    expect(pendingSessionMove('another-chat')).toBeNull()
    expect(pendingSessionMove(null)).toBeNull()
  })

  it('keeps send B when message.complete retires the older send A', () => {
    rememberInFlightSend('imac-runtime', {
      attachments: [],
      ownership: null,
      runtimeId: 'imac-runtime',
      storedSessionId: '20260919_162101_617cd0',
      text: 'A'
    })
    rememberInFlightSend('imac-runtime', {
      attachments: [],
      ownership: null,
      runtimeId: 'imac-runtime',
      storedSessionId: '20260919_162101_617cd0',
      text: 'B'
    })

    completeInFlightSend('imac-runtime')

    expect(armRefusedSendFromTurn('imac-runtime', OWNERSHIP)?.text).toBe('B')
  })
})

describe('moveSessionHere', () => {
  it('claims the exact owner the refusal named, then resends once', async () => {
    const wired = deps()
    refusedSend()

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: true })

    expect(wired.claim).toHaveBeenCalledWith({
      generation: 4,
      leaseId: 'lease-macbook',
      runtimeId: 'imac-runtime',
      storedSessionId: '20260919_162101_617cd0'
    })
    expect(wired.resend).toHaveBeenCalledTimes(1)
    // The resend carries the preserved payload, and names the STORED session so
    // the submit pipeline resumes it instead of creating a contextless chat.
    expect(wired.resend.mock.calls[0][0]).toMatchObject({
      attachments: [IMAGE],
      storedSessionId: '20260919_162101_617cd0',
      text: 'look at this'
    })
  })

  it('claims before it resends — never the other way round', async () => {
    const order: string[] = []

    const wired = deps({
      claim: vi.fn(async () => {
        order.push('claim')

        return { generation: 5, status: 'claimed' }
      }),
      resend: vi.fn(async () => {
        order.push('resend')

        return true
      })
    })

    refusedSend()

    await moveSessionHere('20260919_162101_617cd0')

    expect(order).toEqual(['claim', 'resend'])
    expect(wired.claim).toHaveBeenCalledOnce()
  })

  it('forgets the pending send only after it landed', async () => {
    deps()
    refusedSend()

    await moveSessionHere('20260919_162101_617cd0')

    expect(pendingSessionMove('20260919_162101_617cd0')).toBeNull()
  })

  it('submits exactly once when the button is double-clicked', async () => {
    let release: (() => void) | null = null

    const wired = deps({
      claim: vi.fn(
        () =>
          new Promise<{ generation: number; status: string }>(resolve => {
            release = () => resolve({ generation: 5, status: 'claimed' })
          })
      )
    })

    refusedSend()

    const first = moveSessionHere('20260919_162101_617cd0')
    const second = moveSessionHere('20260919_162101_617cd0')

    release!()

    expect(await first).toEqual({ ok: true })
    // The second click is dropped, not queued: a duplicate resend would post
    // the user's message twice into the chat it just took over.
    expect(await second).toEqual({ ok: false, reason: 'in_flight' })
    expect(wired.claim).toHaveBeenCalledOnce()
    expect(wired.resend).toHaveBeenCalledOnce()
  })
})

describe('a failed move loses nothing', () => {
  it('keeps the draft and attachments when the owner is mid-reply', async () => {
    const wired = deps({
      claim: vi.fn(async () => {
        throw Object.assign(new Error('That chat is mid-reply on the other device.'), {
          data: { reason: 'SESSION_HANDOFF_BUSY' }
        })
      })
    })

    refusedSend()

    const result = await moveSessionHere('20260919_162101_617cd0')

    expect(result.ok).toBe(false)
    expect(result.reason).toBe('SESSION_HANDOFF_BUSY')
    expect(result.message).toMatch(/mid-reply/)
    expect(wired.resend).not.toHaveBeenCalled()
    expect(pendingSessionMove('20260919_162101_617cd0')?.attachments).toEqual([IMAGE])
  })

  it('keeps everything when the claim is stale, and refreshes the owner to claim again', async () => {
    deps({
      claim: vi.fn(async () => {
        throw Object.assign(new Error('This chat moved to another window.'), {
          data: {
            ownership: {
              age_seconds: 2,
              busy: false,
              generation: 9,
              handoff_supported: true,
              lease_id: 'lease-third-device',
              session_id: '20260919_162101_617cd0',
              surface: 'desktop'
            },
            reason: 'SESSION_HANDOFF_STALE'
          }
        })
      })
    })
    refusedSend()

    expect((await moveSessionHere('20260919_162101_617cd0')).reason).toBe('SESSION_HANDOFF_STALE')

    const pending = pendingSessionMove('20260919_162101_617cd0')

    expect(pending?.text).toBe('look at this')
    // Re-armed against whoever owns it NOW, so a second click is a valid
    // compare-and-swap rather than a replay that can only fail again.
    expect(pending?.ownership).toMatchObject({ generation: 9, leaseId: 'lease-third-device' })
  })

  it('keeps the draft when the claim succeeded but the resend failed', async () => {
    const wired = deps({ resend: vi.fn(async () => false) })
    refusedSend()

    const result = await moveSessionHere('20260919_162101_617cd0')

    expect(result).toEqual({ ok: false, reason: 'resend_failed' })
    expect(pendingSessionMove('20260919_162101_617cd0')?.text).toBe('look at this')
    expect(wired.resend).toHaveBeenCalledOnce()
  })

  it('refuses to move a chat whose owner cannot be fenced', async () => {
    const wired = deps()
    refusedSend({ ownership: { ...OWNERSHIP, handoffSupported: false } })

    const result = await moveSessionHere('20260919_162101_617cd0')

    expect(result).toEqual({ ok: false, reason: 'unsupported' })
    expect(wired.claim).not.toHaveBeenCalled()
    expect(pendingSessionMove('20260919_162101_617cd0')).not.toBeNull()
  })

  it('does nothing for a chat with no preserved send', async () => {
    const wired = deps()

    expect(await moveSessionHere('nothing-here')).toEqual({ ok: false, reason: 'nothing_pending' })
    expect(wired.claim).not.toHaveBeenCalled()
  })
})

describe('forgetPendingSessionMove', () => {
  it('drops one chat and leaves the others', () => {
    refusedSend()
    refusedSend({ storedSessionId: 'other-chat' })

    forgetPendingSessionMove('other-chat')

    expect(pendingSessionMove('other-chat')).toBeNull()
    expect(pendingSessionMove('20260919_162101_617cd0')).not.toBeNull()
  })
})

describe('more than one refused send per chat', () => {
  // A chat owned by another device refuses EVERY send, so the user typing twice
  // is the ordinary case, not a corner one. Each refusal is a distinct message
  // with its own attachments, and none of them may be silently overwritten by
  // the next or deleted by an older move completing.
  it('keeps every refused send, oldest first', () => {
    deps()
    refusedSend({ text: 'first thing' })
    refusedSend({ text: 'second thing', attachments: [] })

    expect(pendingSessionMoves('20260919_162101_617cd0').map(p => p.text)).toEqual([
      'first thing',
      'second thing'
    ])
  })

  it('gives every refused send its own immutable identity', () => {
    deps()
    refusedSend({ text: 'first thing' })
    refusedSend({ text: 'second thing' })

    const [first, second] = pendingSessionMoves('20260919_162101_617cd0')

    expect(first.attemptId).toBeTruthy()
    expect(second.attemptId).toBeTruthy()
    expect(first.attemptId).not.toBe(second.attemptId)
    // The send id travels with the attempt, so a retry of THAT message is
    // recognisable by the gateway (see tests/tui_gateway/test_prompt_submit_idempotency.py).
    expect(first.sendId).toBeTruthy()
    expect(first.sendId).not.toBe(second.sendId)
  })

  it('resends every preserved message once, in the order they were typed', async () => {
    const wired = deps()
    refusedSend({ text: 'first thing' })
    refusedSend({ text: 'second thing' })

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: true })

    expect(wired.claim).toHaveBeenCalledOnce()
    expect(wired.resend.mock.calls.map(call => call[0].text)).toEqual(['first thing', 'second thing'])
    expect(pendingSessionMoves('20260919_162101_617cd0')).toEqual([])
  })

  it('a completing move deletes only the attempts it actually sent', async () => {
    // The race: a move is in flight for message A when the user's NEXT send is
    // refused too. Completing by clearing the whole session would swallow B --
    // a message the move never saw, and nobody would ever resend.
    let releaseResend: (() => void) | null = null

    const wired = deps({
      resend: vi.fn(
        () =>
          new Promise<boolean>(resolve => {
            releaseResend = () => resolve(true)
          })
      )
    })

    refusedSend({ text: 'first thing' })

    const moving = moveSessionHere('20260919_162101_617cd0')

    await Promise.resolve()
    refusedSend({ text: 'typed while the move was in flight' })
    releaseResend!()

    expect(await moving).toEqual({ ok: true })
    expect(wired.resend).toHaveBeenCalledOnce()
    expect(pendingSessionMoves('20260919_162101_617cd0').map(p => p.text)).toEqual([
      'typed while the move was in flight'
    ])
  })

  it('a failed resend keeps that attempt and everything after it', async () => {
    const wired = deps({
      resend: vi.fn(async (pending: { text: string }) => pending.text !== 'second thing')
    })

    refusedSend({ text: 'first thing' })
    refusedSend({ text: 'second thing' })
    refusedSend({ text: 'third thing' })

    expect((await moveSessionHere('20260919_162101_617cd0')).reason).toBe('resend_failed')

    expect(wired.resend).toHaveBeenCalledTimes(2)
    expect(pendingSessionMoves('20260919_162101_617cd0').map(p => p.text)).toEqual([
      'second thing',
      'third thing'
    ])
  })

  it('reuses one send id across retries of the same message', async () => {
    // Response loss after acceptance: the resend is retried, and the gateway
    // recognises the id and answers with the acceptance it already gave.
    let attempt = 0

    const wired = deps({
      resend: vi.fn(async () => {
        attempt += 1

        return attempt > 1
      })
    })

    refusedSend({ text: 'first thing' })
    const sendId = pendingSessionMoves('20260919_162101_617cd0')[0].sendId

    expect((await moveSessionHere('20260919_162101_617cd0')).ok).toBe(false)
    expect((await moveSessionHere('20260919_162101_617cd0')).ok).toBe(true)

    expect(wired.resend.mock.calls.map(call => call[0].sendId)).toEqual([sendId, sendId])
    // The first claim already succeeded. A lost resend response must retry the
    // idempotent send directly after reconnect, not claim the stale pre-move
    // owner again (which otherwise needs extra clicks and can never match).
    expect(wired.claim).toHaveBeenCalledOnce()
  })

  it('claims against the FRESHEST owner it was refused with', async () => {
    const wired = deps()
    refusedSend({ text: 'first thing' })
    refusedSend({
      text: 'second thing',
      ownership: { ...OWNERSHIP, generation: 7, leaseId: 'lease-newer' }
    })

    await moveSessionHere('20260919_162101_617cd0')

    expect(wired.claim).toHaveBeenCalledWith(
      expect.objectContaining({ generation: 7, leaseId: 'lease-newer' })
    )
  })
})

describe('ownership that does not describe this chat', () => {
  it('refuses to claim when the owner block names a different session', async () => {
    const wired = deps()
    refusedSend({ ownership: { ...OWNERSHIP, sessionId: 'some-other-chat' } })

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: false, reason: 'unsupported' })
    expect(wired.claim).not.toHaveBeenCalled()
  })

  it('refuses a generation that is not a finite positive integer', async () => {
    const wired = deps()

    for (const generation of [0, -3, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      $pendingSessionMoves.set({})
      refusedSend({ ownership: { ...OWNERSHIP, generation } })

      expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: false, reason: 'unsupported' })
    }

    expect(wired.claim).not.toHaveBeenCalled()
  })
})

// Two usePromptActions trees overlap routinely — a profile swap mounts the new
// one before React unmounts the old, and a second window mounts its own. The
// wiring is module-global, so "unwire on unmount" is only safe if each hook can
// remove EXACTLY what it installed: an older hook tearing down the newer one's
// wiring leaves a mounted app whose "Move here" answers nothing_pending.
describe('overlapping wirings', () => {
  it('an older hook unwiring itself leaves the newer wiring live', async () => {
    const older = wiring()
    const newer = wiring()

    setSessionMoveDeps(older)
    setSessionMoveDeps(newer)
    clearSessionMoveDeps(older)

    refusedSend()

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: true })
    expect(newer.claim).toHaveBeenCalledTimes(1)
    expect(older.claim).not.toHaveBeenCalled()
  })

  it('falls back to the newest still-live wiring when the current one unwires', async () => {
    const older = wiring()
    const newer = wiring()

    setSessionMoveDeps(older)
    setSessionMoveDeps(newer)
    clearSessionMoveDeps(newer)

    refusedSend()

    // The older hook is still mounted and still holds a usable gateway client.
    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: true })
    expect(older.claim).toHaveBeenCalledTimes(1)
    expect(newer.claim).not.toHaveBeenCalled()
  })

  it('leaves nothing wired once every owner has unwired, in either order', async () => {
    const older = wiring()
    const newer = wiring()

    setSessionMoveDeps(older)
    setSessionMoveDeps(newer)
    clearSessionMoveDeps(older)
    clearSessionMoveDeps(newer)

    refusedSend()

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: false, reason: 'nothing_pending' })
    expect(older.claim).not.toHaveBeenCalled()
    expect(newer.claim).not.toHaveBeenCalled()
  })

  it('unwires the ordinary single mount, and tolerates a repeated clear', async () => {
    const only = wiring()

    setSessionMoveDeps(only)
    clearSessionMoveDeps(only)
    clearSessionMoveDeps(only)

    refusedSend()

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: false, reason: 'nothing_pending' })
    expect(only.claim).not.toHaveBeenCalled()
  })

  it('re-installing the same wiring object does not queue it twice', async () => {
    const only = wiring()

    setSessionMoveDeps(only)
    setSessionMoveDeps(only)
    clearSessionMoveDeps(only)

    refusedSend()

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: false, reason: 'nothing_pending' })
  })
})

// The gateway digests a submit by everything it MEANS — text, surface,
// title_preview, display kind, the queued flag — and refuses a `client_send_id`
// that comes back carrying a different digest (same key, different payload =
// conflict). A resend that REBUILDS those fields from live UI state therefore
// cannot land: the HUD has closed, the composer has emptied, and the payload no
// longer matches the one the id was minted for. So the payload travels with the
// attempt, and only the session target is ever retranslated.
describe('the payload the refusal was built from', () => {
  it('hands the resend the exact payload, not one rebuilt at click time', async () => {
    const wired = deps()
    refusedSend({ payload: PAYLOAD })

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: true })

    expect(wired.resend.mock.calls[0][0].payload).toEqual(PAYLOAD)
  })

  it('names no session in the payload — the target is translated, the meaning is not', () => {
    refusedSend({ payload: PAYLOAD })

    const pending = pendingSessionMove('20260919_162101_617cd0')

    // session_id is re-minted by every reconnect and is explicitly excluded from
    // the server's digest, so it is carried as the separate stored/runtime ids.
    expect(pending?.payload).not.toHaveProperty('session_id')
    expect(pending?.storedSessionId).toBe('20260919_162101_617cd0')
    expect(pending?.runtimeId).toBe('imac-runtime')
  })

  it('keeps the payload when a stale claim re-arms the attempt against a fresh owner', async () => {
    deps({
      claim: vi.fn(async () => {
        throw Object.assign(new Error('This chat moved to another window.'), {
          data: {
            ownership: {
              age_seconds: 2,
              busy: false,
              generation: 9,
              handoff_supported: true,
              lease_id: 'lease-third-device',
              session_id: '20260919_162101_617cd0',
              surface: 'desktop'
            },
            reason: 'SESSION_HANDOFF_STALE'
          }
        })
      })
    })
    refusedSend({ payload: PAYLOAD })

    await moveSessionHere('20260919_162101_617cd0')

    // Re-arming rewrites the precondition, never the message.
    expect(pendingSessionMove('20260919_162101_617cd0')?.payload).toEqual(PAYLOAD)
  })

  it('updates the attempt in place when the SAME attempt is refused again', () => {
    refusedSend({ payload: PAYLOAD, text: 'look at this' })

    const first = pendingSessionMove('20260919_162101_617cd0')!

    // What a re-refused resend records: same identities, fresh owner block.
    rememberRefusedSend({
      ...first,
      ownership: { ...OWNERSHIP, generation: 11, leaseId: 'lease-newer' }
    })

    const queue = pendingSessionMoves('20260919_162101_617cd0')

    expect(queue).toHaveLength(1)
    expect(queue[0].attemptId).toBe(first.attemptId)
    expect(queue[0].sendId).toBe(first.sendId)
    expect(queue[0].payload).toEqual(PAYLOAD)
    expect(queue[0].ownership).toMatchObject({ generation: 11, leaseId: 'lease-newer' })
  })
})

// The refusal leaves two rows on screen: the user's own optimistic bubble and the
// ownership card. Both belong to THIS attempt, so the move that finishes the send can
// update the one and remove the other by exact id — instead of appending a second copy
// of the user's message and sweeping every ownership card in the transcript.
describe('the rows a refusal left on screen travel with the attempt', () => {
  it('carries both identities through to the resend', async () => {
    const wired = deps()
    refusedSend({ errorMessageId: 'assistant-error-1', optimisticMessageId: 'user-1' })

    expect(await moveSessionHere('20260919_162101_617cd0')).toEqual({ ok: true })

    expect(wired.resend.mock.calls[0][0]).toMatchObject({
      errorMessageId: 'assistant-error-1',
      optimisticMessageId: 'user-1'
    })
  })

  it('keeps them when a stale claim re-arms the attempt against a fresh owner', async () => {
    deps({
      claim: vi.fn(async () => {
        throw Object.assign(new Error('This chat moved to another window.'), {
          data: {
            ownership: {
              age_seconds: 2,
              busy: false,
              generation: 9,
              handoff_supported: true,
              lease_id: 'lease-third-device',
              session_id: '20260919_162101_617cd0',
              surface: 'desktop'
            },
            reason: 'SESSION_HANDOFF_STALE'
          }
        })
      })
    })
    refusedSend({ errorMessageId: 'assistant-error-1', optimisticMessageId: 'user-1' })

    await moveSessionHere('20260919_162101_617cd0')

    // Re-arming rewrites the precondition, never which rows this attempt owns — the
    // second click must still update that bubble and remove that card.
    expect(pendingSessionMove('20260919_162101_617cd0')).toMatchObject({
      errorMessageId: 'assistant-error-1',
      optimisticMessageId: 'user-1'
    })
  })

  // The asynchronous branch cannot record the card id at refusal time: the stream
  // dispatcher paints it afterwards, adopting the live assistant row or minting one.
  describe('noteSessionMoveErrorMessage', () => {
    it('binds the card to the named attempt and leaves its siblings alone', () => {
      refusedSend({ text: 'first thing' })
      refusedSend({ text: 'second thing' })

      const [first, second] = pendingSessionMoves('20260919_162101_617cd0')

      noteSessionMoveErrorMessage('20260919_162101_617cd0', second.attemptId, 'assistant-error-2')

      const [afterFirst, afterSecond] = pendingSessionMoves('20260919_162101_617cd0')

      expect(afterSecond.errorMessageId).toBe('assistant-error-2')
      expect(afterFirst.errorMessageId).toBeUndefined()
      expect(afterFirst.attemptId).toBe(first.attemptId)
    })

    it('is a no-op for an attempt that already landed', () => {
      refusedSend()

      const { attemptId } = pendingSessionMove('20260919_162101_617cd0')!

      forgetPendingSessionMove('20260919_162101_617cd0')
      noteSessionMoveErrorMessage('20260919_162101_617cd0', attemptId, 'assistant-error-1')

      // A move that finished first must not be resurrected as an empty queue entry.
      expect($pendingSessionMoves.get()).toEqual({})
    })
  })

  describe('armRefusedSendFromTurn', () => {
    it('returns the armed attempt so the dispatcher can name the card it is about to paint', () => {
      rememberInFlightSend('imac-runtime', {
        attachments: [IMAGE],
        optimisticMessageId: 'user-1',
        ownership: null,
        runtimeId: 'imac-runtime',
        storedSessionId: '20260919_162101_617cd0',
        text: 'look at this'
      })

      const armed = armRefusedSendFromTurn('imac-runtime', OWNERSHIP)

      expect(armed).toMatchObject({
        optimisticMessageId: 'user-1',
        storedSessionId: '20260919_162101_617cd0',
        text: 'look at this'
      })
      expect(armed!.attemptId).toBe(pendingSessionMove('20260919_162101_617cd0')!.attemptId)
    })

    it('returns null with nothing held, rather than inventing an attempt', () => {
      expect(armRefusedSendFromTurn('imac-runtime', OWNERSHIP)).toBeNull()
      expect($pendingSessionMoves.get()).toEqual({})
    })
  })
})

describe('canMoveSessionHere', () => {
  it('is false with nothing waiting, and true once a send is refused', () => {
    expect(canMoveSessionHere('20260919_162101_617cd0')).toBe(false)

    refusedSend()

    expect(canMoveSessionHere('20260919_162101_617cd0')).toBe(true)
  })

  it('is false when the owner block cannot be compare-and-swapped against', () => {
    for (const ownership of [
      null,
      { ...OWNERSHIP, handoffSupported: false },
      { ...OWNERSHIP, sessionId: 'some-other-chat' },
      { ...OWNERSHIP, generation: 0 },
      { ...OWNERSHIP, generation: 2.5 },
      { ...OWNERSHIP, leaseId: '' }
    ]) {
      $pendingSessionMoves.set({})
      refusedSend({ ownership })

      expect(canMoveSessionHere('20260919_162101_617cd0')).toBe(false)
    }
  })

  it('tolerates an owner block that omits the session id (older backend)', () => {
    refusedSend({ ownership: { ...OWNERSHIP, sessionId: '' } })

    expect(canMoveSessionHere('20260919_162101_617cd0')).toBe(true)
  })
})
