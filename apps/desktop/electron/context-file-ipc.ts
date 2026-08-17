// Main-process half of the chat context menu's file actions.
//
// The renderer owns the menu UI (app-context-menu.tsx) and knows which chat
// file sits under the pointer; only main can materialize that file on disk and
// hand it to the native clipboard, the OS opener or the file manager. The
// renderer sends WHAT was clicked (a file descriptor, or a file link) and
// which action; main re-validates both before touching anything.

import { execFile } from 'node:child_process'

import type { IpcMain } from 'electron'

import { type ContextFileActionDependencies, runContextFileAction } from './context-file-actions'
import {
  contextMenuModelForContextFile,
  contextMenuModelForLink,
  type ContextMenuPlatform,
  type FileLinkContextMenuModel,
  type FileLinkMenuItemId
} from './file-link-context-menu'
import { copyFileToClipboard } from './native-file-clipboard'

const CONTEXT_FILE_CLIPBOARD_COMMAND_TIMEOUT_MS = 15_000

const FILE_ACTIONS = new Set<FileLinkMenuItemId>(['copy-file', 'copy-link', 'copy-path', 'open-file', 'reveal-file'])

export interface ContextFileActionRequest {
  action?: unknown
  /** `data-hermes-context-file` of the clicked element (JSON text). */
  descriptor?: unknown
  /** A plain file link (`file:` / `#media:`) when no descriptor was present. */
  linkUrl?: unknown
}

export type ContextFileActionResult = { ok: true } | { message: string; ok: false }

export interface ContextFileIpcDeps {
  copyFileBuffer: (format: string, data: Buffer) => Promise<void> | void
  copyText: (value: string) => Promise<void> | void
  log: (message: string) => void
  materialize: (model: FileLinkContextMenuModel) => Promise<string>
  openPath: (filePath: string) => Promise<string>
  platform: ContextMenuPlatform
  revealFile: (filePath: string) => void
}

/** Electron 44 dropped `clipboard.writeBuffer`: a raw OS clipboard format is
 *  written through `clipboard.write()` under this custom MIME type. */
export function osClipboardFormat(format: string): string {
  return `electron application/osclipboard;format="${format}"`
}

export function contextMenuPlatform(platform: NodeJS.Platform = process.platform): ContextMenuPlatform {
  return platform === 'darwin' ? 'darwin' : platform === 'win32' ? 'win32' : 'linux'
}

function runClipboardCommand(command: string, args: string[]): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    execFile(command, args, { timeout: CONTEXT_FILE_CLIPBOARD_COMMAND_TIMEOUT_MS, windowsHide: true }, error =>
      error ? reject(error) : resolve()
    )
  })
}

/** Resolve the request into a file model, or null when it names no file. A
 *  web link is never a file target: those keep the ordinary link section. */
export function contextFileModelForRequest(
  request: ContextFileActionRequest,
  platform: ContextMenuPlatform
): FileLinkContextMenuModel | null {
  if (typeof request.descriptor === 'string' && request.descriptor) {
    return contextMenuModelForContextFile(request.descriptor, platform)
  }

  if (typeof request.linkUrl === 'string' && request.linkUrl) {
    const model = contextMenuModelForLink({ linkURL: request.linkUrl }, platform)

    return model.kind === 'web-link' ? null : model
  }

  return null
}

function actionDependencies(deps: ContextFileIpcDeps): ContextFileActionDependencies {
  return {
    copyFile: filePath =>
      copyFileToClipboard(
        filePath,
        deps.platform,
        { runCommand: runClipboardCommand, writeBuffer: deps.copyFileBuffer },
        { desktop: process.env.XDG_CURRENT_DESKTOP }
      ),
    copyText: deps.copyText,
    materialize: deps.materialize,
    openFile: async filePath => {
      const failure = await deps.openPath(filePath)

      if (failure) {
        throw new Error(failure)
      }
    },
    revealFile: deps.revealFile
  }
}

export async function handleContextFileAction(
  request: ContextFileActionRequest,
  deps: ContextFileIpcDeps
): Promise<ContextFileActionResult> {
  const action = request?.action as FileLinkMenuItemId

  if (!FILE_ACTIONS.has(action)) {
    return { ok: false, message: 'Unsupported file action.' }
  }

  const model = contextFileModelForRequest(request ?? {}, deps.platform)

  if (!model) {
    return { ok: false, message: 'That item is not a file.' }
  }

  try {
    await runContextFileAction(action, model, actionDependencies(deps))

    return { ok: true }
  } catch (error) {
    deps.log(`Context file action failed: ${action}: ${error instanceof Error ? error.message : String(error)}`)

    return { ok: false, message: 'File action failed. See the Hermes log for details.' }
  }
}

export function registerContextFileIpc(ipcMain: Pick<IpcMain, 'handle'>, deps: ContextFileIpcDeps): void {
  ipcMain.handle('hermes:context-menu:file-action', (_event, request: ContextFileActionRequest) =>
    handleContextFileAction(request, deps)
  )
}
