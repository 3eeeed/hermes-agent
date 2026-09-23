import { isSessionNotOwnedError } from '@/app/session/hooks/use-prompt-actions/utils'
import { type ErrorSurface, parseErrorSurface } from '@/lib/error-surface'
import {
  armRefusedSendFromTurn,
  completeInFlightSend,
  noteSessionMoveErrorMessage,
  type PendingSessionMove
} from '@/store/session-move'

/**
 * The card surface for a session-level `error` event. A backend that stamps the refusal itself
 * is believed over the text: only the wire block carries the live owner's compare-and-swap
 * precondition, which is what lets the card offer the move rather than only "start a new chat".
 * A live-owner refusal (SESSION_NOT_OWNED, #106217) is deterministic and disk-full is a machine
 * problem, so neither is retryable.
 */
export function statusErrorSurface(
  wireSurface: unknown,
  errorMessage: string,
  isDiskFull: (message: string) => boolean
): ErrorSurface | null {
  const stamped = parseErrorSurface(wireSurface)

  if (stamped) {
    return stamped
  }

  if (isSessionNotOwnedError(new Error(errorMessage))) {
    return { code: 'SESSION_NOT_OWNED', layer: 'gateway', retryable: false }
  }

  return isDiskFull(errorMessage) ? { code: 'disk_full', layer: 'disk', retryable: false } : null
}

/**
 * The fence refused this turn AFTER accepting its submit, so the send never reached the chat and
 * exists only in the pipeline's in-flight slot. Promote it to a movable attempt — keyed by the
 * stored chat it targeted — so the card can finish it with the exact text and attachments instead
 * of stranding the user's message. Any other terminal error retires only the turn's oldest hold:
 * a later queued send may already be waiting on the same runtime and needs its own envelope.
 */
export function settleInFlightSendForError(
  sessionId: null | string | undefined,
  surface: ErrorSurface | null
): null | PendingSessionMove {
  if (!sessionId) {
    return null
  }

  if (surface?.code === 'SESSION_NOT_OWNED') {
    return armRefusedSendFromTurn(sessionId, surface.ownership ?? null)
  }

  completeInFlightSend(sessionId)

  return null
}

/** Bind the card just painted to the attempt armed for it. The move that finishes the send then
 *  removes THAT row and leaves any older ownership card — still the only way to send the message
 *  it is holding — exactly where it is. */
export function bindMoveToErrorCard(armedMove: null | PendingSessionMove, errorMessageId: null | string | undefined) {
  if (armedMove && errorMessageId) {
    noteSessionMoveErrorMessage(armedMove.storedSessionId, armedMove.attemptId, errorMessageId)
  }
}
