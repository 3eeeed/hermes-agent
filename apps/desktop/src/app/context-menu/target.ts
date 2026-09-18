import type { MediaContextFileDescriptor } from '@/lib/media'

/**
 * What a right-click landed on, resolved from the DOM.
 *
 * One resolver so every surface agrees on ownership. Order encodes priority:
 * an editable wins over the link wrapping it (the caret is where the user is
 * working), a link wins over the image inside it for the LINK section — the
 * image section still appears because the target carries both.
 */

export interface ContextMenuDomTarget {
  /** The enclosing dialog content node, when the click landed inside one. */
  dialogPortalContainer: HTMLElement | null
  /** The clicked editable, when the click landed in one. */
  editable: HTMLElement | null
  /** `href` of the enclosing anchor, as written (never absolutized). */
  linkUrl: string
  /** Source URL of the clicked image, when the click landed on one. */
  imageUrl: string
  /** True when the click landed on an `<img>` (imageUrl may still be empty
   *  for a broken image; Copy image works through coordinates either way). */
  onImage: boolean
  /** The live selection's text at the moment of the click. */
  selectionText: string
  /** A Hermes-attached media/attachment descriptor, when the click landed on
   *  or inside an element carrying `data-hermes-context-file` (chat images,
   *  audio/video players, file attachments — see `@/lib/media`). */
  contextFile: MediaContextFileDescriptor | null
}

/** Form fields and `contenteditable` hosts. Mirrors the keybind helper, but
 *  returns the element so the menu can act on it. */
function editableFrom(element: Element | null): HTMLElement | null {
  if (!element) {
    return null
  }

  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
    return element.disabled || element.readOnly ? null : element
  }

  const host = element.closest('[contenteditable]')

  return host instanceof HTMLElement && host.isContentEditable ? host : null
}

function contextFileFrom(element: Element | null): MediaContextFileDescriptor | null {
  const carrier = element?.closest('[data-hermes-context-file]')
  const raw = carrier?.getAttribute('data-hermes-context-file')

  if (!raw) {
    return null
  }

  try {
    const value = JSON.parse(raw) as Partial<MediaContextFileDescriptor>

    return typeof value?.source === 'string' && typeof value?.name === 'string' && value.source && value.name
      ? { kind: value.kind ?? 'local', name: value.name, source: value.source, ...(value.profile ? { profile: value.profile } : {}) }
      : null
  } catch {
    return null
  }
}

export function resolveDomTarget(element: Element | null): ContextMenuDomTarget {
  const anchor = element?.closest('a[href]')
  const dialogContent = element?.closest('[data-slot="dialog-content"]')
  const image = element?.closest('img')
  const linkUrl = anchor?.getAttribute('href')?.trim() ?? ''

  return {
    dialogPortalContainer: dialogContent instanceof HTMLElement ? dialogContent : null,
    editable: editableFrom(element),
    // A placeholder anchor is not a link the menu can act on.
    linkUrl: linkUrl === '#' ? '' : linkUrl,
    imageUrl: image instanceof HTMLImageElement ? image.currentSrc || image.src : '',
    onImage: Boolean(image),
    selectionText: window.getSelection()?.toString().trim() ?? '',
    contextFile: contextFileFrom(element)
  }
}

/** True when `url` is something the in-app browser can render. */
export function isWebUrl(url: string): boolean {
  return /^https?:\/\//i.test(url)
}
