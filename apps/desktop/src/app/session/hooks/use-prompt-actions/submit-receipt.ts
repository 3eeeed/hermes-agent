import type { ClientSessionState } from '../../../types'

/**
 * A deduplicated (`terminal`) or lost-mid-turn (`unknown`) admission starts no turn and promises no
 * terminal event. A `terminal` dedupe removes our duplicate bubble; an `unknown` one keeps it (the
 * pending fanout's lost card offers Discard). Either way any live turn an owner event established
 * while the receipt was in flight is preserved.
 */
export function settleNonStartingReceipt(
  state: ClientSessionState,
  status: 'terminal' | 'unknown',
  optimisticId: string
): ClientSessionState {
  return {
    ...state,
    messages: status === 'unknown' ? state.messages : state.messages.filter(message => message.id !== optimisticId),
    ...(!state.turnLive &&
      !state.streamId &&
      !state.sawAssistantPayload && {
        busy: false,
        awaitingResponse: false,
        pendingBranchGroup: null,
        turnStartedAt: null
      })
  }
}

/** True when the receipt names a durable user row the optimistic bubble can bind to. */
export function isReceiptRowId(rowId: unknown): rowId is number {
  return typeof rowId === 'number' && Number.isSafeInteger(rowId) && rowId > 0
}

/**
 * The worker may finish before the acknowledgement arrives. Bind only this send's optimistic
 * occurrence; never reset live state or assume the newest user row still belongs to this RPC.
 */
export function bindOptimisticRowId(state: ClientSessionState, optimisticId: string, rowId: number): ClientSessionState {
  const index = state.messages.findIndex(message => message.id === optimisticId && message.role === 'user')

  if (index < 0 || state.messages[index].rowId === rowId) {
    return state
  }

  return {
    ...state,
    messages: state.messages.map((message, i) => (i === index ? { ...message, rowId } : message))
  }
}
