export type ContextMenuPlatform = 'darwin' | 'linux' | 'win32'

export type FileLinkMenuItemId = 'copy-file' | 'copy-link' | 'copy-path' | 'open-file' | 'open-link' | 'reveal-file'

export interface FileLinkContextParams {
  linkURL: string
  suggestedFilename?: string
}

export interface FileLinkMenuItem {
  id: FileLinkMenuItemId
  label: string
}

export interface FileLinkContextMenuModel {
  items: FileLinkMenuItem[]
  kind: 'local-file' | 'remote-file' | 'web-link'
  source: string
}

function fileMenuItems(platform: ContextMenuPlatform): FileLinkMenuItem[] {
  return [
    { id: 'open-file', label: 'Open File' },
    { id: 'copy-link', label: 'Copy Link' },
    { id: 'copy-path', label: 'Copy Path' },
    { id: 'copy-file', label: 'Copy File' },
    { id: 'reveal-file', label: platform === 'darwin' ? 'Show in Finder' : 'Show in File Manager' }
  ]
}

function mediaSourceFromLink(linkURL: string): null | string {
  try {
    const hash = new URL(linkURL).hash

    return hash.startsWith('#media:') ? decodeURIComponent(hash.slice('#media:'.length)) : null
  } catch {
    return null
  }
}

export function contextMenuModelForLink(
  params: FileLinkContextParams,
  platform: ContextMenuPlatform
): FileLinkContextMenuModel {
  const mediaSource = mediaSourceFromLink(params.linkURL)

  if (mediaSource) {
    return {
      kind: /^https?:\/\//i.test(mediaSource) ? 'remote-file' : 'local-file',
      source: mediaSource,
      items: fileMenuItems(platform)
    }
  }

  if (/^https?:\/\//i.test(params.linkURL)) {
    return {
      kind: 'web-link',
      source: params.linkURL,
      items: [
        { id: 'open-link', label: 'Open Link' },
        { id: 'copy-link', label: 'Copy Link' }
      ]
    }
  }

  return {
    kind: 'local-file',
    source: params.linkURL,
    items: fileMenuItems(platform)
  }
}
