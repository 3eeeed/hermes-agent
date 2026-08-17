import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AppContextMenu } from './app-context-menu'
import { $contextMenu } from './store'
import { resolveDomTarget } from './target'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

function installBridge(partial: Partial<NonNullable<Window['hermesDesktop']>> = {}) {
  desktopWindow.hermesDesktop = {
    openExternal: vi.fn().mockResolvedValue(undefined),
    writeClipboard: vi.fn().mockResolvedValue(true),
    ...partial
  } as unknown as Window['hermesDesktop']
}

function attach(html: string): HTMLElement {
  const host = document.createElement('div')

  host.innerHTML = html
  document.body.appendChild(host)

  return host
}

function mountMenu() {
  return render(
    <MemoryRouter>
      <AppContextMenu />
    </MemoryRouter>
  )
}

const descriptor = JSON.stringify({ kind: 'local', name: 'report.pdf', source: '/tmp/report.pdf' })

afterEach(() => {
  $contextMenu.set(null)
  cleanup()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
  delete desktopWindow.hermesDesktop
})

describe('context menu file actions', () => {
  it('resolves the chat file descriptor around the clicked element', () => {
    const host = attach(`<span data-hermes-context-file='${descriptor}'><b>report</b></span>`)

    expect(resolveDomTarget(host.querySelector('b')).contextFile).toBe(descriptor)
  })

  it('offers file verbs on a chat file and sends the descriptor to main', async () => {
    const contextMenuFileAction = vi.fn().mockResolvedValue({ ok: true })
    installBridge({ contextMenuFileAction })
    mountMenu()
    const host = attach(`<span data-hermes-context-file='${descriptor}'>report.pdf</span>`)

    fireEvent.contextMenu(host.firstElementChild!)

    expect(await screen.findByText('Open file')).toBeTruthy()
    expect(screen.getByText('Copy path')).toBeTruthy()
    expect(screen.getByText('Copy file')).toBeTruthy()

    fireEvent.click(screen.getByText('Copy path'))

    await waitFor(() =>
      expect(contextMenuFileAction).toHaveBeenCalledWith({ action: 'copy-path', descriptor })
    )
  })

  it('replaces browser link verbs with file verbs on a file: link', async () => {
    const contextMenuFileAction = vi.fn().mockResolvedValue({ ok: true })
    installBridge({ contextMenuFileAction })
    mountMenu()
    const host = attach('<a href="file:///tmp/report.pdf">report</a>')

    fireEvent.contextMenu(host.querySelector('a')!)

    expect(await screen.findByText('Open file')).toBeTruthy()
    expect(screen.queryByText('Open in external browser')).toBeNull()
  })

  it('keeps the plain menu when the host has no file bridge', async () => {
    installBridge()
    mountMenu()
    const host = attach(`<a data-hermes-context-file='${descriptor}' href="https://example.com/r">report</a>`)

    fireEvent.contextMenu(host.querySelector('a')!)

    expect(await screen.findByText('Open in external browser')).toBeTruthy()
    expect(screen.queryByText('Open file')).toBeNull()
  })
})
