import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { cancelOAuthSession, pollOAuthSession, startOAuthLogin, submitOAuthCode } from '@/api/config'
import {
  deletePoolCredential,
  getCredentialPoolUsage,
  getPoolSessionCredentialSelection,
  type PooledAccountProvider,
  setPoolSessionCredentialSelection
} from '@/api/system'
import type { CredentialPoolEntry, CredentialPoolResponse, CredentialPoolUsageEntry } from '@/types/hermes'

/** Device-code providers show a code to type; PKCE providers take a pasted code. */
export interface PooledAccountAddSession {
  flow: 'device_code' | 'pkce'
  poll_interval: number
  session_id: string
  url: string
  user_code: string
}

export interface PooledAccountsController {
  accounts: readonly CredentialPoolEntry[]
  /** Whether a new account can be added, independent of how many exist. */
  canAddAccounts: boolean
  activeCredentialId: null | string
  addError: null | string
  addSession: PooledAccountAddSession | null
  cancelAdd: () => void
  confirmDelete: () => Promise<void>
  deleteCandidate: { id: string; label: string } | null
  deleteError: null | string
  deleting: boolean
  labelFor: (entry: CredentialPoolEntry, index: number) => string
  pasteCode: string
  select: (credentialId: string) => void
  setDeleteCandidate: (candidate: { id: string; label: string } | null) => void
  setDeleteError: (message: null | string) => void
  setPasteCode: (code: string) => void
  startAdd: () => Promise<void>
  submitPastedCode: () => Promise<void>
  usageById: Map<string, CredentialPoolUsageEntry>
  usageLoading: boolean
}

/**
 * One provider's account list, quota, add/remove flows and per-chat selection.
 *
 * Codex and Anthropic differ only in how a new account is authorized (device
 * code vs. PKCE paste); everything after that — pool listing, usage, per-chat
 * pinning, deletion — is identical, so this hook owns it once. Adding a third
 * pooled provider should mean one more call, not another copy of this logic.
 */
export function usePooledAccounts(options: {
  activeGatewayProfile: null | string
  credentialPool: UseQueryResult<CredentialPoolResponse>
  draftCredentialId: null | string
  enabled: boolean
  focusedStoredSessionId: null | string
  onDraftSelect: (credentialId: string) => void
  provider: PooledAccountProvider
}): PooledAccountsController {
  const {
    activeGatewayProfile,
    credentialPool,
    draftCredentialId,
    enabled,
    focusedStoredSessionId,
    onDraftSelect,
    provider
  } = options

  const [selectedCredentialId, setSelectedCredentialId] = useState<null | string>(null)
  const [addSession, setAddSession] = useState<PooledAccountAddSession | null>(null)
  const [addError, setAddError] = useState<null | string>(null)
  const [pasteCode, setPasteCode] = useState('')
  const [deleteCandidate, setDeleteCandidate] = useState<{ id: string; label: string } | null>(null)
  const [deleteError, setDeleteError] = useState<null | string>(null)
  const [deleting, setDeleting] = useState(false)

  const accounts = useMemo(
    () => credentialPool.data?.providers.find(entry => entry.provider === provider)?.entries ?? [],
    [credentialPool.data, provider]
  )

  const accountUsage = useQuery({
    // Queried even with an empty pool: the response also reports whether
    // accounts can be added, which is how a provider with no entries yet
    // (Anthropic, before its first explicit sign-in) still offers its add
    // button instead of hiding the menu that contains it.
    enabled,
    queryFn: () => getCredentialPoolUsage(provider, activeGatewayProfile || undefined),
    queryKey: ['credential-pool-usage', provider, activeGatewayProfile],
    refetchInterval: 120_000,
    retry: false,
    staleTime: 90_000
  })

  const canAddAccounts = accountUsage.data?.can_add_accounts ?? false

  const usageById = useMemo(
    () => new Map((accountUsage.data?.entries ?? []).map(entry => [entry.id, entry])),
    [accountUsage.data]
  )

  const sessionSelection = useQuery({
    enabled: enabled && Boolean(focusedStoredSessionId),
    queryFn: () => getPoolSessionCredentialSelection(provider, focusedStoredSessionId!, activeGatewayProfile || undefined),
    queryKey: ['pool-session-selection', provider, focusedStoredSessionId, activeGatewayProfile],
    retry: false,
    staleTime: Infinity
  })

  useEffect(() => {
    setSelectedCredentialId(sessionSelection.data?.credential_id ?? null)
  }, [sessionSelection.data?.credential_id, focusedStoredSessionId])

  // New chats are persisted only with their first prompt. Keep the chosen
  // account visible in the draft until then.
  const activeCredentialId = focusedStoredSessionId ? selectedCredentialId : draftCredentialId

  // Device-code sign-ins complete in the background, so poll. PKCE finishes
  // synchronously on submit and must NOT be polled — there is nothing to poll.
  useEffect(() => {
    if (!addSession || addSession.flow !== 'device_code') {
      return
    }

    let cancelled = false

    const check = async () => {
      try {
        const result = await pollOAuthSession(provider, addSession.session_id, activeGatewayProfile || undefined)

        if (cancelled) {
          return
        }

        if (result.status === 'approved') {
          setAddSession(null)
          setAddError(null)
          void credentialPool.refetch()
          void accountUsage.refetch()
        } else if (result.status === 'error' || result.status === 'denied' || result.status === 'expired') {
          setAddError(result.error_message || 'The sign-in request expired. Please try again.')
          setAddSession(null)
        }
      } catch {
        if (!cancelled) {
          setAddError('Could not check the sign-in status. Please try again.')
        }
      }
    }

    void check()
    const timer = window.setInterval(() => void check(), Math.max(addSession.poll_interval, 2) * 1000)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeGatewayProfile, accountUsage, addSession, credentialPool, provider])

  const startAdd = async () => {
    setAddError(null)
    setPasteCode('')

    try {
      // addAccount appends this grant to the pool. Without it the backend's
      // singleton save path replaces the stored login, so adding a 2nd account
      // silently removed the 1st.
      const result = await startOAuthLogin(provider, activeGatewayProfile || undefined, { addAccount: true })
      const url = result.flow === 'device_code' ? result.verification_url : result.auth_url
      setAddSession({
        flow: result.flow,
        poll_interval: result.flow === 'device_code' ? result.poll_interval : 0,
        session_id: result.session_id,
        url,
        user_code: result.flow === 'device_code' ? result.user_code : ''
      })
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch (error) {
      setAddError(error instanceof Error ? error.message : 'Could not start sign-in.')
    }
  }

  const cancelAdd = () => {
    if (addSession) {
      void cancelOAuthSession(addSession.session_id, activeGatewayProfile || undefined)
    }

    setAddSession(null)
    setAddError(null)
    setPasteCode('')
  }

  const submitPastedCode = async () => {
    if (!addSession || !pasteCode.trim()) {
      return
    }

    setAddError(null)

    try {
      await submitOAuthCode(provider, addSession.session_id, pasteCode.trim(), activeGatewayProfile || undefined)
      setAddSession(null)
      setPasteCode('')
      void credentialPool.refetch()
      void accountUsage.refetch()
    } catch (error) {
      setAddError(error instanceof Error ? error.message : 'That code was not accepted. Please try again.')
    }
  }

  const confirmDelete = async () => {
    if (!deleteCandidate) {
      return
    }

    setDeleting(true)
    setDeleteError(null)

    try {
      await deletePoolCredential(provider, deleteCandidate.id, activeGatewayProfile || undefined)

      if (selectedCredentialId === deleteCandidate.id) {
        setSelectedCredentialId(null)
      }

      setDeleteCandidate(null)
      void credentialPool.refetch()
      void accountUsage.refetch()
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : 'Could not remove this account.')
    } finally {
      setDeleting(false)
    }
  }

  const select = (credentialId: string) => {
    if (!focusedStoredSessionId) {
      onDraftSelect(credentialId)

      return
    }

    // Paint the choice before the dropdown closes. The backend write stays
    // authoritative; revert if it rejects the account/session pairing.
    const previousCredentialId = selectedCredentialId
    setSelectedCredentialId(credentialId)
    void setPoolSessionCredentialSelection(provider, focusedStoredSessionId, credentialId, activeGatewayProfile)
      .then(result => {
        setSelectedCredentialId(result.credential_id)
        void sessionSelection.refetch()
      })
      .catch(() => setSelectedCredentialId(previousCredentialId))
  }

  return {
    accounts,
    activeCredentialId,
    addError,
    addSession,
    canAddAccounts,
    cancelAdd,
    confirmDelete,
    deleteCandidate,
    deleteError,
    deleting,
    labelFor: (entry, index) => (entry.label ?? '').trim() || String(index + 1),
    pasteCode,
    select,
    setDeleteCandidate,
    setDeleteError,
    setPasteCode,
    startAdd,
    submitPastedCode,
    usageById,
    usageLoading: accountUsage.isLoading
  }
}
