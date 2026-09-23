import { JsonRpcGatewayError } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import { $composerAttachments, type ComposerAttachment } from '@/store/composer'
import { $hudMode } from '@/store/hud'
import { clearNotifications } from '@/store/notifications'
import {
  $connection
} from '@/store/session'
import {
  $pendingSessionMoves,
  armRefusedSendFromTurn,
  canMoveSessionHere,
  forgetInFlightSend,
  moveSessionHere,
  rememberRefusedSend
} from '@/store/session-move'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'
import { clearStoredJournal } from './prepared-submissions-test-utils'
import { clearSingleFlightSessionResumeState } from './single-flight-resume'

// Suites in this file reuse the same stored-id constants. The module-level single-flight resume map
// (and drift-recovery cache) would otherwise leak a never-settling in-flight promise from one test into the next.
beforeEach(() => {
  clearSingleFlightSessionResumeState()
  clearStoredJournal()
  // Queue mutations build on the persisted map, not the atom — a queue an
  // earlier test left in storage would otherwise sit ahead of this test's send.
  window.localStorage.removeItem('hermes.desktop.composerQueue.v1')
  vi.mocked(getLatestSessionMessages).mockReset()
  vi.mocked(getLatestSessionMessages).mockImplementation(async () => ({ messages: [], session_id: 'session' }))
})

vi.mock('@/hermes', () => ({
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: 'session' })),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS: 1_800_000,
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

// Every typed command also fires this (fire-and-forget); these tests assert the command's own traffic.
const SLASH_METRIC = 'shared_metrics.slash_command'

// Upstream's admission contract: an accepted submit echoes the send's own submission id.
function submitReceipt(params: unknown) {
  return { status: 'started', submission_id: (params as { submission_id?: string } | undefined)?.submission_id } as never
}

describe('usePromptActions live-owner refusal: Move here (#106217)', () => {
  afterEach(() => {
    cleanup()
    clearNotifications()
    $hudMode.set(false)
    $composerAttachments.set([])
  })

  // The composer clears the draft and its attachment chips BEFORE dispatching
  // the send, so a refusal that does not preserve the payload here loses the
  // user's message outright. These pin that the refusal arms a MOVE instead:
  // the owner to compare-and-swap against, plus everything needed to finish
  // the send once the chat is ours.
  const OWNERSHIP = {
    age_seconds: 3540,
    busy: false,
    generation: 4,
    handoff_supported: true,
    lease_id: 'lease-macbook',
    session_id: 'stored-1',
    surface: 'desktop'
  }

  const refuseWithOwnership = () =>
    vi.fn(async (method: string, params?: unknown) => {
      if (method === 'prompt.submit') {
        throw new JsonRpcGatewayError('Session stored-1 already has a live owner (desktop, lease age 59m).', {
          code: 4090,
          data: { ownership: OWNERSHIP, reason: 'SESSION_NOT_OWNED' }
        })
      }

      return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
    })

  it('preserves the refused send so the card can move the chat here', async () => {
    $pendingSessionMoves.set({})

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        refreshSessions={async () => undefined}
        requestGateway={refuseWithOwnership()}
      />
    )

    expect(await handle!.submitText('move this one over')).toBe(false)

    const pending = Object.values($pendingSessionMoves.get()).flat()

    expect(pending).toHaveLength(1)
    expect(pending[0].text).toBe('move this one over')
    // Immutable identities: one for compare-and-delete, one for server-side dedup.
    expect(pending[0].attemptId).toBeTruthy()
    expect(pending[0].sendId).toBeTruthy()
    // The exact owner the gateway named — the claim is a compare-and-swap, so a
    // guessed lease id or generation could only fail or, worse, steal.
    expect(pending[0].ownership).toMatchObject({ generation: 4, handoffSupported: true, leaseId: 'lease-macbook' })
  })

  it('carries the owner onto the error bubble so the card can offer the move', async () => {
    $pendingSessionMoves.set({})

    let latest: Record<string, unknown> | undefined
    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        onSeedState={next => {
          latest = next
        }}
        refreshSessions={async () => undefined}
        requestGateway={refuseWithOwnership()}
      />
    )

    await handle!.submitText('move this one over')

    const bubble = (latest?.messages as { errorSurface?: { ownership?: unknown } }[]).at(-1)

    expect(bubble?.errorSurface?.ownership).toMatchObject({ generation: 4, leaseId: 'lease-macbook' })
  })

  it('moving the chat claims it, then sends the preserved message exactly once', async () => {
    $pendingSessionMoves.set({})

    const calls: string[] = []

    const requestGateway = vi.fn(async (method: string, params?: unknown) => {
      calls.push(method)

      if (method === 'prompt.submit') {
        // Refused until the chat is ours; the move's resend then lands.
        if (!calls.includes('session.handoff.claim')) {
          throw new JsonRpcGatewayError('Session stored-1 already has a live owner (desktop, lease age 59m).', {
            code: 4090,
            data: { ownership: OWNERSHIP, reason: 'SESSION_NOT_OWNED' }
          })
        }

        return submitReceipt(params)
      }

      if (method === 'session.handoff.claim') {
        // Echoed verbatim from the refusal — anything else is not a fence.
        expect(params).toMatchObject({ expected_generation: 4, expected_lease_id: 'lease-macbook' })

        return { generation: 5, status: 'claimed' } as never
      }

      return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )

    expect(await handle!.submitText('move this one over')).toBe(false)

    const storedSessionId = Object.keys($pendingSessionMoves.get())[0]

    // The owner block must name the chat being moved; the harness mints its own
    // stored id, so point the preserved attempt's precondition at it.
    for (const attempt of $pendingSessionMoves.get()[storedSessionId]) {
      rememberRefusedSend({ ...attempt, ownership: { ...attempt.ownership!, sessionId: storedSessionId } })
    }

    // The card lives inside the mounted app, so the move runs against the LIVE
    // wiring; unmounting first would (correctly) unwire it.
    const moved = await moveSessionHere(storedSessionId)

    expect(moved).toEqual({ ok: true })
    expect(calls.filter(method => method === 'session.handoff.claim')).toHaveLength(1)
    expect(calls.filter(method => method === 'prompt.submit')).toHaveLength(2)
    // Landed, so nothing is held back for a second click.
    expect($pendingSessionMoves.get()).toEqual({})
  })

  // The gateway keys a retry on `client_send_id` and binds that id to a DIGEST of
  // everything the submit means — text, surface, title_preview, display kind, the
  // queued flag. A resend that rebuilds those fields from live UI state collides
  // with its own id (same key, different digest = conflict), and the preserved
  // message becomes permanently unsendable. These pin the replay to the payload
  // the refusal was actually built from.
  describe('the replay reproduces the refused payload exactly', () => {
    const REPORT: ComposerAttachment = {
      id: 'att-q3',
      kind: 'file',
      label: 'q3.pdf',
      path: '/tmp/q3.pdf',
      refText: '@file:/tmp/q3.pdf',
      titlePreview: 'Q3 report'
    }

    /** Refuse until the chat is claimed, then accept. Records every submit. */
    function movableGateway(submitted: Record<string, unknown>[]) {
      const calls: string[] = []

      return {
        calls,
        requestGateway: vi.fn(async (method: string, params?: Record<string, unknown>) => {
          calls.push(method)

          if (method === 'file.attach') {
            return { attached: true, ref_text: '@file:.hermes/workspace/q3.pdf' } as never
          }

          if (method === 'session.handoff.claim') {
            return { generation: 5, status: 'claimed' } as never
          }

          if (method === 'prompt.submit') {
            submitted.push({ ...params })

            if (!calls.includes('session.handoff.claim')) {
              throw new JsonRpcGatewayError('Session stored-1 already has a live owner (desktop, lease age 59m).', {
                code: 4090,
                data: { ownership: { ...OWNERSHIP, session_id: RUNTIME_SESSION_ID }, reason: 'SESSION_NOT_OWNED' }
              })
            }

            return submitReceipt(params)
          }

          return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
        })
      }
    }

    it('replays the HUD surface and title_preview the send was refused with, after the HUD closed', async () => {
      $pendingSessionMoves.set({})
      // Typed into the floating HUD: the gateway digests `surface: 'hud'`.
      $hudMode.set(true)

      const submitted: Record<string, unknown>[] = []
      const { requestGateway } = movableGateway(submitted)

      let handle: HarnessHandle | null = null
      await actRender(
        <Harness onReady={h => (handle = h)} refreshSessions={async () => undefined} requestGateway={requestGateway} />
      )

      expect(await handle!.submitText('look at this', { attachments: [REPORT] })).toBe(false)

      expect(submitted[0]).toMatchObject({ surface: 'hud', title_preview: 'Q3 report' })

      // The user dismisses the HUD and clicks "Move here" from the main window.
      // Nothing about the message changed — only the window they are looking at.
      $hudMode.set(false)
      $composerAttachments.set([])

      const pending = $pendingSessionMoves.get()[RUNTIME_SESSION_ID][0]

      expect(await moveSessionHere(RUNTIME_SESSION_ID)).toEqual({ ok: true })
      expect(submitted).toHaveLength(2)

      const { session_id: refusedTarget, submission_id: _refusedAttempt, ...refusedPayload } = submitted[0]
      const { session_id: replayTarget, client_send_id: replaySendId, submission_id: _replayAttempt, ...replayPayload } = submitted[1]

      // Byte-for-byte the same message; only the transient keys differ.
      expect(replayPayload).toEqual(refusedPayload)
      expect(replayTarget).toBe(refusedTarget)
      expect(replaySendId).toBe(pending.sendId)
    })

    it('retranslates only the session target when the runtime id was rebound', async () => {
      $pendingSessionMoves.set({})
      $hudMode.set(true)

      const submitted: Record<string, unknown>[] = []
      const calls: string[] = []
      const runtimeIdByStoredSessionIdRef = { current: new Map([['stored-1', 'rt-1']]) }

      const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
        calls.push(method)

        if (method === 'session.handoff.claim') {
          return { generation: 5, status: 'claimed' } as never
        }

        if (method === 'session.resume') {
          return { session_id: 'rt-2' } as never
        }

        if (method === 'prompt.submit') {
          submitted.push({ ...params })

          if (!calls.includes('session.handoff.claim')) {
            throw new JsonRpcGatewayError('Session stored-1 already has a live owner (desktop, lease age 59m).', {
              code: 4090,
              data: { ownership: { ...OWNERSHIP, session_id: 'stored-1' }, reason: 'SESSION_NOT_OWNED' }
            })
          }

          return submitReceipt(params)
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })

      let handle: HarnessHandle | null = null
      await actRender(
        <Harness
          activeSessionId="rt-1"
          onReady={h => (handle = h)}
          refreshSessions={async () => undefined}
          requestGateway={requestGateway}
          runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
          storedSessionId="stored-1"
        />
      )

      expect(await handle!.submitText('look at this')).toBe(false)
      expect(submitted[0]).toMatchObject({ session_id: 'rt-1', surface: 'hud' })

      // The runtime binding was reaped between the refusal and the click, so the
      // replay has to resume the STORED chat and address a freshly minted id.
      runtimeIdByStoredSessionIdRef.current.delete('stored-1')
      $hudMode.set(false)

      expect(await moveSessionHere('stored-1')).toEqual({ ok: true })
      expect(calls).toContain('session.resume')

      const { session_id: refusedTarget, submission_id: _refusedAttempt, ...refusedPayload } = submitted[0]
      const { session_id: replayTarget, client_send_id: _sendId, submission_id: _replayAttempt, ...replayPayload } = submitted[1]

      // The target moved to the rebound runtime; the meaning did not.
      expect(refusedTarget).toBe('rt-1')
      expect(replayTarget).toBe('rt-2')
      expect(replayPayload).toEqual(refusedPayload)
      expect(replayPayload).toMatchObject({ surface: 'hud', text: 'look at this' })
    })
  })

  // rememberRefusedSend updates an attempt in place ONLY when it is handed that
  // attempt's immutable id. Without it threaded through the resend, a re-refused
  // move mints a second identity and queues the SAME message twice — the user
  // sees one send become two pending cards, and completing one leaves the other
  // holding a message the gateway has already accepted.
  describe('a re-refused move updates the attempt it retried', () => {
    function refusingGateway() {
      return vi.fn(async (method: string, params?: unknown) => {
        if (method === 'session.handoff.claim') {
          return { generation: 5, status: 'claimed' } as never
        }

        if (method === 'prompt.submit') {
          throw new JsonRpcGatewayError('Session stored-1 already has a live owner (desktop, lease age 59m).', {
            code: 4090,
            data: { ownership: { ...OWNERSHIP, session_id: RUNTIME_SESSION_ID }, reason: 'SESSION_NOT_OWNED' }
          })
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })
    }

    it('re-records the same attempt instead of queueing the message twice', async () => {
      $pendingSessionMoves.set({})

      let handle: HarnessHandle | null = null
      await actRender(
        <Harness
          onReady={h => (handle = h)}
          refreshSessions={async () => undefined}
          requestGateway={refusingGateway()}
        />
      )

      expect(await handle!.submitText('move this one over')).toBe(false)

      const before = $pendingSessionMoves.get()[RUNTIME_SESSION_ID][0]

      // Claim lands, resend is refused again (the other device took it straight back).
      expect((await moveSessionHere(RUNTIME_SESSION_ID)).reason).toBe('resend_failed')

      const after = $pendingSessionMoves.get()[RUNTIME_SESSION_ID]

      expect(after).toHaveLength(1)
      expect(after[0].attemptId).toBe(before.attemptId)
      expect(after[0].sendId).toBe(before.sendId)
      expect(after[0].text).toBe('move this one over')
    })

    it('deletes only the attempt that landed, leaving the newer one intact and unduplicated', async () => {
      $pendingSessionMoves.set({})

      const calls: string[] = []

      const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
        calls.push(method)

        if (method === 'session.handoff.claim') {
          return { generation: 5, status: 'claimed' } as never
        }

        if (method === 'prompt.submit') {
          // Only the first message is let through once the chat is ours.
          if (calls.includes('session.handoff.claim') && String(params?.text).includes('first')) {
            return submitReceipt(params)
          }

          throw new JsonRpcGatewayError('Session stored-1 already has a live owner (desktop, lease age 59m).', {
            code: 4090,
            data: { ownership: { ...OWNERSHIP, session_id: RUNTIME_SESSION_ID }, reason: 'SESSION_NOT_OWNED' }
          })
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })

      let handle: HarnessHandle | null = null
      await actRender(
        <Harness onReady={h => (handle = h)} refreshSessions={async () => undefined} requestGateway={requestGateway} />
      )

      await handle!.submitText('first thing')
      await handle!.submitText('second thing')

      const [, second] = $pendingSessionMoves.get()[RUNTIME_SESSION_ID]

      expect((await moveSessionHere(RUNTIME_SESSION_ID)).reason).toBe('resend_failed')

      const after = $pendingSessionMoves.get()[RUNTIME_SESSION_ID]

      expect(after).toHaveLength(1)
      expect(after[0].attemptId).toBe(second.attemptId)
      expect(after[0].sendId).toBe(second.sendId)
      expect(after[0].text).toBe('second thing')
    })
  })

  it('an older tree unmounting after a newer one mounts leaves "Move here" wired to the newer', async () => {
    // A profile swap mounts the replacement tree before React unmounts the old
    // one. The wiring is module-global, so the departing hook must remove only
    // what IT installed — clearing unconditionally leaves the live app's card
    // answering nothing_pending with the user's message still held hostage.
    $pendingSessionMoves.set({})

    const gateway = (log: string[]) =>
      vi.fn(async (method: string, params?: unknown) => {
        log.push(method)

        if (method === 'session.handoff.claim') {
          return { generation: 5, status: 'claimed' } as never
        }

        if (method === 'prompt.submit') {
          return submitReceipt(params)
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })

    const callsOld: string[] = []
    const callsNew: string[] = []

    const oldTree = await actRender(
      <Harness onReady={() => undefined} refreshSessions={async () => undefined} requestGateway={gateway(callsOld)} />
    )

    await actRender(
      <Harness onReady={() => undefined} refreshSessions={async () => undefined} requestGateway={gateway(callsNew)} />
    )

    await act(async () => {
      oldTree.unmount()
    })

    rememberRefusedSend({
      attachments: [],
      ownership: {
        ageSeconds: 12,
        busy: false,
        generation: 4,
        handoffSupported: true,
        leaseId: 'lease-macbook',
        sessionId: RUNTIME_SESSION_ID,
        surface: 'desktop'
      },
      runtimeId: RUNTIME_SESSION_ID,
      storedSessionId: RUNTIME_SESSION_ID,
      text: 'move this one over'
    })

    const moved = await moveSessionHere(RUNTIME_SESSION_ID)

    expect(moved).toEqual({ ok: true })
    expect(callsNew).toContain('session.handoff.claim')
    expect(callsOld).not.toContain('session.handoff.claim')
    expect($pendingSessionMoves.get()).toEqual({})
  })

  // The real cross-device report: MacBook owns the stored chat at generation 3, the
  // user attaches an image on the iMac and sends. The gateway ACCEPTS prompt.submit
  // (staging the image warmed a deferred agent build) and the fence refuses the turn at
  // admission, so the refusal never reaches the catch below — it arrives as a stream
  // error. The card showed the SESSION_NOT_OWNED copy with no "Move here", because the
  // send the composer had already cleared existed nowhere. The pipeline therefore HOLDS
  // an accepted send until its turn is accounted for.
  describe('an image send the gateway accepts and the fence refuses at turn admission', () => {
    const STORED = '20260919_162101_617cd0'
    const RUNTIME = 'rt-imac-9f21'

    const IMAGE_OWNERSHIP = {
      ageSeconds: 62,
      busy: false,
      generation: 3,
      handoffSupported: true,
      leaseId: 'lease-macbook',
      sessionId: STORED,
      surface: 'desktop'
    }

    const SCREENSHOT: ComposerAttachment = {
      id: 'att-shot',
      kind: 'image',
      label: 'shot.png',
      path: '/Users/u/Desktop/shot.png',
      // The chip already holds the file as a base64 data URL, so staging needs no disk read.
      previewUrl: 'data:image/png;base64,aGVsbG8='
    }

    beforeEach(() => {
      $pendingSessionMoves.set({})
      // Cross-device: the chat lives on a remote Hermes, so the image crosses as bytes.
      $connection.set({ mode: 'remote' } as never)
      // Remote staging reads the full-resolution bytes fresh from disk via the desktop bridge.
      vi.stubGlobal('hermesDesktop', { readFileDataUrl: vi.fn(async () => 'data:image/png;base64,aGVsbG8=') })
    })

    afterEach(() => {
      forgetInFlightSend(RUNTIME)
      $connection.set(null as never)
      vi.unstubAllGlobals()
    })

    /** Accepts the submit, as the gateway does when the fence only refuses at admission. */
    async function sendImage() {
      const calls: string[] = []

      const requestGateway = vi.fn(async (method: string, params?: unknown) => {
        calls.push(method)

        if (method === 'image.attach_bytes') {
          return { attached: true, count: 1, path: '/remote/.hermes/images/upload_1.png' } as never
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })

      let handle: HarnessHandle | null = null
      await actRender(
        <Harness
          activeSessionId={RUNTIME}
          onReady={h => (handle = h)}
          refreshSessions={async () => undefined}
          requestGateway={requestGateway as never}
          runtimeIdByStoredSessionIdRef={{ current: new Map([[STORED, RUNTIME]]) }}
          storedSessionId={STORED}
        />
      )

      expect(await handle!.submitText('what is in this screenshot?', { attachments: [SCREENSHOT] })).toBe(true)
      expect(calls).toEqual(['image.attach_bytes', 'prompt.submit'])
    }

    it('holds the accepted send so the async refusal can still offer the move', async () => {
      await sendImage()

      // Nothing is pending yet: the send was accepted and may well run.
      expect($pendingSessionMoves.get()).toEqual({})

      // The turn is refused at admission; the stream dispatcher promotes the hold.
      expect(armRefusedSendFromTurn(RUNTIME, IMAGE_OWNERSHIP)).not.toBeNull()

      const queued = $pendingSessionMoves.get()[STORED]

      expect(queued).toHaveLength(1)
      expect(queued[0].text).toBe('what is in this screenshot?')
      // The WORKSPACE-staged copy, so the move's resend does not re-upload the bytes.
      expect(queued[0].attachments.map(a => a.path)).toEqual(['/remote/.hermes/images/upload_1.png'])
      // The exact digest-bound params the refused submit carried.
      expect(queued[0].payload).toMatchObject({ text: 'what is in this screenshot?' })
      expect(queued[0].runtimeId).toBe(RUNTIME)
      // ...and the card offers the move.
      expect(canMoveSessionHere(STORED)).toBe(true)
    })

    it('releases the hold once the turn runs, so a later refusal cannot resend it', async () => {
      await sendImage()

      // message.complete for this runtime: the send is in the chat now.
      forgetInFlightSend(RUNTIME)

      expect(armRefusedSendFromTurn(RUNTIME, IMAGE_OWNERSHIP)).toBeNull()
      expect($pendingSessionMoves.get()).toEqual({})
    })

    it('records the refusal in the open — and holds nothing — when the submit itself is refused', async () => {
      const requestGateway = vi.fn(async (method: string, params?: unknown) => {
        if (method === 'image.attach_bytes') {
          return { attached: true, count: 1, path: '/remote/.hermes/images/upload_1.png' } as never
        }

        if (method === 'prompt.submit') {
          throw new JsonRpcGatewayError(
            `Session ${STORED} is open in another Hermes window/terminal.`,
            { code: 4090, data: { ownership: { ...OWNERSHIP, session_id: STORED, generation: 3 }, reason: 'SESSION_NOT_OWNED' } }
          )
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })

      let handle: HarnessHandle | null = null
      await actRender(
        <Harness
          activeSessionId={RUNTIME}
          onReady={h => (handle = h)}
          refreshSessions={async () => undefined}
          requestGateway={requestGateway as never}
          runtimeIdByStoredSessionIdRef={{ current: new Map([[STORED, RUNTIME]]) }}
          storedSessionId={STORED}
        />
      )

      expect(await handle!.submitText('what is in this screenshot?', { attachments: [SCREENSHOT] })).toBe(false)

      expect($pendingSessionMoves.get()[STORED]).toHaveLength(1)
      expect(canMoveSessionHere(STORED)).toBe(true)
      // The sync path recorded it, so nothing is left held for a later turn to promote.
      expect(armRefusedSendFromTurn(RUNTIME, IMAGE_OWNERSHIP)).toBeNull()
      expect($pendingSessionMoves.get()[STORED]).toHaveLength(1)
    })
  })

  // The cleanup half of the same cross-device report. The move itself worked — one
  // durable user row, the ownership generation advanced — but the iMac was left showing
  // the message THREE times: the refused attempt's optimistic bubble, its ownership
  // card, and the replay's own freshly minted bubble, with the image still chipped in
  // the composer (where the rejected dispatch had put it back). Nothing here is backend
  // duplication; it is renderer identity, so the attempt carries the rows and the
  // occurrences it owns and the landed replay reconciles exactly those.
  describe('a landed move leaves exactly what the user sent, once', () => {
    const STORED = '20260919_162101_617cd0'
    const RUNTIME = 'rt-imac-9f21'

    const OWNER_WIRE = { ...OWNERSHIP, generation: 3, session_id: STORED }

    /** The chip as the composer staged it. The occurrence token is what separates THIS
     *  submission of the image from one the user drags in again afterwards. */
    const SHOT: ComposerAttachment = {
      id: 'att-shot',
      kind: 'image',
      label: 'shot.png',
      occurrenceId: 'occ-submitted',
      path: '/Users/u/Desktop/shot.png',
      previewUrl: 'data:image/png;base64,aGVsbG8='
    }

    /** An ownership card from an EARLIER refusal, still the only route left to send the
     *  message it is holding. A sweep of "every SESSION_NOT_OWNED card" would delete it. */
    const HISTORICAL_CARD = {
      completedAt: 1_700_000_000,
      error: 'Session is open in another Hermes window.',
      errorSurface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', retryable: false },
      id: 'assistant-error-historical',
      parts: [],
      role: 'assistant',
      timestamp: 1_700_000_000
    }

    interface Row {
      error?: string
      errorSurface?: { code?: string }
      id: string
      role: string
      attachmentRefs?: string[]
    }

    beforeEach(() => {
      $pendingSessionMoves.set({})
      $composerAttachments.set([])
      // Cross-device: the chat lives on a remote Hermes, so the image crosses as bytes.
      $connection.set({ mode: 'remote' } as never)
      vi.stubGlobal('hermesDesktop', { readFileDataUrl: vi.fn(async () => 'data:image/png;base64,aGVsbG8=') })
    })

    afterEach(() => {
      forgetInFlightSend(RUNTIME)
      $composerAttachments.set([])
      $connection.set(null as never)
      vi.unstubAllGlobals()
    })

    /** Refuses prompt.submit until the chat is claimed — the ordinary sync refusal,
     *  which is what leaves both rows on screen and the chips back in the composer. */
    function movableGateway({ alwaysRefuse = false } = {}) {
      const calls: string[] = []

      const requestGateway = vi.fn(async (method: string, params?: unknown) => {
        calls.push(method)

        if (method === 'image.attach_bytes') {
          return { attached: true, count: 1, path: '/remote/.hermes/images/upload_1.png' } as never
        }

        if (method === 'session.handoff.claim') {
          return { generation: 4, status: 'claimed' } as never
        }

        if (method === 'prompt.submit') {
          if (alwaysRefuse || !calls.includes('session.handoff.claim')) {
            throw new JsonRpcGatewayError(`Session ${STORED} is open in another Hermes window/terminal.`, {
              code: 4090,
              data: { ownership: OWNER_WIRE, reason: 'SESSION_NOT_OWNED' }
            })
          }

          return submitReceipt(params)
        }

        return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
      })

      return { calls, requestGateway }
    }

    /**
     * Send the image and let the fence refuse it, then put the composer back the way the
     * rejected dispatch leaves it: the submitted occurrence restored (use-composer-submit
     * reloads what the gateway rejected) plus a second occurrence of the SAME image the
     * user added while deciding what to do.
     */
    async function refusedImageSend(gateway: ReturnType<typeof movableGateway>) {
      const rows: Row[][] = []
      let handle: HarnessHandle | null = null

      await actRender(
        <Harness
          activeSessionId={RUNTIME}
          onReady={h => (handle = h)}
          onSeedState={next => rows.push(next.messages as Row[])}
          refreshSessions={async () => undefined}
          requestGateway={gateway.requestGateway as never}
          runtimeIdByStoredSessionIdRef={{ current: new Map([[STORED, RUNTIME]]) }}
          seedMessages={[HISTORICAL_CARD]}
          storedSessionId={STORED}
        />
      )

      expect(await handle!.submitText('what is in this screenshot?', { attachments: [SHOT] })).toBe(false)

      $composerAttachments.set([
        { ...SHOT, attachedSessionId: RUNTIME, path: '/remote/.hermes/images/upload_1.png' },
        { ...SHOT, occurrenceId: 'occ-readded' }
      ])

      return { latest: () => rows[rows.length - 1] }
    }

    it('reuses the bubble the refusal painted rather than appending a second copy', async () => {
      const gateway = movableGateway()
      const { latest } = await refusedImageSend(gateway)

      const refusedRows = latest()
      const bubble = refusedRows.find(row => row.role === 'user')!

      expect(bubble.attachmentRefs).toHaveLength(1)

      expect(await moveSessionHere(STORED)).toEqual({ ok: true })

      const movedRows = latest()

      // One message typed, one message shown — under the id it has had all along, so the
      // bubble the user watched is the bubble that finally sent.
      expect(movedRows.filter(row => row.role === 'user')).toHaveLength(1)
      expect(movedRows.find(row => row.role === 'user')!.id).toBe(bubble.id)
      expect(movedRows.find(row => row.role === 'user')!.attachmentRefs).toHaveLength(1)
    })

    it('removes the card for THIS attempt and leaves an unrelated ownership card standing', async () => {
      const gateway = movableGateway()
      const { latest } = await refusedImageSend(gateway)

      const refusedRows = latest()

      const thisCard = refusedRows.find(
        row => row.errorSurface?.code === 'SESSION_NOT_OWNED' && row.id !== HISTORICAL_CARD.id
      )!

      expect(thisCard).toBeDefined()

      expect(await moveSessionHere(STORED)).toEqual({ ok: true })

      const movedRows = latest()

      expect(movedRows.some(row => row.id === thisCard.id)).toBe(false)
      // The older refusal is still holding its own unsent message; taking its card away
      // would take the only way to send it too.
      expect(movedRows.some(row => row.id === HISTORICAL_CARD.id)).toBe(true)
    })

    it('removes the occurrence it actually sent and keeps the one the user re-added', async () => {
      const gateway = movableGateway()
      await refusedImageSend(gateway)

      expect(await moveSessionHere(STORED)).toEqual({ ok: true })

      // Same image, same id — a different occurrence, so it is still the user's to send.
      expect($composerAttachments.get().map(a => a.occurrenceId)).toEqual(['occ-readded'])
    })

    it('replays the message exactly once and holds nothing back', async () => {
      const gateway = movableGateway()
      await refusedImageSend(gateway)

      expect(await moveSessionHere(STORED)).toEqual({ ok: true })

      expect(gateway.calls.filter(method => method === 'prompt.submit')).toHaveLength(2)
      expect(gateway.calls.filter(method => method === 'session.handoff.claim')).toHaveLength(1)
      expect($pendingSessionMoves.get()).toEqual({})
    })

    // The other half of the contract: nothing is cleaned up on a move that did NOT land.
    // The evidence and the draft are all the user has left to try again with.
    describe('a move refused again preserves everything needed to retry', () => {
      it('keeps one attempt, one bubble, one card and both chips', async () => {
        const gateway = movableGateway({ alwaysRefuse: true })
        const { latest } = await refusedImageSend(gateway)

        const before = $pendingSessionMoves.get()[STORED][0]

        expect((await moveSessionHere(STORED)).reason).toBe('resend_failed')

        const rows = latest()

        // The attempt still exists, under the identities it was minted with.
        const after = $pendingSessionMoves.get()[STORED]

        expect(after).toHaveLength(1)
        expect(after[0].attemptId).toBe(before.attemptId)
        expect(after[0].sendId).toBe(before.sendId)
        expect(after[0].errorMessageId).toBe(before.errorMessageId)
        expect(after[0].optimisticMessageId).toBe(before.optimisticMessageId)

        // The re-refusal repaints the card this attempt already owns instead of stacking
        // a second one, and the bubble is still the single one it started as.
        expect(rows.filter(row => row.role === 'user')).toHaveLength(1)
        expect(rows.filter(row => row.errorSurface?.code === 'SESSION_NOT_OWNED')).toHaveLength(2)
        expect(rows.some(row => row.id === before.errorMessageId)).toBe(true)
        expect(rows.some(row => row.id === HISTORICAL_CARD.id)).toBe(true)

        // Nothing was taken out of the composer, so the draft survives the failed move.
        expect($composerAttachments.get().map(a => a.occurrenceId)).toEqual(['occ-submitted', 'occ-readded'])
      })

      it('still moves cleanly on a later click that does land', async () => {
        let refuse = true
        const calls: string[] = []

        const requestGateway = vi.fn(async (method: string, params?: unknown) => {
          calls.push(method)

          if (method === 'image.attach_bytes') {
            return { attached: true, count: 1, path: '/remote/.hermes/images/upload_1.png' } as never
          }

          if (method === 'session.handoff.claim') {
            return { generation: 4, status: 'claimed' } as never
          }

          if (method === 'prompt.submit') {
            if (refuse) {
              throw new JsonRpcGatewayError(`Session ${STORED} is open in another Hermes window/terminal.`, {
                code: 4090,
                data: { ownership: OWNER_WIRE, reason: 'SESSION_NOT_OWNED' }
              })
            }

            return submitReceipt(params)
          }

          return (method === 'prompt.submit' ? submitReceipt(params) : {}) as never
        })

        const { latest } = await refusedImageSend({ calls, requestGateway })

        expect((await moveSessionHere(STORED)).reason).toBe('resend_failed')

        refuse = false

        expect(await moveSessionHere(STORED)).toEqual({ ok: true })

        const rows = latest()

        // One bubble, the repainted card gone with the attempt, the historical card kept.
        expect(rows.filter(row => row.role === 'user')).toHaveLength(1)
        expect(rows.filter(row => row.errorSurface?.code === 'SESSION_NOT_OWNED').map(row => row.id)).toEqual([
          HISTORICAL_CARD.id
        ])
        expect($composerAttachments.get().map(a => a.occurrenceId)).toEqual(['occ-readded'])
        expect($pendingSessionMoves.get()).toEqual({})
      })
    })
  })
})
