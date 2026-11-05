import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider, TRANSLATIONS } from '@/i18n'
import { $interfaceMode } from '@/store/interface-mode'
import { $sidebarUnfinishedFilter, resetSidebarView } from '@/store/layout'
import { makeSessionInfo } from '@/test/session-info'

import { SidebarFilterMenu } from './filter-menu'
import { UnfinishedWorkDot } from './unfinished-work'

afterEach(() => {
  cleanup()
  resetSidebarView()
})

describe('UnfinishedWorkDot', () => {
  it('speaks the Egyptian Arabic tooltip with the dirty file count', () => {
    const session = makeSessionInfo({ id: 's', unfinished: { dirty_count: 3, repos: ['/r'] } })

    render(
      <I18nProvider configClient={null} initialLocale="ar">
        <UnfinishedWorkDot session={session} />
      </I18nProvider>
    )

    expect(screen.getByRole('img', { name: 'فيه 3 ملف متعدل من غير كوميت' })).toBeTruthy()
  })

  it('renders nothing for a clean session or an older backend without the field', () => {
    const { container } = render(
      <>
        <UnfinishedWorkDot session={makeSessionInfo({ id: 'a', unfinished: null })} />
        <UnfinishedWorkDot session={makeSessionInfo({ id: 'b' })} />
      </>
    )

    expect(container.querySelector('[data-unfinished-dot]')).toBeNull()
  })
})

describe('Filter menu unfinished option', () => {
  it('toggles the persisted unfinished-only filter and reset clears it', () => {
    $interfaceMode.set('advanced')
    render(
      <I18nProvider configClient={null} initialLocale="ar">
        <SidebarFilterMenu />
      </I18nProvider>
    )
    const ar = TRANSLATIONS.ar.sidebar.filter

    expect(ar.unfinished).toBe('مخلصتش')
    fireEvent.keyDown(screen.getByRole('button', { name: ar.filters }), { key: 'Enter' })
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: ar.unfinished }))

    expect($sidebarUnfinishedFilter.get()).toBe(true)
    expect(screen.getByRole('menuitemcheckbox', { name: ar.unfinished }).getAttribute('aria-checked')).toBe('true')

    resetSidebarView()

    expect($sidebarUnfinishedFilter.get()).toBe(false)
  })
})
