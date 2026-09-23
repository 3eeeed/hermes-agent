import type { ChatMessage } from '@/lib/chat-messages'
import type { ErrorSurface } from '@/lib/error-surface'
import type { ComposerAttachment } from '@/store/composer'
import {
  forgetInFlightSend,
  type PendingSessionMove,
  type PromptSubmitPayload,
  type RefusedSend,
  rememberInFlightSend,
  rememberRefusedSend
} from '@/store/session-move'

import type { ClientSessionState } from '../../../types'

import { isSessionNotOwnedError, ownershipErrorSurface, sessionOwnershipFromError, type SubmitTextOptions } from './utils'

/** The error card's surface for a refused submit, carrying the live owner the gateway named on a
 *  SESSION_NOT_OWNED refusal: the compare-and-swap precondition the card's "Move here" claims with. */
export function refusalErrorSurface(error: unknown): { errorSurface?: ErrorSurface } {
  const base = ownershipErrorSurface(error)
  const ownership = base.errorSurface?.code === 'SESSION_NOT_OWNED' ? sessionOwnershipFromError(error) : null

  return ownership && base.errorSurface ? { errorSurface: { ...base.errorSurface, ownership } } : base
}

interface RefusedSubmit {
  attachments: ComposerAttachment[]
  optimisticMessageId: string
  options?: SubmitTextOptions
  payload: null | PromptSubmitPayload
  runtimeId: null | string
  storedSessionId: null | string | undefined
  text: string
}

/**
 * Preserve a send another device's ownership refused, and name the error card it paints.
 *
 * The composer cleared the draft and the attachment chips BEFORE dispatching, so once the
 * gateway refuses SESSION_NOT_OWNED this is where the user's message survives until they decide
 * (store/session-move.ts). The card id is addressable so a later "Move here" that lands removes
 * THIS card rather than sweeping the transcript; a replay refused AGAIN reuses the card its own
 * attempt already owns instead of stacking a second one. Only an ownership refusal may reuse that
 * row — any other failure is a different ending and the attempt still points at the card.
 */
export function recordOwnershipRefusal(error: unknown, send: RefusedSubmit): string {
  const notOwned = isSessionNotOwnedError(error)
  const options = send.options
  const errorMessageId = (notOwned && options?.moveErrorMessageId) || `assistant-error-${Date.now()}`

  if (notOwned && send.storedSessionId) {
    // Identities are minted on the first refusal and reused by every retry of THIS message: the
    // attempt id is what completion compare-and-deletes on, the send id is what makes the gateway
    // recognise a replayed acceptance.
    rememberRefusedSend({
      attachments: send.attachments,
      displayKind: options?.displayKind,
      displayText: options?.displayText,
      errorMessageId,
      optimisticMessageId: send.optimisticMessageId,
      ownership: sessionOwnershipFromError(error),
      runtimeId: send.runtimeId,
      storedSessionId: send.storedSessionId,
      text: send.text,
      ...(send.payload && { payload: send.payload }),
      ...(options?.moveAttemptId && { attemptId: options.moveAttemptId }),
      ...(options?.clientSendId && { sendId: options.clientSendId })
    })
  }

  return errorMessageId
}

/**
 * Everything a submit MEANS is digest-bound to its send id, so a "Move here" resend replays the
 * payload its refusal was built from rather than rebuilding it: by the time the user clicks, the
 * HUD that set `surface: 'hud'` has closed and the composer that supplied `title_preview` has been
 * emptied, and a payload that differs by one field collides with its own id and can never be sent.
 * Only this attempt's own `submission_id` is fresh (the gateway leaves it out of the digest).
 */
export function replayablePayload(
  replay: PromptSubmitPayload | undefined,
  submissionId: string,
  fresh: () => PromptSubmitPayload
): PromptSubmitPayload {
  return replay ? { ...replay, submission_id: submissionId } : fresh()
}

/**
 * The keys the gateway does NOT digest, applied per attempt: the runtime target (re-minted by
 * every reconnect), the images staged during THIS attempt (transport-lifetime evidence consumed
 * when the durable ledger answers an accepted replay) and the send id that makes a lost
 * acceptance replay instead of becoming a second turn.
 */
export function attemptSubmitParams(
  payload: PromptSubmitPayload,
  targetId: string,
  stagedImagePaths: string[] | undefined,
  clientSendId: string | undefined
) {
  return {
    ...payload,
    ...(stagedImagePaths?.length && { staged_attachment_paths: stagedImagePaths }),
    ...(clientSendId && { client_send_id: clientSendId }),
    session_id: targetId
  }
}

/**
 * The send to hold against a refusal that does NOT come back as the submit's rejection. The
 * gateway re-proves ownership at turn admission, and a submit that staged an attachment first is
 * routinely accepted and then refused there — arriving as a stream error after the composer has
 * already cleared the draft and the chips. It carries the row it already owns, so the replay
 * updates that bubble instead of painting another one.
 */
export function heldInFlightSend(send: RefusedSubmit): RefusedSend {
  const options = send.options

  return {
    attachments: send.attachments,
    displayKind: options?.displayKind,
    displayText: options?.displayText,
    optimisticMessageId: send.optimisticMessageId,
    ownership: null,
    runtimeId: send.runtimeId,
    storedSessionId: send.storedSessionId ?? '',
    text: send.text,
    ...(send.payload && { payload: send.payload }),
    ...(options?.moveAttemptId && { attemptId: options.moveAttemptId }),
    ...(options?.moveErrorMessageId && { errorMessageId: options.moveErrorMessageId }),
    ...(options?.clientSendId && { sendId: options.clientSendId })
  }
}

/** Recovery re-minted the runtime: move the hold to the new key, keeping its identities. */
export function rekeyHeldSend(
  fromRuntimeId: null | string,
  toRuntimeId: string,
  held: RefusedSend,
  attempt: null | PendingSessionMove
): null | PendingSessionMove {
  forgetInFlightSend(fromRuntimeId, attempt?.attemptId)

  return rememberInFlightSend(toRuntimeId, {
    ...held,
    runtimeId: toRuntimeId,
    ...(attempt && { attemptId: attempt.attemptId, sendId: attempt.sendId })
  })
}

/**
 * A landed move answers the ownership card its refusal painted. Removed by the id the attempt
 * recorded, never by "every SESSION_NOT_OWNED card": an older message's card is still holding
 * that message unsent. A card that had already streamed content keeps it and loses only the refusal.
 */
export function answerMoveErrorCard(state: ClientSessionState, cardId: string): ClientSessionState {
  return {
    ...state,
    messages: state.messages.flatMap(message => {
      if (message.id !== cardId || message.errorSurface?.code !== 'SESSION_NOT_OWNED') {
        return [message]
      }

      if (!message.parts.length) {
        return []
      }

      const { error: _error, errorSurface: _errorSurface, ...kept } = message

      return [kept]
    })
  }
}

/** A replay refused again repaints the card its attempt already owns; every other refusal appends its own. */
export function upsertRefusalCard(messages: ChatMessage[], card: ChatMessage): ChatMessage[] {
  return messages.some(m => m.id === card.id) ? messages.map(m => (m.id === card.id ? card : m)) : [...messages, card]
}

/** Paint (or repaint) a refused submit's error card and settle the turn it never got. */
export function withRefusalCard(
  state: ClientSessionState,
  error: unknown,
  card: { id: string; message: string; fallback: string; occurredAt: number }
): ClientSessionState {
  const message: ChatMessage = {
    id: card.id,
    role: 'assistant',
    parts: [],
    error: card.message || card.fallback,
    ...refusalErrorSurface(error),
    branchGroupId: state.pendingBranchGroup ?? undefined,
    completedAt: card.occurredAt,
    timestamp: card.occurredAt
  }

  return {
    ...state,
    messages: upsertRefusalCard(state.messages, message),
    busy: false,
    awaitingResponse: false,
    pendingBranchGroup: null,
    sawAssistantPayload: true,
    // The failed submit's clock seed dies with the turn it never got.
    turnStartedAt: null
  }
}
