import { useEffect, useRef } from 'react'

import { clearSessionMoveDeps, type SessionMoveDeps, setSessionMoveDeps } from '@/store/session-move'

import type { useSubmitPrompt } from './submit'
import { type GatewayRequest, withSessionNotFoundResume } from './utils'

type SubmitPromptText = ReturnType<typeof useSubmitPrompt>

export function useSessionMoveWiring(requestGateway: GatewayRequest, submitPromptText: SubmitPromptText): void {
  // "Move here" for a chat another device owns. The error card triggers it; this
  // layer supplies the two capabilities it cannot reach on its own — the gateway
  // client and the submit pipeline — the same inversion as sessionTileDelegate.
  //
  // The claim goes through withSessionNotFoundResume so a runtime id this device
  // minted before a reconnect is re-resumed rather than failing the move, and the
  // resend names the STORED session so the pipeline continues that conversation
  // instead of minting a contextless new one.
  //
  // The wiring object is installed once and is this hook's ownership token: the
  // registry removes only the object it is handed, so an older tree unmounting
  // after a newer one mounted (the ordinary overlap of a profile swap) can no
  // longer unwire the live app. Its methods read the CURRENT gateway client and
  // submit pipeline out of a ref rather than closing over them, so a stable
  // identity costs nothing in freshness.
  const sessionMoveLive = useRef({ requestGateway, submitPromptText })
  // Latest-callback ref, written during render: a click can only land after a commit.
  sessionMoveLive.current = { requestGateway, submitPromptText }

  const sessionMoveDeps = useRef<null | SessionMoveDeps>(null)

  if (!sessionMoveDeps.current) {
    sessionMoveDeps.current = {
      claim: async ({ generation, leaseId, runtimeId, storedSessionId }) => {
        const { requestGateway: live } = sessionMoveLive.current

        const { result } = await withSessionNotFoundResume(
          runtimeId || storedSessionId,
          storedSessionId,
          liveId =>
            live<{ generation: number; status: string }>('session.handoff.claim', {
              expected_generation: generation,
              expected_lease_id: leaseId,
              session_id: liveId
            }),
          { requestGateway: live }
        )

        return result
      },
      // The attempt's own identities go back out with it: the send id so the
      // gateway recognises a replayed acceptance, the attempt id so a SECOND
      // refusal updates this entry instead of queueing the message twice, and the
      // captured payload so the replay is the message the send id was minted for
      // rather than one rebuilt from whatever the UI looks like now.
      //
      // The two RENDERER identities travel with it for the same reason: the replay
      // updates the bubble the refusal already painted rather than appending a
      // second copy of the user's message, and removes that refusal's own error
      // card rather than every ownership card in the transcript.
      resend: pending =>
        sessionMoveLive.current.submitPromptText(pending.text, {
          attachments: pending.attachments,
          clientSendId: pending.sendId,
          moveAttemptId: pending.attemptId,
          ...(pending.optimisticMessageId && { moveOptimisticId: pending.optimisticMessageId }),
          ...(pending.errorMessageId && { moveErrorMessageId: pending.errorMessageId }),
          ...(pending.displayKind === 'hidden' && { displayKind: 'hidden' as const }),
          ...(pending.displayText && { displayText: pending.displayText }),
          ...(pending.payload && { replayPayload: pending.payload }),
          storedSessionId: pending.storedSessionId
        })
    }
  }

  useEffect(() => {
    const wiring = sessionMoveDeps.current!

    setSessionMoveDeps(wiring)

    // Drop OUR wiring when this hook goes away (a window closing, a profile swap
    // remounting the tree). A stale closure here would claim and resend through a
    // gateway client the app has already replaced.
    return () => clearSessionMoveDeps(wiring)
  }, [])
}
