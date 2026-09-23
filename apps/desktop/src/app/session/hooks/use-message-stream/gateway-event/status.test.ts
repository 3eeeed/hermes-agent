// The gateway `error` event (tui_gateway/prompt_turn.py) carries only a
// message — no error_surface. The dispatcher must still classify the two
// refusals it can mean so the card and toast read like a classified turn:
// plain words up front, raw text demoted to the detail line, and Retry gone
// where retrying reproduces the failure.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'
import { $notifications } from '@/store/notifications'
import { requestDesktopOnboarding } from '@/store/onboarding'
import {
  $pendingSessionMoves,
  canMoveSessionHere,
  forgetInFlightSend,
  rememberInFlightSend
} from '@/store/session-move'

import { handleStatusEvent } from './status'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))
vi.mock('@/store/onboarding', () => ({ requestDesktopOnboarding: vi.fn() }))

const OWNED_REFUSAL =
  'Session 20260909_095312_6b93f5 already has a live owner (tui, pid 32977, lease age 22m). ' +
  'Attach through a compatible owner, or close the session in its owning surface before resuming here.'

// A string is the gateway's own `code`; an object is extra payload fields (error_surface).
function errorContext(message: string, extra: Record<string, unknown> | string = {}) {
  const failAssistantMessage = vi.fn()
  const payload = { message, ...(typeof extra === 'string' ? { code: extra } : extra) } as GatewayEventContext['payload']

  const ctx: GatewayEventContext = {
    deps: {
      compactedTurnRef: { current: new Set<string>() },
      failAssistantMessage,
      flushQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(),
      queryClient: { invalidateQueries: vi.fn() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn()
    } as unknown as GatewayEventContext['deps'],
    event: { payload, session_id: 'sess-1', type: 'error' },
    explicitSid: 'sess-1',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload,
    scheduleConfigRefresh: vi.fn(),
    sessionId: 'sess-1'
  }

  return { ctx, failAssistantMessage }
}

afterEach(() => {
  $notifications.set([])
  vi.mocked(requestDesktopOnboarding).mockClear()
})

describe('gateway `error` event → error card + toast', () => {
  it('stamps SESSION_NOT_OWNED on a live-owner refusal so the card drops Retry', () => {
    const { ctx, failAssistantMessage } = errorContext(OWNED_REFUSAL)

    expect(handleStatusEvent(ctx)).toBe(true)

    expect(failAssistantMessage).toHaveBeenCalledWith(
      'sess-1',
      OWNED_REFUSAL,
      1_700_000_100,
      expect.objectContaining({ code: 'SESSION_NOT_OWNED', retryable: false })
    )
  })

  it('toasts the plain explanation, not the lease jargon, and keeps the raw text as detail', () => {
    const { ctx } = errorContext(OWNED_REFUSAL)

    handleStatusEvent(ctx)

    const toast = $notifications.get()[0]

    expect(toast.message).toBeTruthy()
    expect(toast.message).not.toMatch(/lease|pid|live owner/i)
    expect(toast.detail).toBe(OWNED_REFUSAL)
    expect(toast.action).toBeUndefined()
  })

  it('routes a blank install to onboarding on the gateway code, whatever the sentence says', () => {
    // agent init with no usable provider: the gateway stamps code=provider_not_configured
    // (tui_gateway/server.py). The fix for this chat is setup, so the user must land there and
    // not on a toast — even after the sentence is reworded again.
    const { ctx } = errorContext(
      'Hermes could not start the assistant. Details: something new we never matched.',
      'provider_not_configured'
    )

    handleStatusEvent(ctx)

    expect(requestDesktopOnboarding).toHaveBeenCalledTimes(1)
  })

  it('does not route an unrelated failure to onboarding', () => {
    const { ctx } = errorContext('Hermes could not finish this turn. Try again.')

    handleStatusEvent(ctx)

    expect(requestDesktopOnboarding).not.toHaveBeenCalled()
  })

  it("keeps the server's own plain copy as the toast message when no code was recovered", () => {
    // tui_gateway/user_messages.py already writes actionable sentences for
    // pre-turn failures; the generic "couldn't finish" gloss must not bury them.
    const serverCopy = 'Hermes could not start the assistant for this chat. Check your model settings and try again.'
    const { ctx, failAssistantMessage } = errorContext(serverCopy)

    handleStatusEvent(ctx)

    expect(failAssistantMessage).toHaveBeenCalledWith('sess-1', serverCopy, 1_700_000_100, null)
    const toast = $notifications.get()[0]
    expect(toast.message).toBe(serverCopy)
    expect(toast.detail).toBeUndefined()
  })

  it('raises no toast for the session on screen when its card explains the code (one event, one surface)', () => {
    const { ctx, failAssistantMessage } = errorContext('[Errno 28] No space left on device')

    handleStatusEvent({ ...ctx, isActiveEvent: true })

    expect(failAssistantMessage).toHaveBeenCalledTimes(1)
    expect($notifications.get()).toEqual([])
  })

  it("still toasts a bare error's server sentence on screen: the card copy is generic", () => {
    const sentence = 'Another Hermes process holds the auth store lock. Wait for it to finish or close it, then retry.'
    const { ctx } = errorContext(sentence)

    handleStatusEvent({ ...ctx, isActiveEvent: true })

    expect($notifications.get()).toEqual([expect.objectContaining({ message: sentence })])
  })
})

// The cross-device miss (#106217 follow-up): the gateway ACCEPTS prompt.submit and the
// fence catches the turn at ADMISSION — the ordinary shape once staging an attachment
// has warmed a deferred agent build. The refusal then arrives here, on the event stream,
// long after the submit pipeline's own catch could see it. The card rendered the
// SESSION_NOT_OWNED copy and no "Move here", because nothing promoted the send the
// composer had already cleared from the draft.
describe('an ownership refusal that arrives as a turn error', () => {
  const STORED = '20260919_162101_617cd0'
  const RUNTIME = 'sess-1'

  const OWNERSHIP_WIRE = {
    age_seconds: 62,
    busy: false,
    generation: 3,
    handoff_supported: true,
    lease_id: 'lease-macbook',
    session_id: STORED,
    surface: 'desktop'
  }

  const IMAGE: ComposerAttachment = {
    id: 'att-shot',
    kind: 'image',
    label: 'shot.png',
    path: '/remote/.hermes/images/upload_1.png'
  }

  /** The send the submit pipeline holds once the gateway has accepted it. */
  const holdImageSend = () =>
    rememberInFlightSend(RUNTIME, {
      attachments: [IMAGE],
      ownership: null,
      payload: { text: 'what is in this screenshot?' },
      runtimeId: RUNTIME,
      storedSessionId: STORED,
      text: 'what is in this screenshot?'
    })

  beforeEach(() => {
    $pendingSessionMoves.set({})
    forgetInFlightSend(RUNTIME)
  })

  it('promotes the held send to a movable attempt keyed by the STORED chat', () => {
    holdImageSend()

    const { ctx } = errorContext(OWNED_REFUSAL, {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', ownership: OWNERSHIP_WIRE, retryable: false }
    })

    expect(handleStatusEvent(ctx)).toBe(true)

    // Keyed by the stored chat the card asks about — never the runtime id the event named.
    const queued = $pendingSessionMoves.get()[STORED]

    expect(queued).toHaveLength(1)
    expect(queued[0].text).toBe('what is in this screenshot?')
    expect(queued[0].attachments).toEqual([IMAGE])
    // Replayed verbatim: a payload rebuilt at click time collides with its own send id.
    expect(queued[0].payload).toEqual({ text: 'what is in this screenshot?' })
    expect(queued[0].attemptId).toBeTruthy()
    expect(queued[0].sendId).toBeTruthy()
    // ...and the card can actually offer the move.
    expect(canMoveSessionHere(STORED)).toBe(true)
  })

  it('carries the owner block onto the error bubble so the card gates on the same evidence', () => {
    holdImageSend()

    const { ctx, failAssistantMessage } = errorContext(OWNED_REFUSAL, {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', ownership: OWNERSHIP_WIRE, retryable: false }
    })

    handleStatusEvent(ctx)

    expect(failAssistantMessage).toHaveBeenCalledWith(
      RUNTIME,
      OWNED_REFUSAL,
      1_700_000_100,
      expect.objectContaining({
        code: 'SESSION_NOT_OWNED',
        ownership: expect.objectContaining({ generation: 3, handoffSupported: true, leaseId: 'lease-macbook' })
      })
    )
  })

  it('promotes exactly once — a repeated refusal cannot queue the same message twice', () => {
    holdImageSend()

    const event = () =>
      errorContext(OWNED_REFUSAL, {
        error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', ownership: OWNERSHIP_WIRE, retryable: false }
      }).ctx

    handleStatusEvent(event())
    handleStatusEvent(event())

    expect($pendingSessionMoves.get()[STORED]).toHaveLength(1)
  })

  // The card this refusal paints is the one a landed "Move here" has to take back down.
  // Only failAssistantMessage knows which row carried the failure (it either seals the
  // live assistant bubble or mints one), so the dispatcher binds that id to the attempt
  // it just armed. Without it the replay has no way to name the stale card except "any
  // ownership error" — which would also delete an older card still holding its own
  // unsent message.
  it('binds the card it painted to the attempt it armed', () => {
    holdImageSend()

    const { ctx, failAssistantMessage } = errorContext(OWNED_REFUSAL, {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', ownership: OWNERSHIP_WIRE, retryable: false }
    })

    failAssistantMessage.mockReturnValue('assistant-error-1700000100')

    handleStatusEvent(ctx)

    expect($pendingSessionMoves.get()[STORED][0].errorMessageId).toBe('assistant-error-1700000100')
  })

  it('binds nothing when no send was held — an unrelated turn error owns no attempt', () => {
    const { ctx, failAssistantMessage } = errorContext(OWNED_REFUSAL, {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', ownership: OWNERSHIP_WIRE, retryable: false }
    })

    failAssistantMessage.mockReturnValue('assistant-error-1700000100')

    handleStatusEvent(ctx)

    // Fail-closed: no held send, so no attempt was invented to hang the card on.
    expect($pendingSessionMoves.get()).toEqual({})
  })

  it('releases the hold on a terminal error that is not an ownership refusal', () => {
    holdImageSend()

    const { ctx } = errorContext('The model provider had an internal error. Your message was not answered.')

    handleStatusEvent(ctx)

    expect($pendingSessionMoves.get()).toEqual({})

    // Nothing is held, so a LATER turn's ownership refusal cannot resurrect this send.
    const { ctx: refusal } = errorContext(OWNED_REFUSAL, {
      error_surface: { code: 'SESSION_NOT_OWNED', layer: 'gateway', ownership: OWNERSHIP_WIRE, retryable: false }
    })

    handleStatusEvent(refusal)

    expect($pendingSessionMoves.get()).toEqual({})
  })
})
