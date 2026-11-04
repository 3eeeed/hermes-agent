import { useStore } from '@nanostores/react'
import { useCallback, useMemo } from 'react'

import type { SessionInfo } from '@/hermes'
import {
  $sidebarPrFilter,
  $sidebarProfileFilter,
  $sidebarStatusFilter,
  $sidebarUnfinishedFilter
} from '@/store/layout'
import { normalizeProfileKey } from '@/store/profile'
import { $pullRequestsByBranch, pullRequestBucket, sessionPrKey } from '@/store/pull-requests'
import { $sessionDotStateById, sessionStatusBucket } from '@/store/session-dot-state'

import { sessionMatchesProjectFilter } from './projects'
import { unfinishedCount } from './unfinished-work'

interface SessionFilterScope {
  projectFilter: readonly string[]
  projectOwners: Parameters<typeof sessionMatchesProjectFilter>[3]
  projects: Parameters<typeof sessionMatchesProjectFilter>[2]
  scopedSessions: SessionInfo[]
  showAllProfiles: boolean
}

/** The sidebar's row filters as ONE predicate, so the flat list and the
 *  project lanes narrow by the same rule. A project lane holds rows the loaded
 *  page may not, so it has to be answerable per session rather than by
 *  membership in the filtered set. Detached rows file under the Home bucket id
 *  (same rule as the overview overlay), so filtering to Home keeps Home's rows. */
export function useSessionFilters({
  projectFilter,
  projectOwners,
  projects,
  scopedSessions,
  showAllProfiles
}: SessionFilterScope) {
  const statusFilter = useStore($sidebarStatusFilter)
  const profileFilter = useStore($sidebarProfileFilter)
  const prFilter = useStore($sidebarPrFilter)
  const unfinishedOnly = useStore($sidebarUnfinishedFilter)
  const pullRequests = useStore($pullRequestsByBranch)
  const dotStates = useStore($sessionDotStateById)

  const sessionMatchesFilters = useCallback(
    (session: SessionInfo) => {
      if (statusFilter.length && !statusFilter.includes(sessionStatusBucket(dotStates[session.id]))) {
        return false
      }

      // Files this session wrote are still uncommitted (backend scan).
      if (unfinishedOnly && unfinishedCount(session) <= 0) {
        return false
      }

      // Narrowing to a few of the profiles on screen. Scoped to one profile the
      // list is already that profile's, so a stale selection can't blank it.
      if (showAllProfiles && profileFilter.length && !profileFilter.includes(normalizeProfileKey(session.profile))) {
        return false
      }

      if (prFilter.length) {
        const key = sessionPrKey(session)

        if (!prFilter.includes(pullRequestBucket(key ? pullRequests[key] : undefined))) {
          return false
        }
      }

      // Same membership the sidebar groups and colors by (backend owner first,
      // cwd walk otherwise), so a filtered row lands in the lane the user
      // picked it from.
      return sessionMatchesProjectFilter(session, projectFilter, projects, projectOwners)
    },
    [
      statusFilter,
      unfinishedOnly,
      projectFilter,
      profileFilter,
      showAllProfiles,
      prFilter,
      pullRequests,
      projects,
      projectOwners,
      dotStates
    ]
  )

  const filtersNarrow =
    unfinishedOnly ||
    statusFilter.length > 0 ||
    projectFilter.length > 0 ||
    prFilter.length > 0 ||
    (showAllProfiles && profileFilter.length > 0)

  // Offer the header toggle only while some row can answer it.
  const anyUnfinished = useMemo(() => scopedSessions.some(session => unfinishedCount(session) > 0), [scopedSessions])

  return { anyUnfinished, filtersNarrow, sessionMatchesFilters }
}
