import { pathToFileURL } from 'node:url'

import { DESKTOP_PROFILE_NAME_RE } from './desktop-profile'

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
  /** Registered gateway connection that owns a `gateway` file. */
  connectionId?: string
  items: FileLinkMenuItem[]
  kind: 'local-file' | 'remote-file' | 'web-link'
  name?: string
  profile?: string
  remoteKind?: 'external' | 'gateway'
  source: string
}

interface ContextFileDescriptor {
  connectionId?: string
  kind: 'external' | 'gateway' | 'local'
  name: string
  profile?: string
  source: string
}

// Registry connection ids are slugs; anything else never names a connection.
const CONNECTION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/

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
    const keys = value && typeof value === 'object' ? Object.keys(value) : []

    if (keys.some(key => !['connectionId', 'kind', 'name', 'profile', 'source'].includes(key))) {
      return null
    }

    const source = typeof value.source === 'string' ? value.source.trim() : ''
    const name = typeof value.name === 'string' ? value.name.trim() : ''
    const kind = value.kind
    const profile = typeof value.profile === 'string' ? value.profile.trim() : ''
    const connectionId = typeof value.connectionId === 'string' ? value.connectionId.trim() : ''

    if (
      !source ||
      !name ||
      source.length > 16_384 ||
      name.length > 1_024 ||
      !['external', 'gateway', 'local'].includes(kind || '') ||
      (profile && !DESKTOP_PROFILE_NAME_RE.test(profile)) ||
      (connectionId && !CONNECTION_ID_RE.test(connectionId)) ||
      ((profile || connectionId) && kind !== 'gateway')
    ) {
      return null
    }

    if (kind === 'external' && !/^https?:\/\//i.test(source)) {
      return null
    }

    const hasNonFileScheme =
      /^[a-z][a-z\d+.-]*:/i.test(source) && !/^file:/i.test(source) && !/^[a-z]:[\\/]/i.test(source)

    if (kind !== 'external' && hasNonFileScheme) {
      return null
    }

    if (kind === 'local') {
      return {
        kind: 'local-file',
        source,
        name,
        items: fileMenuItems(platform)
      }
    }

    return {
      kind: 'remote-file',
      remoteKind: kind,
      source,
      name,
      ...(profile ? { profile } : {}),
      ...(connectionId ? { connectionId } : {}),
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
      ...(remote ? { remoteKind: 'external' as const } : {}),
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
