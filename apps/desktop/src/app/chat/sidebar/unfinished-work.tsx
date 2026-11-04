import { useStore } from '@nanostores/react'

import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $sidebarUnfinishedFilter, toggleSidebarUnfinishedFilter } from '@/store/layout'
import type { SessionInfo } from '@/types/hermes'

/** Files the session wrote that are still uncommitted, per the backend's last
 *  background scan; 0 when clean or unknown. */
export function unfinishedCount(session: Pick<SessionInfo, 'unfinished'>): number {
  return session.unfinished?.dirty_count ?? 0
}

// Hollow amber: amber is the sidebar's "your move" color, and hollow keeps it
// apart from the filled needs-input status dot in the row's lead slot.
const MARK = 'inline-block size-1.5 shrink-0 rounded-full border border-amber-500'

/** The small mark beside a session title: it edited files nobody committed. */
export function UnfinishedWorkDot({ className, session }: { className?: string; session: SessionInfo }) {
  const { t } = useI18n()
  const count = unfinishedCount(session)

  if (count <= 0) {
    return null
  }

  const label = t.sidebar.row.unfinishedWork(count)

  return (
    <Tip label={label}>
      <span aria-label={label} className={cn(MARK, className)} data-unfinished-dot="" role="img" />
    </Tip>
  )
}

/** Sessions-header toggle: show only unfinished sessions. Hidden until some
 *  session is unfinished, but stays while on so the filter can be cleared. */
export function UnfinishedFilterToggle({ available, className }: { available: boolean; className?: string }) {
  const { t } = useI18n()
  const on = useStore($sidebarUnfinishedFilter)

  if (!available && !on) {
    return null
  }

  const label = t.sidebar.filter.unfinished

  return (
    <Tip label={label}>
      <Button
        aria-label={label}
        aria-pressed={on}
        className={cn(className, on && 'bg-(--ui-control-active-background) text-foreground opacity-100')}
        onClick={toggleSidebarUnfinishedFilter}
        size="icon-xs"
        type="button"
        variant="ghost"
      >
        <span aria-hidden="true" className={cn(MARK, on && 'bg-amber-500')} />
      </Button>
    </Tip>
  )
}
