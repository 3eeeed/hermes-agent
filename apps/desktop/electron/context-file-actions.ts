import { contextFileSourceLink, type FileLinkContextMenuModel, type FileLinkMenuItemId } from './file-link-context-menu'

export interface ContextFileActionDependencies {
  copyFile: (filePath: string) => Promise<void>
  copyText: (value: string) => void
  materialize: (model: FileLinkContextMenuModel) => Promise<string>
  openFile: (filePath: string) => Promise<void>
  revealFile: (filePath: string) => void
}

const ACTION_LABELS: Record<FileLinkMenuItemId, string> = {
  'copy-file': 'Copy File',
  'copy-link': 'Copy Link',
  'copy-path': 'Copy Path',
  'open-file': 'Open File',
  'open-link': 'Open Link',
  'reveal-file': 'Show in File Manager'
}

// Failures previously only reached the internal log, leaving the user with a
// menu item that silently did nothing. Raw errors can carry gateway URLs,
// tokens, and internal addresses, so map them to a sanitized sentence.
export function contextFileActionErrorMessage(action: FileLinkMenuItemId, error: unknown): string {
  const label = ACTION_LABELS[action] || 'File action'
  const reason = error instanceof Error ? error.message : ''

  if (/public network/i.test(reason)) {
    return `${label} was blocked because that file is not on a public network address.`
  }

  if (/exceeds \d+ bytes/i.test(reason)) {
    return `${label} failed because the file is larger than the download limit.`
  }

  if (/HTTP (\d{3})/.test(reason)) {
    return `${label} failed because the file could not be downloaded.`
  }

  return `${label} failed. See the Hermes log for details.`
}

export async function runContextFileAction(
  action: FileLinkMenuItemId,
  model: FileLinkContextMenuModel,
  dependencies: ContextFileActionDependencies
): Promise<void> {
  if (action === 'copy-link') {
    dependencies.copyText(contextFileSourceLink(model.source))

    return
  }

  const localPath = await dependencies.materialize(model)

  if (action === 'copy-path') {
    dependencies.copyText(localPath)

    return
  }

  if (action === 'copy-file') {
    await dependencies.copyFile(localPath)

    return
  }

  if (action === 'open-file') {
    await dependencies.openFile(localPath)

    return
  }

  if (action === 'reveal-file') {
    dependencies.revealFile(localPath)

    return
  }

  throw new Error(`Context file action is not implemented: ${action}`)
}
