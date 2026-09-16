import { atom } from 'nanostores'

/**
 * Account chosen on a fresh, as-yet-unsaved chat. The selection is consumed
 * before that chat's first prompt is submitted, then persists on the session
 * in the backend. It is intentionally not stored in localStorage: account
 * choice is scoped to one conversation, never a global default.
 */
export interface DraftCodexCredentialSelection {
  credentialId: string
  profile: null | string
}

export const $draftCodexCredentialSelection = atom<DraftCodexCredentialSelection | null>(null)

/**
 * The same draft selection for Anthropic. Kept as a SEPARATE atom rather than a
 * provider-keyed map: a chat can carry one pending choice per provider, and
 * sharing an atom would make picking a Claude account clear a pending Codex one.
 */
export const $draftAnthropicCredentialSelection = atom<DraftCodexCredentialSelection | null>(null)

export function clearDraftCodexCredentialSelection(): void {
  $draftCodexCredentialSelection.set(null)
  $draftAnthropicCredentialSelection.set(null)
}

export function consumeDraftCodexCredentialSelection(): DraftCodexCredentialSelection | null {
  const selection = $draftCodexCredentialSelection.get()
  $draftCodexCredentialSelection.set(null)

  return selection
}

export function consumeDraftAnthropicCredentialSelection(): DraftCodexCredentialSelection | null {
  const selection = $draftAnthropicCredentialSelection.get()
  $draftAnthropicCredentialSelection.set(null)

  return selection
}

export function setDraftCodexCredentialSelection(selection: DraftCodexCredentialSelection): void {
  $draftCodexCredentialSelection.set(selection)
}

export function setDraftAnthropicCredentialSelection(selection: DraftCodexCredentialSelection): void {
  $draftAnthropicCredentialSelection.set(selection)
}
