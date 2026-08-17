import { pathToFileURL } from 'node:url'

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
  downloadUrl?: string
  items: FileLinkMenuItem[]
  kind: 'local-file' | 'remote-file' | 'web-link'
  name?: string
  source: string
}

interface ContextFileDescriptor {
  downloadUrl?: string
  name: string
  remote: boolean
  source: string
}

function revealLabel(platform: ContextMenuPlatform): string {
  return platform === 'darwin' ? 'Show in Finder' : platform === 'win32' ? 'Show in Explorer' : 'Show in File Manager'
}

function fileMenuItems(platform: ContextMenuPlatform): FileLinkMenuItem[] {
  return [
    { id: 'open-file', label: 'Open File' },
    { id: 'copy-link', label: 'Copy Link' },
    { id: 'copy-path', label: 'Copy Path' },
    { id: 'copy-file', label: 'Copy File' },
    { id: 'reveal-file', label: revealLabel(platform) }
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

export function contextFileSourceLink(source: string): string {
  const trimmed = source.trim()

  if (/^[a-z]:[\\/]/i.test(trimmed)) {
    const [drive, ...segments] = trimmed.replace(/\\/g, '/').split('/')

    return `file:///${drive}/${segments.map(segment => encodeURIComponent(segment)).join('/')}`
  }

  return /^[a-z][a-z\d+.-]*:\/\//i.test(trimmed) ? trimmed : pathToFileURL(trimmed).toString()
}

export function contextMenuModelForContextFile(
  rawDescriptor: string,
  platform: ContextMenuPlatform
): FileLinkContextMenuModel | null {
  if (!rawDescriptor || rawDescriptor.length > 32_768) {
    return null
  }

  try {
    const value = JSON.parse(rawDescriptor) as Partial<ContextFileDescriptor>
    const source = typeof value.source === 'string' ? value.source.trim() : ''
    const name = typeof value.name === 'string' ? value.name.trim() : ''

    if (!source || !name || source.length > 16_384 || name.length > 1_024 || typeof value.remote !== 'boolean') {
      return null
    }

    if (value.remote) {
      const downloadUrl = typeof value.downloadUrl === 'string' ? value.downloadUrl : ''

      if (!/^https?:\/\//i.test(downloadUrl)) {
        return null
      }

      return {
        kind: 'remote-file',
        source,
        name,
        downloadUrl,
        items: fileMenuItems(platform)
      }
    }

    return {
      kind: 'local-file',
      source,
      name,
      items: fileMenuItems(platform)
    }
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
    const remote = /^https?:\/\//i.test(mediaSource)

    return {
      kind: remote ? 'remote-file' : 'local-file',
      source: mediaSource,
      name: params.suggestedFilename || undefined,
      ...(remote ? { downloadUrl: mediaSource } : {}),
      items: fileMenuItems(platform)
    }
  }

  if (!/^file:/i.test(params.linkURL)) {
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
