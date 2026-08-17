import type { ReactNode } from 'react'

import type { Translations } from '@/i18n'
import { isMacPlatform, isWindowsPlatform } from '@/lib/platform'
import { notifyError } from '@/store/notifications'

import { type ContextMenuDomTarget, isFileUrl } from './target'

export type ContextFileAction = 'copy-file' | 'copy-link' | 'copy-path' | 'open-file' | 'reveal-file'

export interface FileMenuItemSpec {
  action: ContextFileAction
  icon: string
  label: string
}

/** The chat file under the click, as main will receive it: the element's
 *  descriptor, or a plain `file:` link. Null when the click names no file or
 *  this host cannot act on local files (no Electron bridge). */
export function contextFileRequest(
  target: ContextMenuDomTarget
): { descriptor: string } | { linkUrl: string } | null {
  if (!window.hermesDesktop?.contextMenuFileAction) {
    return null
  }

  if (target.contextFile) {
    return { descriptor: target.contextFile }
  }

  return isFileUrl(target.linkUrl) ? { linkUrl: target.linkUrl } : null
}

function revealLabel(t: Translations): string {
  return isMacPlatform()
    ? t.fileMenu.revealFinder
    : isWindowsPlatform()
      ? t.fileMenu.revealExplorer
      : t.fileMenu.revealFileManager
}

export function fileMenuItemSpecs(t: Translations): FileMenuItemSpec[] {
  const copy = t.contextMenu.file

  return [
    { action: 'open-file', icon: 'go-to-file', label: copy.openFile },
    { action: 'copy-link', icon: 'link', label: copy.copyLink },
    { action: 'copy-path', icon: 'copy', label: copy.copyPath },
    { action: 'copy-file', icon: 'files', label: copy.copyFile },
    { action: 'reveal-file', icon: 'folder-opened', label: revealLabel(t) }
  ]
}

export async function runContextFileMenuAction(
  action: ContextFileAction,
  request: { descriptor: string } | { linkUrl: string },
  t: Translations
): Promise<void> {
  try {
    const result = await window.hermesDesktop?.contextMenuFileAction?.({ action, ...request })

    if (result && !result.ok) {
      notifyError(new Error(result.message), t.contextMenu.file.actionFailed)
    }
  } catch (error) {
    notifyError(error, t.contextMenu.file.actionFailed)
  }
}

/** One section with the file verbs, or null when the click names no file. */
export function fileMenuSection(
  target: ContextMenuDomTarget,
  t: Translations,
  renderItem: (spec: FileMenuItemSpec, onSelect: () => void) => ReactNode
): ReactNode[] | null {
  const request = contextFileRequest(target)

  if (!request) {
    return null
  }

  return fileMenuItemSpecs(t).map(spec =>
    renderItem(spec, () => void runContextFileMenuAction(spec.action, request, t))
  )
}
