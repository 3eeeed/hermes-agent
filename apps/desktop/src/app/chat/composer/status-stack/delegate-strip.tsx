import { useState } from 'react'

import { StatusSection } from '@/components/chat/status-section'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { useI18n } from '@/i18n'
import { $delegateBySession, clearDelegateStatus } from '@/store/delegate-status'
import { requestForOwnedSession } from '@/store/session-states'
import { $subagentsBySession } from '@/store/subagents'

import { useSessionValue } from './session-control-utils'

interface DelegateStripProps {
  sessionId: string
}

/** One line above the composer: where a Delegate turn stands, and a way to stop its worker. */
export function DelegateStrip({ sessionId }: DelegateStripProps) {
  const { t } = useI18n()
  const progress = useSessionValue($delegateBySession, sessionId)
  const [stopping, setStopping] = useState(false)

  if (!progress) {
    return null
  }

  const copy = t.statusStack.delegate
  const attempt = copy.attempt(Math.max(1, progress.attempt), progress.maxAttempts)

  const label = {
    planning: copy.planning,
    worker: `${copy.worker(progress.lane)} · ${attempt}`,
    review: `${copy.review} · ${attempt}`,
    done: copy.done,
    interrupted: copy.interrupted
  }[progress.phase]

  const busy = progress.phase === 'planning' || progress.phase === 'worker' || progress.phase === 'review'

  const stopWorker = async () => {
    // Stop only this session's running lane worker; the orchestrator then reports what landed.
    const running = ($subagentsBySession.get()[sessionId] ?? []).filter(item => item.status === 'running')
    setStopping(true)

    try {
      await Promise.all(
        running.map(item =>
          requestForOwnedSession(
            sessionId,
            async () => {
              throw new Error(t.agents.requestRejected)
            },
            'subagent.interrupt',
            { session_id: sessionId, subagent_id: item.id }
          ).catch(() => undefined)
        )
      )
    } finally {
      setStopping(false)
    }
  }

  const accessory =
    progress.phase === 'worker' ? (
      <Button disabled={stopping} onClick={() => void stopWorker()} size="micro" type="button" variant="text">
        {copy.stopWorker}
      </Button>
    ) : progress.phase === 'interrupted' ? (
      <Button onClick={() => clearDelegateStatus(sessionId)} size="micro" type="button" variant="text">
        {t.statusStack.dismiss}
      </Button>
    ) : undefined

  return (
    <div className="composer-no-drag min-w-0" data-phase={progress.phase} data-slot="composer-delegate">
      <StatusSection
        accessory={accessory}
        collapsedIndicator={
          busy ? (
            <GlyphSpinner ariaLabel={label} className="text-(--ui-purple)" spinner="braille" />
          ) : undefined
        }
        icon={
          <Codicon
            className={progress.phase === 'interrupted' ? 'text-(--ui-yellow)' : 'text-(--ui-purple)'}
            name={progress.phase === 'interrupted' ? 'warning' : 'organization'}
            size="0.8rem"
          />
        }
        label={label}
      >
        {progress.phase === 'interrupted' ? (
          <p className="px-2 pb-2 text-xs text-(--ui-text-secondary)">{copy.interruptedDetail}</p>
        ) : (
          <p className="px-2 pb-2 text-xs text-(--ui-text-secondary)">{copy.changed(progress.changedCount)}</p>
        )}
      </StatusSection>
    </div>
  )
}
