import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sidebarShowArchived } from '@/store/layout'
import { $changeEventsAvailable, $cronChangeTick, $sessionsChangeTick } from '@/store/live-sync'
import { $activeSessionId } from '@/store/session'
import { loadArchivedSessions } from '@/store/sidebar-archive'

import { useBackgroundSync } from './use-background-sync'

vi.mock('@/store/sidebar-archive', () => ({
  loadArchivedSessions: vi.fn()
}))

const noop = () => undefined
const requestGateway = async () => ({ sessions: [] })

function render(
  activeGatewayProfile: string,
  activeConnectionId: string,
  refreshSessions: () => Promise<void>,
  gatewayRequest = requestGateway
) {
  return renderHook(
    ({ connectionId, profile }: { connectionId: string; profile: string }) => {
      useBackgroundSync({
        activeConnectionId: connectionId,
        activeGatewayProfile: profile,
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        refreshActiveTranscript: noop,
        refreshCronJobs: noop,
        refreshCurrentModel: noop,
        refreshHermesConfig: noop,
        refreshMessagingSessions: noop,
        refreshSessions,
        requestGateway: gatewayRequest
      })
    },
    { initialProps: { connectionId: activeConnectionId, profile: activeGatewayProfile } }
  )
}

describe('useBackgroundSync profile-scoped session refresh', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    $activeSessionId.set(null)
    $changeEventsAvailable.set(false)
    $cronChangeTick.set(0)
    $sessionsChangeTick.set(0)
    $sidebarShowArchived.set(false)
    vi.mocked(loadArchivedSessions).mockReset()
    // Change-driven refreshes only run for a window the user is viewing; jsdom's
    // document.hasFocus() is not reliably true, so pin it.
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('coalesces change ticks while the live status request is pending', async () => {
    $changeEventsAvailable.set(true)
    let release!: (value: { sessions: [] }) => void

    const pending = new Promise<{ sessions: [] }>(resolve => {
      release = resolve
    })

    const request = vi.fn(() => pending)
    render('default', 'local', async () => undefined, request)
    await act(async () => undefined)

    for (let tick = 1; tick <= 8; tick += 1) {
      await act(async () => {
        $sessionsChangeTick.set(tick)
      })
    }

    expect(request).toHaveBeenCalledTimes(1)
    await act(async () => {
      release({ sessions: [] })
    })
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('refreshes the session list after the active gateway profile changes', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', 'local', refreshSessions)

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
    refreshSessions.mockClear()

    hook.rerender({ connectionId: 'local', profile: 'nova' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('refreshes the session list when the backend changes but the profile name does not', async () => {
    const refreshSessions = vi.fn(async () => undefined)
    const hook = render('default', 'work', refreshSessions)

    await act(async () => undefined)
    refreshSessions.mockClear()

    hook.rerender({ connectionId: 'homelab', profile: 'default' })

    await act(async () => undefined)
    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('reloads archived sessions after an external session change while the Archived view is open', async () => {
    $changeEventsAvailable.set(true)
    $sidebarShowArchived.set(true)
    render('default', 'local', async () => undefined)

    await act(async () => {
      $sessionsChangeTick.set(1)
    })

    expect(loadArchivedSessions).toHaveBeenCalledTimes(1)
  })
  it('holds an external session change for an unviewed window and catches up once on focus', async () => {
    $changeEventsAvailable.set(true)
    $sidebarShowArchived.set(true)
    const hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    render('default', 'local', async () => undefined)

    await act(async () => {
      $sessionsChangeTick.set(1)
      $sessionsChangeTick.set(2)
    })
    expect(loadArchivedSessions).not.toHaveBeenCalled()

    hasFocus.mockReturnValue(true)
    await act(async () => {
      window.dispatchEvent(new Event('focus'))
    })

    expect(loadArchivedSessions).toHaveBeenCalledTimes(1)
  })
})
