import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { PooledAccountsController } from '@/app/shell/hooks/use-pooled-accounts'
import { PooledAccountsMenu } from '@/app/shell/pooled-accounts-menu'
import type { CredentialPoolEntry, CredentialPoolUsageEntry } from '@/types/hermes'

function controllerWith(
  usage: CredentialPoolUsageEntry | null,
  usageLoading: boolean,
  overrides: Partial<PooledAccountsController> = {}
): PooledAccountsController {
  const entry = { id: 'acct-1', label: 'one' } as unknown as CredentialPoolEntry

  return {
    accounts: [entry],
    activeCredentialId: null,
    addError: null,
    addSession: null,
    canAddAccounts: true,
    cancelAdd: () => {},
    confirmDelete: async () => {},
    deleteCandidate: null,
    deleteError: null,
    deleting: false,
    labelFor: () => 'one',
    pasteCode: '',
    renameCandidate: null,
    renameError: null,
    renaming: false,
    select: () => {},
    setDeleteCandidate: () => {},
    setDeleteError: () => {},
    setPasteCode: () => {},
    setRenameCandidate: () => {},
    startAdd: async () => {},
    submitPastedCode: async () => {},
    submitRename: async () => {},
    usageById: usage ? new Map([[usage.id, usage]]) : new Map(),
    usageLoading,
    ...overrides
  }
}

describe('PooledAccountsMenu account row status', () => {
  it('shows the pending placeholder only while quota has not arrived', () => {
    render(<PooledAccountsMenu controller={controllerWith(null, true)} focusedStoredSessionId={null} />)

    expect(screen.getByText('Checking…')).toBeTruthy()
  })

  // Anthropic answers with real quota windows but no plan name. The row must
  // report the arrived quota, never stay on the pending placeholder.
  it('drops the placeholder once usage arrives without a plan name', () => {
    const usage: CredentialPoolUsageEntry = {
      available: true,
      id: 'acct-1',
      label: 'one',
      plan: null,
      windows: [{ detail: null, label: 'Current week', reset_at: null, used_percent: 88 }]
    }

    render(<PooledAccountsMenu controller={controllerWith(usage, false)} focusedStoredSessionId={null} />)

    expect(screen.queryByText('Checking…')).toBeNull()
    expect(screen.getByText('12% left')).toBeTruthy()
  })
})

describe('PooledAccountsMenu renaming', () => {
  // The edit starts from the real label: the usual rename appends to what is
  // already there, and an empty box would make the user retype it.
  it('opens the rename form seeded with the current label', () => {
    const setRenameCandidate = vi.fn()

    render(
      <PooledAccountsMenu
        controller={controllerWith(null, false, { setRenameCandidate })}
        focusedStoredSessionId={null}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Rename one' }))

    expect(setRenameCandidate).toHaveBeenCalledWith({ id: 'acct-1', label: 'one' })
  })

  // Selecting the account and editing its name are different intents; the
  // rename controls sit inside the row, so a click must not also re-pin the
  // chat to that account.
  it('does not select the account when its rename button is clicked', () => {
    const select = vi.fn()

    render(
      <PooledAccountsMenu controller={controllerWith(null, false, { select })} focusedStoredSessionId={null} />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Rename one' }))

    expect(select).not.toHaveBeenCalled()
  })

  it('cannot submit a blank label', () => {
    render(
      <PooledAccountsMenu
        controller={controllerWith(null, false, { renameCandidate: { id: 'acct-1', label: '   ' } })}
        focusedStoredSessionId={null}
      />
    )

    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(true)
  })

  it('submits the edited label', () => {
    const submitRename = vi.fn(async () => {})

    render(
      <PooledAccountsMenu
        controller={controllerWith(null, false, {
          renameCandidate: { id: 'acct-1', label: 'Ahmed personal' },
          submitRename
        })}
        focusedStoredSessionId={null}
      />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(submitRename).toHaveBeenCalled()
  })
})
