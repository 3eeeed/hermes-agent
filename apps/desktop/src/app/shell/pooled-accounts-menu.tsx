import type { PooledAccountsController } from '@/app/shell/hooks/use-pooled-accounts'
import { CheckCircle2, Pencil, Plus, X } from '@/lib/icons'
import { cn } from '@/lib/utils'

/**
 * The account menu body for one pooled provider: pick the account this chat
 * runs on, read its remaining quota, add another, or remove one.
 *
 * Rendered for both Codex and Anthropic — all provider-specific behaviour lives
 * in the controller, so this stays a pure view.
 */
export function PooledAccountsMenu({
  controller,
  focusedStoredSessionId
}: {
  controller: PooledAccountsController
  focusedStoredSessionId: null | string
}) {
  const {
    accounts,
    activeCredentialId,
    addError,
    addSession,
    cancelAdd,
    confirmDelete,
    deleteCandidate,
    deleteError,
    deleting,
    labelFor,
    pasteCode,
    renameCandidate,
    renameError,
    renaming,
    select,
    setDeleteCandidate,
    setDeleteError,
    setPasteCode,
    setRenameCandidate,
    startAdd,
    submitPastedCode,
    submitRename,
    usageById,
    usageLoading
  } = controller

  const activeLabel = (accounts.find(entry => entry.id === activeCredentialId)?.label ?? '').trim()

  return (
    <div className="space-y-2">
      <div className="rounded-md border border-primary/50 bg-primary/10 px-2 py-1.5 text-xs font-medium text-foreground">
        {activeCredentialId
          ? (
              <>
                {focusedStoredSessionId ? 'This chat is using ' : 'This new chat will use '}
                <span className="font-semibold text-primary">{activeLabel || 'the selected account'}</span>.
              </>
            )
          : accounts.length
            ? 'Choose an account for this chat.'
            // No saved accounts yet. Hermes may still be running on a
            // credential it borrowed from an external client (Claude Code),
            // which it will not adopt without an explicit sign-in here.
            : 'No accounts saved yet. Add one to manage it from this menu.'}
      </div>

      {accounts.map((entry, index) => {
        const label = labelFor(entry, index)
        const usage = usageById.get(entry.id)
        const pendingDelete = deleteCandidate?.id === entry.id
        const pendingRename = renameCandidate?.id === entry.id
        const selected = activeCredentialId === entry.id

        return (
          // A row is a div, not a button: it holds the × as a sibling control,
          // and a button inside a button is invalid markup React refuses to nest.
          <div
            className={cn(
              'relative rounded border border-(--ui-stroke-secondary) transition-colors',
              selected && 'border-2 border-primary bg-primary/15 shadow-sm',
              pendingDelete && 'border-destructive/60'
            )}
            key={entry.id}
          >
            <button
              aria-pressed={selected}
              className={cn(
                'block w-full space-y-1.5 rounded px-2 py-2 pr-12 text-left text-xs transition-colors hover:bg-accent/40',
                selected && 'bg-primary/10'
              )}
              onClick={() => select(entry.id)}
              type="button"
            >
              <div className="flex items-center justify-between gap-3">
                <span className="flex min-w-0 items-center gap-1.5 truncate font-medium text-foreground">
                  {selected && <CheckCircle2 aria-label="Active for this chat" className="size-4 shrink-0 text-primary" />}
                  <span className="truncate">{label}</span>
                  {selected && <span className="shrink-0 rounded bg-primary px-1.5 py-0.5 text-[0.625rem] font-bold tracking-wide text-primary-foreground">ACTIVE</span>}
                </span>
                {/* Anthropic's usage endpoint reports no plan name at all, so a
                    bare `?? 'Checking…'` left every Claude row stuck on
                    "Checking…" forever even though its quota had arrived.
                    The placeholder belongs to the pending state only. */}
                <span className="shrink-0 text-(--ui-text-tertiary)">
                  {usage?.plan ?? entry.last_status ?? (usage ? '' : 'Checking…')}
                </span>
              </div>

              {/* The account's real email, decoded from its own token — not the
                  (freely renameable) label. Two rows can share a label like "1";
                  this is the only way to tell which underlying account each one
                  actually is. Omitted when the provider's token carries none
                  (Anthropic) rather than showing a misleading blank line. */}
              {(usage?.email ?? entry.email) && (
                <div className="truncate text-[0.6875rem] text-(--ui-text-tertiary)">{usage?.email ?? entry.email}</div>
              )}

              {usage?.available && usage.windows?.length
                ? usage.windows.map(window => {
                    const used = window.used_percent == null ? null : Math.max(0, Math.min(100, window.used_percent))
                    const remaining = used == null ? null : Math.round(100 - used)
                    const reset = window.reset_at ? new Date(window.reset_at).toLocaleString() : null

                    return (
                      <div className="space-y-1" key={window.label}>
                        <div className="flex justify-between gap-3 text-(--ui-text-secondary)">
                          <span>{window.label}</span>
                          <span>{remaining == null ? 'Unavailable' : `${remaining}% left`}</span>
                        </div>
                        {used == null ? null : <div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${used}%` }} /></div>}
                        {reset ? <div className="text-[0.6875rem] text-(--ui-text-tertiary)">Resets {reset}</div> : null}
                        {!reset && window.detail ? <div className="text-[0.6875rem] text-(--ui-text-tertiary)">{window.detail}</div> : null}
                      </div>
                    )
                  })
                : (
                    <div className="text-(--ui-text-tertiary)">
                      {usageLoading ? 'Loading limits…' : usage ? 'Usage unavailable' : 'Loading limits…'}
                    </div>
                  )}

              {usage?.details?.map(detail => <div className="text-[0.6875rem] text-(--ui-text-tertiary)" key={detail}>{detail}</div>)}
            </button>

            <div className="absolute right-1 top-1 flex items-center gap-0.5">
              <button
                aria-label={`Rename ${label}`}
                className="rounded p-1 text-(--ui-text-tertiary) transition-colors hover:bg-accent/40 hover:text-foreground"
                onClick={() => {
                  setDeleteCandidate(null)
                  // Seed with the CURRENT label so the common edit (append a
                  // name to what is already there) starts from the real value
                  // rather than an empty box.
                  setRenameCandidate({ id: entry.id, label })
                }}
                type="button"
              >
                <Pencil className="size-3" />
              </button>

              <button
                aria-label={`Remove ${label}`}
                className="rounded p-1 text-(--ui-text-tertiary) transition-colors hover:bg-destructive/15 hover:text-destructive"
                onClick={() => {
                  setDeleteError(null)
                  setRenameCandidate(null)
                  setDeleteCandidate({ id: entry.id, label })
                }}
                type="button"
              >
                <X className="size-3" />
              </button>
            </div>

            {/* Renaming is inline for the same reason deletion is: this panel
                lives inside a dropdown, and a Dialog opened from here unmounts
                with it. */}
            {pendingRename && (
              <div className="space-y-2 border-t border-(--ui-stroke-secondary) px-2 py-2 text-xs">
                <input
                  aria-label={`New name for ${label}`}
                  autoFocus
                  className="w-full rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs text-foreground"
                  disabled={renaming}
                  onChange={event => setRenameCandidate({ id: entry.id, label: event.target.value })}
                  onKeyDown={event => {
                    if (event.key === 'Enter') {
                      void submitRename()
                    } else if (event.key === 'Escape') {
                      setRenameCandidate(null)
                    }
                  }}
                  placeholder="Whose account is this?"
                  spellCheck={false}
                  value={renameCandidate.label}
                />
                {renameError && <div className="text-destructive">{renameError}</div>}
                <div className="flex justify-end gap-2">
                  <button
                    className="rounded px-2 py-1 text-(--ui-text-secondary) transition-colors hover:bg-accent/40"
                    disabled={renaming}
                    onClick={() => setRenameCandidate(null)}
                    type="button"
                  >
                    Cancel
                  </button>
                  <button
                    className="rounded bg-primary px-2 py-1 font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-60"
                    disabled={renaming || !renameCandidate.label.trim()}
                    onClick={() => void submitRename()}
                    type="button"
                  >
                    {renaming ? 'Saving…' : 'Save'}
                  </button>
                </div>
              </div>
            )}

            {/* Confirmation lives inline instead of in a modal: this panel is
                inside a dropdown, and opening a Dialog from here closes the
                dropdown and unmounts the dialog with it. Deletion is
                irreversible, so it must never be one stray click away. */}
            {pendingDelete && (
              <div className="space-y-2 border-t border-(--ui-stroke-secondary) px-2 py-2 text-xs">
                <div className="text-foreground">Remove “{label}”? You will need to sign in again to use it.</div>
                {deleteError && <div className="text-destructive">{deleteError}</div>}
                <div className="flex justify-end gap-2">
                  <button
                    className="rounded px-2 py-1 text-(--ui-text-secondary) transition-colors hover:bg-accent/40"
                    disabled={deleting}
                    onClick={() => {
                      setDeleteCandidate(null)
                      setDeleteError(null)
                    }}
                    type="button"
                  >
                    Cancel
                  </button>
                  <button
                    className="rounded bg-destructive px-2 py-1 font-medium text-destructive-foreground transition-opacity hover:opacity-90 disabled:opacity-60"
                    disabled={deleting}
                    onClick={() => void confirmDelete()}
                    type="button"
                  >
                    {deleting ? 'Removing…' : 'Remove'}
                  </button>
                </div>
              </div>
            )}
          </div>
        )
      })}

      {/* Add-account row. The device code is shown here rather than only in the
          opened browser tab: the popup can be blocked, and the code is the only
          way to finish the sign-in. */}
      {addSession
        ? (
            <div className="space-y-2 rounded border border-primary/50 px-2 py-2 text-xs">
              {addSession.flow === 'device_code'
                ? (
                    <>
                      <div className="text-foreground">Enter this code in the browser to add an account:</div>
                      <div className="select-all text-center font-mono text-sm tracking-widest text-foreground">
                        {addSession.user_code}
                      </div>
                    </>
                  )
                : (
                    <>
                      <div className="text-foreground">Approve in the browser, then paste the code you are given:</div>
                      <input
                        autoFocus
                        className="w-full rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 font-mono text-xs text-foreground"
                        onChange={event => setPasteCode(event.target.value)}
                        onKeyDown={event => {
                          if (event.key === 'Enter') {
                            void submitPastedCode()
                          }
                        }}
                        placeholder="Paste code here"
                        spellCheck={false}
                        value={pasteCode}
                      />
                    </>
                  )}

              <div className="flex justify-between gap-2">
                <a className="truncate text-(--ui-text-tertiary) underline" href={addSession.url} rel="noreferrer" target="_blank">
                  Open sign-in page
                </a>
                <div className="flex shrink-0 gap-2">
                  {addSession.flow === 'pkce' && (
                    <button
                      className="rounded bg-primary px-2 py-0.5 font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-60"
                      disabled={!pasteCode.trim()}
                      onClick={() => void submitPastedCode()}
                      type="button"
                    >
                      Finish
                    </button>
                  )}
                  <button
                    className="rounded px-2 py-0.5 text-(--ui-text-secondary) transition-colors hover:bg-accent/40"
                    onClick={cancelAdd}
                    type="button"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )
        : (
            <button
              className="flex w-full items-center gap-2 rounded border border-dashed border-(--ui-stroke-secondary) px-2 py-2 text-xs text-(--ui-text-secondary) transition-colors hover:bg-accent/40 hover:text-foreground"
              onClick={() => void startAdd()}
              type="button"
            >
              <Plus className="size-3" />
              <span>Add account</span>
            </button>
          )}

      {addError && <div className="text-xs text-destructive">{addError}</div>}
    </div>
  )
}
