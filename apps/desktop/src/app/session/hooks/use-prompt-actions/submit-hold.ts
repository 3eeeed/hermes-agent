import type { ComposerAttachment } from '@/store/composer'
import {
  forgetInFlightSend,
  type PendingSessionMove,
  type PromptSubmitPayload,
  type RefusedSend,
  rememberInFlightSend
} from '@/store/session-move'

import { heldInFlightSend, recordOwnershipRefusal, rekeyHeldSend } from './ownership-refusal'
import type { SubmitTextOptions } from './utils'

interface SubmitHoldInit {
  attachments: ComposerAttachment[]
  optimisticMessageId: string
  options?: SubmitTextOptions
  text: string
}

/**
 * One submit's "Move here" bookkeeping (ownership-refusal.ts): what a refusal preserves (the staged
 * attachments and, once built, the digest-bound payload — null before then, so the resend rebuilds
 * like a first send) and the in-flight hold a later stream-error refusal finds by runtime id.
 */
export function createSubmitHold(init: SubmitHoldInit, initialRuntimeId: null | string) {
  const send = {
    ...init,
    payload: null as null | PromptSubmitPayload
  }

  let attempt: null | PendingSessionMove = null
  let heldRuntimeId = initialRuntimeId
  let held: null | RefusedSend = null

  return {
    send,
    /** Hold the send against a refusal that arrives later as a stream error. */
    hold(runtimeId: null | string, storedSessionId: null | string | undefined) {
      held = heldInFlightSend({ ...send, runtimeId, storedSessionId })
      attempt = runtimeId ? rememberInFlightSend(runtimeId, held) : null
    },
    /** Recovery re-minted the runtime: a stream refusal will now name the new one. */
    rekey(recoveredId: string) {
      if (held) {
        attempt = rekeyHeldSend(heldRuntimeId, recoveredId, held, attempt)
      }

      heldRuntimeId = recoveredId
    },
    /**
     * The gateway admitted it. Tokenized chips match across staging clones; legacy chips match by
     * exact object identity, so a newer same-id replacement is preserved while the staged object for
     * a submitted file is removed. A "Move here" replay owns the chips its refusal put back, and
     * answers the ownership card that refusal painted.
     */
    accepted(on: {
      ownsChips: boolean
      removeAttachments: (attachments: readonly ComposerAttachment[]) => void
      answerCard: (cardId: string) => void
    }) {
      const options = send.options

      if (on.ownsChips || options?.moveAttemptId) {
        on.removeAttachments(send.attachments)
      }

      if (options?.moveErrorMessageId) {
        on.answerCard(options.moveErrorMessageId)
      }
    },
    /** Failed in the open: recorded by `refuse`, never left held for a later turn's refusal. */
    release() {
      forgetInFlightSend(heldRuntimeId, attempt?.attemptId)
    },
    /** Preserve a refused send for "Move here"; returns the error card id to paint. */
    refuse(error: unknown, runtimeId: null | string, storedSessionId: null | string | undefined): string {
      return recordOwnershipRefusal(error, { ...send, runtimeId, storedSessionId })
    }
  }
}

/** A "Move here" replay updates the bubble its refused attempt painted (ownership-refusal.ts). */
export function optimisticMessageId(options: SubmitTextOptions | undefined, submissionId: string): string {
  return options?.moveOptimisticId ?? `user-${submissionId}`
}
