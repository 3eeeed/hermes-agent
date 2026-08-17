/**
 * The custom (renderer) context menu's main-process half.
 *
 * The app popups no native menus: the renderer owns the menu UI so labels
 * are translated with the rest of the app. Main keeps only what Chromium
 * reports here and the renderer cannot see:
 *  - spell-check facts (misspelled word + suggestions) — forwarded so the
 *    renderer appends them to its already-open menu,
 *  - the gesture coordinates — kept for copyImageAt, which needs them.
 */

import type { BrowserWindow, IpcMain } from 'electron'

const lastContextMenuPoint = new Map<number, { x: number; y: number }>()

export function installContextMenuBridge(window: BrowserWindow): void {
  window.webContents.on('context-menu', (_event, params) => {
    lastContextMenuPoint.set(window.webContents.id, { x: params.x, y: params.y })

    const suggestions = Array.isArray(params.dictionarySuggestions) ? params.dictionarySuggestions : []

    if (params.isEditable && params.misspelledWord) {
      window.webContents.send('hermes:context-menu-spellcheck', {
        misspelledWord: params.misspelledWord,
        suggestions
      })
    }
  })
}

export function registerContextMenuIpc(ipcMain: Pick<IpcMain, 'handle'>): void {
  // The custom context menu's edit verbs. They act on the SENDER's focused
  // element, so the renderer restores focus to the editable before invoking.
  ipcMain.handle('hermes:context-menu:edit', (event, command) => {
    const contents = event.sender

    if (command === 'copy') {
      contents.copy()
    } else if (command === 'cut') {
      contents.cut()
    } else if (command === 'paste') {
      contents.paste()
    } else if (command === 'selectAll') {
      contents.selectAll()
    }
  })

  // Copy the image under the sender's LAST context-menu gesture. Chromium only
  // exposes image bytes through copyImageAt, and only main saw the coordinates.
  ipcMain.handle('hermes:context-menu:copy-image', event => {
    const point = lastContextMenuPoint.get(event.sender.id)

    if (point) {
      event.sender.copyImageAt(point.x, point.y)
    }
  })
}
