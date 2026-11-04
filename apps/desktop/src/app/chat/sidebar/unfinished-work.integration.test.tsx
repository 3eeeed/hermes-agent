// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $sidebarFiltersActive, $sidebarUnfinishedFilter, resetSidebarView } from '@/store/layout'
import { $sessions, $sessionsLoading } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { ChatSidebar } from './index'

const noop = () => {}

const noopAsync = async () => {}

const dirty = makeSessionInfo({
  id: 'dirty',
  last_active: 3,
  profile: 'default',
  started_at: 1,
  title: 'SEO rollout',
  unfinished: { dirty_count: 18, repos: ['/srv/workspaces/impact'] }
})

const clean = makeSessionInfo({ id: 'clean', last_active: 2, profile: 'default', started_at: 1, title: 'Committed' })

const renderSidebar = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={noopAsync}
          onTriggerCronJob={noopAsync}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

describe('ChatSidebar unfinished work', () => {
  beforeEach(() => {
    $sessions.set([dirty, clean])
    $sessionsLoading.set(false)
  })

  afterEach(() => {
    cleanup()
    resetSidebarView()
    $sessions.set([])
    $sessionsLoading.set(true)
  })

  it('marks only the row whose files are uncommitted', () => {
    renderSidebar()

    const marks = screen.getAllByRole('img', { name: '18 files changed without a commit' })

    expect(marks).toHaveLength(1)
    expect(marks[0].closest('.group.row-hover')?.textContent).toContain('SEO rollout')
  })

  it('narrows the list to unfinished sessions from the header toggle', () => {
    renderSidebar()

    expect(screen.getByText('Committed')).toBeTruthy()
    const toggle = screen.getByRole('button', { name: 'Unfinished' })
    expect(toggle.getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(toggle)

    expect($sidebarUnfinishedFilter.get()).toBe(true)
    expect($sidebarFiltersActive.get()).toBe(true)
    expect(screen.getByRole('button', { name: 'Unfinished' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('SEO rollout')).toBeTruthy()
    expect(screen.queryByText('Committed')).toBeNull()
  })

  it('hides the toggle while nothing is unfinished, but keeps it while the filter is on', () => {
    $sessions.set([clean])
    renderSidebar()

    expect(screen.queryByRole('button', { name: 'Unfinished' })).toBeNull()
    cleanup()

    $sidebarUnfinishedFilter.set(true)
    renderSidebar()

    expect(screen.getByRole('button', { name: 'Unfinished' })).toBeTruthy()
    expect(screen.queryByText('Committed')).toBeNull()
  })
})
