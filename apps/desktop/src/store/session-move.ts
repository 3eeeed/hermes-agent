/**
 * MOVE A CHAT HERE — the client half of `session.handoff.claim`.
 *
 * Two Desktop windows on two machines can drive one remote Hermes profile (each
 * ownershipId spawns its own `hermes serve --isolated`), so they are separate
 * processes sharing one active-session registry. Opening a chat on the second
 * device reads fine; sending is refused `SESSION_NOT_OWNED`, because exactly one
 * writer per chat is a correctness guarantee, not a limitation to route around.
 *
 * The user's way through is an explicit move, and this store owns it:
 *
 *  - **It is the preservation mechanism.** `use-composer-submit` clears the
 *    draft and the attachment chips BEFORE dispatching the send, so once the
 *    gateway refuses, the message exists nowhere else. We hold the raw text and
 *    the already-staged attachment objects until the user decides.
 *  - **The claim is exact.** We echo the `(leaseId, generation)` the refusal
 *    named. A click on a stale card loses safely — the backend's
 *    compare-and-swap refuses it — rather than stealing from whoever owns the
 *    chat now.
 *  - **The resend is once.** Claim, then resend, then forget. A double click is
 *    dropped, not queued: a duplicate would post the user's message twice into
 *    the chat it just took over. Any failure keeps everything, and the resend
 *    always names the STORED session, so a lost move never turns into a
 *    silently-minted new chat.
 *
 * The gateway work (fencing generations, owner quiescence, fail-closed refusals)
 * lives in `hermes_cli/active_sessions.py` and `tui_gateway/methods_session_handoff.py`.
 */

import { map } from 'nanostores'

import type { ComposerAttachment } from './composer'

/** The live owner a `4090` refusal named — the compare-and-swap precondition.
 *  Mirrors `ActiveSessionOwner.as_wire()`; carries no pid, path or credential. */
export interface SessionOwnership {
  sessionId: string
  leaseId: string
  generation: number
  surface: string
  busy: boolean
  ageSeconds: null | number
  /** False for an owner running a Hermes without fencing generations: its lease
   *  cannot be fenced, so a move would not be safe and is not offered. */
  handoffSupported: boolean
}

/**
 * The `prompt.submit` params a send was built from, minus the two keys the gateway
 * treats as transient (`session_id`, re-minted by every reconnect, and
 * `client_send_id`, the retry key itself). Everything else is digested: the text,
 * the display kind, the surface, the voice context, the queued flag, the title
 * preview — and anything added later.
 *
 * That digest is bound to the send id, so a retry carrying the SAME id with a
 * DIFFERENT payload is refused outright (`conflict`) rather than answered. A
 * payload rebuilt at click time is a different payload: the HUD that set
 * `surface: 'hud'` has since closed, and the composer that supplied
 * `title_preview` has been emptied. Hence this is captured once, at the refusal,
 * and replayed verbatim.
 */
export type PromptSubmitPayload = Readonly<
  Record<string, boolean | number | readonly number[] | readonly { mime: string; path: string }[] | string>
>

/** A send the fence refused, kept whole so the move can finish it. */
export interface PendingSessionMove {
  storedSessionId: string
  /** Immutable identity for THIS refused send, minted once when it is recorded.
   *  Completion compare-and-deletes on it, so an older move finishing can never
   *  swallow a newer message the user typed while it was in flight. */
  attemptId: string
  /** Immutable per-message id sent to the gateway as `client_send_id`. Reused by
   *  every retry of this message, so an acceptance whose RESPONSE was lost is
   *  recognised server-side instead of posting the message twice. */
  sendId: string
  /** This device's live runtime id for the chat at refusal time. Stale ids are
   *  recovered by the claim itself (the shared session-not-found resume). */
  runtimeId: null | string
  /** The RAW composer text, not the ref-expanded prompt: the resend rebuilds
   *  attachment refs itself, exactly as a first-time send does. */
  text: string
  /** Attachment objects as they were when the submit failed — already staged
   *  into the session workspace, so the resend does not re-upload them. */
  attachments: ComposerAttachment[]
  /** The EXACT digest-bound params the refused submit carried, when it got far
   *  enough to build them. The resend replays this instead of rebuilding from
   *  live UI state — see PromptSubmitPayload. Absent when the send failed before
   *  the params existed (a credential or attach failure), where the resend has
   *  no choice but to rebuild. */
  payload?: PromptSubmitPayload
  displayText?: string
  displayKind?: string
  /** The optimistic transcript row this send already painted. The replay REUSES it
   *  instead of minting a fresh one, so the move updates the bubble the user has
   *  been looking at rather than appending a second copy of their own message
   *  beside it. Absent for an off-screen send, which paints no bubble. */
  optimisticMessageId?: string
  /** The ownership error card THIS refusal painted. A landed replay removes exactly
   *  that row — never "every SESSION_NOT_OWNED card", which would delete an older
   *  message's card while it is still holding that message unsent. Absent until the
   *  card exists (the asynchronous branch only learns the id once it is rendered). */
  errorMessageId?: string
  ownership: null | SessionOwnership
}

export interface AcceptedSessionMoveSend {
  attemptId: string
  storedSessionId: string
  text: string
}

const acceptedSendListeners = new Set<(accepted: AcceptedSessionMoveSend) => void>()

/** Tell the originating composer that a refused send finally landed. The
 * composer owns the live editor and can therefore clear only the restoration
 * produced by this attempt, while preserving anything the user typed later. */
export function announceSessionMoveSendAccepted(accepted: AcceptedSessionMoveSend): void {
  for (const listener of acceptedSendListeners) {
    listener(accepted)
  }
}

export function onSessionMoveSendAccepted(listener: (accepted: AcceptedSessionMoveSend) => void): () => void {
  acceptedSendListeners.add(listener)

  return () => acceptedSendListeners.delete(listener)
}

/** Refused sends per stored chat, oldest first. A chat another device owns refuses
 *  EVERY send, so "the user typed twice" is the ordinary case — one slot per chat
 *  meant the second message silently overwrote the first. */
export const $pendingSessionMoves = map<Record<string, PendingSessionMove[]>>({})

let attemptCounter = 0

function mintId(kind: string): string {
  attemptCounter += 1

  return `${kind}-${Date.now().toString(36)}-${attemptCounter}-${Math.random().toString(36).slice(2, 10)}`
}

/** Parse the wire `ownership` block (snake_case, straight off the RPC). */
export function parseSessionOwnership(value: unknown): null | SessionOwnership {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as Record<string, unknown>
  const leaseId = typeof raw.lease_id === 'string' ? raw.lease_id : ''
  const generation = typeof raw.generation === 'number' ? raw.generation : 0

  // A generation is a counter, and it is half of a compare-and-swap precondition:
  // a float, NaN, Infinity or 0 is not a value to guess a fencing comparison from.
  if (!leaseId || !Number.isSafeInteger(generation) || generation <= 0) {
    return null
  }

  return {
    ageSeconds: typeof raw.age_seconds === 'number' ? raw.age_seconds : null,
    busy: raw.busy === true,
    generation,
    handoffSupported: raw.handoff_supported === true,
    leaseId,
    sessionId: typeof raw.session_id === 'string' ? raw.session_id : '',
    surface: typeof raw.surface === 'string' ? raw.surface : 'unknown'
  }
}

/** What a caller supplies when recording a refusal: identities are minted here. */
export type RefusedSend = Omit<PendingSessionMove, 'attemptId' | 'sendId'> &
  Partial<Pick<PendingSessionMove, 'attemptId' | 'sendId'>>

/** Record a refused send, appending it behind anything already waiting. Identities are
 *  minted once and never rewritten, so re-recording the SAME attempt (a retry that was
 *  refused again) updates it in place instead of queueing the message twice. */
export function rememberRefusedSend(pending: RefusedSend): void {
  if (!pending.storedSessionId) {
    return
  }

  const attempt: PendingSessionMove = {
    ...pending,
    attemptId: pending.attemptId || mintId('attempt'),
    sendId: pending.sendId || mintId('send')
  }

  const queue = $pendingSessionMoves.get()[pending.storedSessionId] ?? []
  const existing = queue.findIndex(entry => entry.attemptId === attempt.attemptId)

  $pendingSessionMoves.setKey(
    pending.storedSessionId,
    existing >= 0 ? queue.map((entry, index) => (index === existing ? attempt : entry)) : [...queue, attempt]
  )
}

/**
 * The send this device has in flight for one runtime, held until the turn it started
 * either produces something or is refused.
 *
 * A refusal does NOT always come back as the submit's own rejection. When the gateway
 * accepts the submit and the fence catches it at turn admission — the ordinary shape
 * once staging an attachment has warmed a deferred agent build — the refusal arrives
 * asynchronously on the event stream instead, where the payload that produced it is
 * long out of scope. Without this, that refusal renders the card with no way to finish
 * the send: the composer cleared the draft and the chips at dispatch, so the user's
 * message exists nowhere else.
 *
 * Keyed by RUNTIME id, which is what a stream event names. Sends stay FIFO: while
 * turn A is running the composer can queue B, and A's `message.complete` must retire
 * only A rather than deleting B's refusal recovery envelope.
 */
const inFlightSends = new Map<string, PendingSessionMove[]>()

/** Hold a dispatched send against an asynchronous refusal. Identities are minted here so a
 *  send armed later carries the SAME attempt/send id it would have had at a sync refusal. */
export function rememberInFlightSend(runtimeId: string, pending: RefusedSend): null | PendingSessionMove {
  if (!runtimeId || !pending.storedSessionId) {
    return null
  }

  const attempt: PendingSessionMove = {
    ...pending,
    attemptId: pending.attemptId || mintId('attempt'),
    sendId: pending.sendId || mintId('send')
  }

  const queue = inFlightSends.get(runtimeId) ?? []

  inFlightSends.set(runtimeId, [...queue, attempt])

  return attempt
}

/** Drop one known failed/re-keyed submit. With no identity this is an explicit
 * runtime teardown and clears every hold (used by session disposal/tests). */
export function forgetInFlightSend(runtimeId: null | string | undefined, attemptId?: string): void {
  if (!runtimeId) {
    return
  }

  if (!attemptId) {
    inFlightSends.delete(runtimeId)

    return
  }

  const kept = (inFlightSends.get(runtimeId) ?? []).filter(entry => entry.attemptId !== attemptId)

  if (kept.length) {
    inFlightSends.set(runtimeId, kept)
  } else {
    inFlightSends.delete(runtimeId)
  }
}

/** A completed turn consumes only the oldest envelope for this runtime. */
export function completeInFlightSend(runtimeId: null | string | undefined): void {
  if (!runtimeId) {
    return
  }

  const queue = inFlightSends.get(runtimeId) ?? []
  const remaining = queue.slice(1)

  if (remaining.length) {
    inFlightSends.set(runtimeId, remaining)
  } else {
    inFlightSends.delete(runtimeId)
  }
}

/**
 * Promote the send held for `runtimeId` into a movable pending attempt — the async half of
 * the SESSION_NOT_OWNED branch in the submit pipeline.
 *
 * Fail-closed in both directions: with nothing held there is nothing to finish and no
 * attempt is invented, and the attempt is keyed by the STORED chat the send targeted (what
 * the error card asks about), never by the runtime id the event happened to name.
 *
 * Returns the armed attempt (null when nothing was held) — the caller needs its identity
 * to record which error card this refusal is about to paint.
 */
export function armRefusedSendFromTurn(
  runtimeId: null | string | undefined,
  ownership: null | SessionOwnership
): null | PendingSessionMove {
  const queue = runtimeId ? inFlightSends.get(runtimeId) : undefined
  const held = queue?.[0]

  if (!held) {
    return null
  }

  completeInFlightSend(runtimeId)
  rememberRefusedSend({ ...held, ownership: ownership ?? held.ownership })

  return pendingSessionMoves(held.storedSessionId).find(entry => entry.attemptId === held.attemptId) ?? null
}

/**
 * Bind the error card a refusal painted to the attempt it belongs to.
 *
 * The synchronous branch mints the row id itself and records it with the attempt. The
 * asynchronous one cannot: the card is painted by the stream dispatcher's own failure
 * path, which either adopts the live assistant row or mints one, so the id only exists
 * after the fact. Without this the landed replay would have no way to name the stale
 * card except "any ownership error", which would also delete a card still holding an
 * older unsent message.
 *
 * No-op when the attempt is already gone (a move that finished first).
 */
export function noteSessionMoveErrorMessage(storedSessionId: string, attemptId: string, errorMessageId: string): void {
  const queue = $pendingSessionMoves.get()[storedSessionId]

  if (!queue?.some(entry => entry.attemptId === attemptId)) {
    return
  }

  $pendingSessionMoves.setKey(
    storedSessionId,
    queue.map(entry => (entry.attemptId === attemptId ? { ...entry, errorMessageId } : entry))
  )
}

/** The owner block is only a usable compare-and-swap precondition when it describes
 *  THIS chat with a finite positive generation the backend can fence on. Anything else
 *  (a mismatched session id, a float/NaN/0 generation, an owner that predates fencing)
 *  is not something to guess at — the card falls back to "start a new chat". */
function claimableOwner(
  storedSessionId: string,
  ownership: null | SessionOwnership
): null | SessionOwnership {
  if (!ownership?.handoffSupported || !ownership.leaseId) {
    return null
  }

  const { generation } = ownership

  if (!Number.isSafeInteger(generation) || generation <= 0) {
    return null
  }

  // A block naming another chat means the refusal and the preserved send disagree
  // about what is being moved; claiming on it could move the WRONG conversation.
  return ownership.sessionId && ownership.sessionId !== storedSessionId ? null : ownership
}

/** Whether "Move here" can actually do anything for this chat: a refused send is
 *  waiting AND its owner block is a usable compare-and-swap precondition. The card
 *  hides the button otherwise — an action that can only fail is worse than none. */
export function canMoveSessionHere(storedSessionId: null | string | undefined): boolean {
  const queued = pendingSessionMoves(storedSessionId)

  return Boolean(
    queued.length && storedSessionId && claimableOwner(storedSessionId, queued[queued.length - 1].ownership)
  )
}

/** Every refused send for this chat, oldest first. */
export function pendingSessionMoves(storedSessionId: null | string | undefined): PendingSessionMove[] {
  return (storedSessionId && $pendingSessionMoves.get()[storedSessionId]) || []
}

/** The oldest refused send for this chat — what the error card asks about to decide
 *  whether there is anything for "Move here" to finish. */
export function pendingSessionMove(storedSessionId: null | string | undefined): null | PendingSessionMove {
  return pendingSessionMoves(storedSessionId)[0] ?? null
}

/** Drop EXACTLY the named attempts. Never "clear the chat": a move that started before
 *  the user's next message must not delete a message it never saw. */
export function forgetSessionMoveAttempts(storedSessionId: string, attemptIds: readonly string[]): void {
  const sent = new Set(attemptIds)

  for (const attemptId of sent) {
    claimedForRetry.delete(attemptId)
  }

  const queue = $pendingSessionMoves.get()[storedSessionId]

  if (!queue?.some(entry => sent.has(entry.attemptId))) {
    return
  }

  const kept = queue.filter(entry => !sent.has(entry.attemptId))

  if (kept.length) {
    $pendingSessionMoves.setKey(storedSessionId, kept)

    return
  }

  const { [storedSessionId]: _dropped, ...rest } = $pendingSessionMoves.get()

  $pendingSessionMoves.set(rest)
}

export function forgetPendingSessionMove(storedSessionId: string): void {
  forgetSessionMoveAttempts(storedSessionId, pendingSessionMoves(storedSessionId).map(entry => entry.attemptId))
}

export interface SessionMoveDeps {
  /** `session.handoff.claim`, recovering a stale runtime id on the way. */
  claim(input: { generation: number; leaseId: string; runtimeId: null | string; storedSessionId: string }): Promise<{
    generation: number
    status: string
  }>
  /** Re-run the refused send through the ordinary submit pipeline. */
  resend(pending: PendingSessionMove): Promise<boolean>
}

/** Every live wiring, oldest first; the last one is what a move runs through.
 *
 *  A register rather than a slot because owners OVERLAP: a profile swap mounts the
 *  replacement `usePromptActions` before React unmounts the old one, and a second
 *  window mounts its own. With one slot, whoever unmounted last won — the departing
 *  hook wiped a wiring it never installed, and the live app's "Move here" answered
 *  `nothing_pending` while still holding the user's message. Each owner is removed
 *  by identity, so a hook can only ever unwire itself. */
const wirings: SessionMoveDeps[] = []
// A claim that succeeded before its resend response was lost. The next click
// retries the immutable send id directly: claiming the pre-move owner again is
// necessarily stale and used to force two or three extra clicks after reconnect.
// Consumed after one direct retry; if that retry is genuinely refused, the
// refreshed ownership it records is claimed normally on the following click.
const claimedForRetry = new Set<string>()

/** Wired by the layer that owns the gateway client and the submit pipeline
 *  (`usePromptActions`), the same inversion as `sessionTileDelegate`. The object
 *  identity is the ownership token: pass the SAME object back to
 *  `clearSessionMoveDeps` on unmount. Re-installing one already registered just
 *  promotes it to newest rather than registering it twice. */
export function setSessionMoveDeps(next: SessionMoveDeps): void {
  const registered = wirings.indexOf(next)

  if (registered >= 0) {
    wirings.splice(registered, 1)
  }

  wirings.push(next)
}

/**
 * Unwire on unmount. A window closing or a profile swap remounting the tree would
 * otherwise leave a closure holding a gateway client the app has already replaced,
 * and a later "Move here" would claim and resend through it.
 *
 * Removes ONLY the wiring it is handed — an owner unmounting out of order cannot
 * unwire anyone else, and when the newest owner leaves, the newest one still mounted
 * takes over instead of the app going unwired. Idempotent.
 *
 * Called with no argument it drops every wiring: a full teardown/reset, not something
 * an individual owner should do.
 */
export function clearSessionMoveDeps(owner?: SessionMoveDeps): void {
  if (!owner) {
    wirings.length = 0
    claimedForRetry.clear()

    return
  }

  const registered = wirings.lastIndexOf(owner)

  if (registered >= 0) {
    wirings.splice(registered, 1)
  }
}

/** The newest still-live wiring. */
function sessionMoveDeps(): null | SessionMoveDeps {
  return wirings[wirings.length - 1] ?? null
}

export interface SessionMoveResult {
  ok: boolean
  reason?: string
  message?: string
}

const inFlight = new Set<string>()

/** Reason codes the backend answers a refused claim with (`error.data.reason`). */
function refusalReason(error: unknown): string {
  const data = (error as { data?: { reason?: unknown } } | undefined)?.data

  return typeof data?.reason === 'string' && data.reason ? data.reason : 'claim_failed'
}

function refreshedOwnership(error: unknown): null | SessionOwnership {
  return parseSessionOwnership((error as { data?: { ownership?: unknown } } | undefined)?.data?.ownership)
}

/**
 * Claim the chat for this device and finish every send the fence refused, in order.
 *
 * Returns `{ok: true}` only when all of them went out. Every other outcome leaves the
 * unsent attempts intact for another try, and re-arms them against the CURRENT owner when
 * the backend named one — so a second click is a fresh compare-and-swap, not a replay that
 * can only fail. Attempts are deleted by exact id as each one lands, so a message the user
 * typed while this move was in flight is never swallowed by it.
 */
export async function moveSessionHere(storedSessionId: string): Promise<SessionMoveResult> {
  const queued = pendingSessionMoves(storedSessionId)
  // Captured once: the claim and the resends that finish it must go through ONE
  // wiring, even if a tree mounts or unmounts while the move is in flight.
  const deps = sessionMoveDeps()

  if (!queued.length || !deps) {
    return { ok: false, reason: 'nothing_pending' }
  }

  // The freshest refusal describes the owner as of the most recent attempt; an older
  // block is a generation the backend has already moved past.
  const owner = claimableOwner(storedSessionId, queued[queued.length - 1].ownership)

  if (!owner) {
    return { ok: false, reason: 'unsupported' }
  }

  if (inFlight.has(storedSessionId)) {
    return { ok: false, reason: 'in_flight' }
  }

  inFlight.add(storedSessionId)

  try {
    const retryAcceptedSend = claimedForRetry.has(queued[0].attemptId)

    if (retryAcceptedSend) {
      // Consume one direct-retry allowance for every attempt the successful
      // claim covered. A genuine re-refusal records fresh ownership and the
      // following click returns to the normal claim path.
      for (const attempt of queued) {
        claimedForRetry.delete(attempt.attemptId)
      }
    }

    if (!retryAcceptedSend) {
      try {
        await deps.claim({
          generation: owner.generation,
          leaseId: owner.leaseId,
          runtimeId: queued[queued.length - 1].runtimeId,
          storedSessionId
        })

        for (const attempt of queued) {
          claimedForRetry.add(attempt.attemptId)
        }
      } catch (error) {
        const ownership = refreshedOwnership(error)

        if (ownership) {
          // Re-arm every waiting attempt against whoever owns the chat NOW, so the next
          // click is a valid compare-and-swap instead of a guaranteed-stale replay.
          for (const attempt of queued) {
            rememberRefusedSend({ ...attempt, ownership })
          }
        }

        return {
          message: error instanceof Error ? error.message : String(error),
          ok: false,
          reason: refusalReason(error)
        }
      }
    }

    // Owned here now. Each attempt is forgotten only once IT has landed: a failed resend
    // must leave that message — and everything the user typed after it — recoverable.
    const landed: string[] = []

    for (const attempt of queued) {
      let sent = false
      let failure: unknown = null

      try {
        sent = await deps.resend(attempt)
      } catch (error) {
        failure = error
      }

      if (!sent) {
        forgetSessionMoveAttempts(storedSessionId, landed)

        return {
          ...(failure instanceof Error ? { message: failure.message } : {}),
          ok: false,
          reason: 'resend_failed'
        }
      }

      announceSessionMoveSendAccepted({
        attemptId: attempt.attemptId,
        storedSessionId,
        text: attempt.text
      })
      landed.push(attempt.attemptId)
    }

    forgetSessionMoveAttempts(storedSessionId, landed)

    return { ok: true }
  } finally {
    inFlight.delete(storedSessionId)
  }
}
