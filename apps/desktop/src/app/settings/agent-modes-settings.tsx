import { type ModelOptionProvider, REASONING_EFFORT_VALUES } from '@hermes/shared'
import { ChevronDown } from 'lucide-react'
import { type ReactNode, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { controlVariants } from '@/components/ui/control'
import { getCredentialPool, getGlobalModelOptions } from '@/hermes'
import { cn } from '@/lib/utils'
import {
  type DebateOptions,
  type DelegateOptions,
  type ModeConfig,
  type ModeRole,
  modeScopeKey,
  readModeConfig,
  saveModeConfig,
  type WorkerRole
} from '@/store/agent-modes'
import type { CredentialPoolEntry } from '@/types/hermes'

const blankRole = (): ModeRole => ({ provider: '', model: '' })
const blankWorker = (index = 0): WorkerRole => ({ ...blankRole(), lane: index ? `worker-${index + 1}` : 'worker' })
const defaultDelegate = (): DelegateOptions => ({ workers: [blankWorker()] })
const defaultDebate = (): DebateOptions => ({ side_a: blankRole(), side_b: blankRole(), judge: blankRole(), rounds: 1 })

type Accounts = Record<string, CredentialPoolEntry[]>

/** Label stacked above its control, so every value sits next to its own arrow. */
function FieldBox({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={cn('grid min-w-0 gap-1', className)}>
      <span className="text-[0.6875rem] font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

function NativeSelect({
  ariaLabel,
  value,
  onChange,
  children
}: {
  ariaLabel: string
  value: string
  onChange: (value: string) => void
  children: ReactNode
}) {
  return (
    <span className="relative flex min-w-0">
      <select
        aria-label={ariaLabel}
        className={cn(controlVariants({ size: 'sm' }), 'cursor-pointer appearance-none truncate pr-7')}
        onChange={event => onChange(event.target.value)}
        value={value}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
    </span>
  )
}

function EffortSelect({ label, value, onChange }: { label: string; value?: string; onChange: (effort?: string) => void }) {
  return (
    <FieldBox label="Effort">
      <NativeSelect ariaLabel={`${label} effort`} onChange={effort => onChange(effort || undefined)} value={value ?? ''}>
        <option value="">Default</option>
        {REASONING_EFFORT_VALUES.map(effort => (
          <option key={effort} value={effort}>
            {effort}
          </option>
        ))}
      </NativeSelect>
    </FieldBox>
  )
}

function accountName(entry: CredentialPoolEntry, index: number): string {
  const name = entry.label?.trim() || `Account ${index + 1}`

  return entry.email && entry.email !== name ? `${name} · ${entry.email}` : name
}

function RoleEditor({
  label,
  role,
  providers,
  onChange,
  accounts
}: {
  label: string
  role: ModeRole & { credential_id?: string }
  providers: ModelOptionProvider[]
  onChange: (role: ModeRole & { credential_id?: string }) => void
  /** Present only for workers: that provider's registered accounts. */
  accounts?: Accounts
}) {
  const models = providers.find(provider => provider.slug === role.provider)?.models ?? []
  const pool = accounts?.[role.provider] ?? []
  const missing = !!role.credential_id && !pool.some(entry => entry.id === role.credential_id)

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_minmax(0,0.7fr)]">
      <FieldBox label="Provider">
        <NativeSelect
          ariaLabel={`${label} provider`}
          onChange={provider => onChange({ ...role, provider, model: '', credential_id: undefined })}
          value={role.provider}
        >
          <option value="">Choose provider</option>
          {providers.map(provider => (
            <option key={provider.slug} value={provider.slug}>
              {provider.name}
            </option>
          ))}
        </NativeSelect>
      </FieldBox>
      <FieldBox label="Model">
        <NativeSelect ariaLabel={`${label} model`} onChange={model => onChange({ ...role, model })} value={role.model}>
          <option value="">Choose model</option>
          {models.map(model => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </NativeSelect>
      </FieldBox>
      <EffortSelect label={label} onChange={effort => onChange({ ...role, effort })} value={role.effort} />
      {accounts && pool.length > 0 && (
        <FieldBox className="sm:col-span-2 lg:col-span-1" label="Account">
          <NativeSelect
            ariaLabel={`${label} account`}
            onChange={id => onChange({ ...role, credential_id: id || undefined })}
            value={role.credential_id ?? ''}
          >
            <option value="">Automatic (rotate accounts)</option>
            {missing && <option value={role.credential_id}>Removed account</option>}
            {pool.map((entry, index) => (
              <option key={entry.id} value={entry.id}>
                {accountName(entry, index)}
              </option>
            ))}
          </NativeSelect>
        </FieldBox>
      )}
    </div>
  )
}

export function AgentModesSettings({ connectionId, profile }: { connectionId: string | null; profile: string }) {
  const scope = modeScopeKey(connectionId, profile)
  const [config, setConfig] = useState<ModeConfig>(() => readModeConfig(scope))
  const [providers, setProviders] = useState<ModelOptionProvider[]>([])
  const [accounts, setAccounts] = useState<Accounts>({})
  const [error, setError] = useState('')
  const [saved, setSaved] = useState('')
  useEffect(() => {
    setConfig(readModeConfig(scope))
    setError('')
    setSaved('')
    let current = true
    setProviders([])
    void getGlobalModelOptions({ explicitOnly: true }, profile)
      .then(result => {
        if (current) {
          setProviders(result.providers.filter(row => row.slug && row.models?.length && row.authenticated !== false))
        }
      })
      .catch(() => {
        if (current) {
          setError('Model catalog unavailable; retry by reopening settings.')
        }
      })

    setAccounts({})
    void getCredentialPool(profile)
      .then(result => {
        if (current) {
          setAccounts(Object.fromEntries(result.providers.map(row => [row.provider, row.entries])))
        }
      })
      .catch(() => undefined) // no pool listing: workers simply keep automatic rotation

    return () => {
      current = false
    }
  }, [scope, profile])
  const delegate = config.delegate ?? defaultDelegate()
  const debate = config.debate ?? defaultDebate()

  const changeDelegate = (patch: Partial<DelegateOptions>) => {
    setConfig({ ...config, delegate: { ...delegate, ...patch } })
    setSaved('')
  }

  const changeDebate = (patch: Partial<DebateOptions>) => {
    setConfig({ ...config, debate: { ...debate, ...patch } })
    setSaved('')
  }

  const changeWorker = (index: number, worker: WorkerRole) =>
    changeDelegate({ workers: delegate.workers.map((item, i) => (i === index ? worker : item)) })

  const save = (mode: 'delegate' | 'debate') => {
    try {
      const next = { ...readModeConfig(scope), [mode]: mode === 'delegate' ? delegate : debate }
      saveModeConfig(scope, next)
      setError('')
      setSaved(`${mode} settings saved on this device.`)
    } catch {
      setError('Choose a provider and model for every worker, and give each lane a unique name.')
      setSaved('')
    }
  }

  const missingAccount = delegate.workers.some(
    worker => worker.credential_id && !(accounts[worker.provider] ?? []).some(entry => entry.id === worker.credential_id)
  )

  return (
    <section aria-label="Agent modes" className="grid gap-5">
      <p className="text-xs text-muted-foreground">
        Normal uses your current chat model and settings. Delegate and Debate are available only when this backend
        advertises native execution support. Preferences are saved on this device for this connection and profile; they
        do not sync across devices.
      </p>
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
      {saved && (
        <p className="text-xs text-muted-foreground" role="status">
          {saved}
        </p>
      )}
      <fieldset className="grid gap-4 rounded-lg border p-4">
        <legend className="px-1 text-sm font-medium">Delegate</legend>
        <p className="text-xs text-muted-foreground">
          The orchestrator is this chat&apos;s model: it reads, plans, briefs a worker, then reviews and runs the checks.
          It cannot write files itself. Workers do the implementation with the normal tools and approvals. Delegate
          stays on for the chat until you switch back to Normal.
        </p>
        <div className="w-44">
          <FieldBox label="Orchestrator effort">
            <NativeSelect
              ariaLabel="Orchestrator effort"
              onChange={effort => changeDelegate({ orchestrator: effort ? { effort } : undefined })}
              value={delegate.orchestrator?.effort ?? ''}
            >
              <option value="">Default</option>
              {REASONING_EFFORT_VALUES.map(effort => (
                <option key={effort} value={effort}>
                  {effort}
                </option>
              ))}
            </NativeSelect>
          </FieldBox>
        </div>
        {delegate.workers.map((worker, index) => (
          <div className="grid gap-3 rounded-md border p-3" key={index}>
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-semibold">Worker {index + 1}</span>
              <div className="flex items-center gap-3">
                <label className="flex cursor-pointer items-center gap-1.5 text-xs">
                  <input
                    checked={(delegate.default_lane ?? delegate.workers[0]?.lane) === worker.lane}
                    name="delegate-default-lane"
                    onChange={() => changeDelegate({ default_lane: worker.lane })}
                    type="radio"
                  />
                  Default lane
                </label>
                <Button
                  disabled={delegate.workers.length === 1}
                  onClick={() => {
                    const workers = delegate.workers.filter((_, i) => i !== index)
                    changeDelegate({
                      workers,
                      default_lane: delegate.default_lane === worker.lane ? undefined : delegate.default_lane
                    })
                  }}
                  size="sm"
                  type="button"
                  variant="text"
                >
                  Remove worker
                </Button>
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <FieldBox label="Lane name">
                <input
                  aria-label={`Worker ${index + 1} lane`}
                  className={controlVariants({ size: 'sm' })}
                  onChange={event => changeWorker(index, { ...worker, lane: event.target.value })}
                  value={worker.lane}
                />
              </FieldBox>
              <FieldBox label="Responsibility">
                <input
                  aria-label={`Worker ${index + 1} responsibility`}
                  className={controlVariants({ size: 'sm' })}
                  onChange={event => changeWorker(index, { ...worker, responsibility: event.target.value || undefined })}
                  placeholder="e.g. frontend, tests"
                  value={worker.responsibility ?? ''}
                />
              </FieldBox>
            </div>
            <RoleEditor
              accounts={accounts}
              label={`Worker ${index + 1}`}
              onChange={role => changeWorker(index, { ...worker, ...role })}
              providers={providers}
              role={worker}
            />
          </div>
        ))}
        {missingAccount && (
          <p className="text-xs text-destructive">
            A worker is set to an account that is no longer registered. Choose another account or Automatic.
          </p>
        )}
        <div className="flex items-center justify-end gap-2 border-t pt-3">
          <Button
            disabled={delegate.workers.length >= 4}
            onClick={() => changeDelegate({ workers: [...delegate.workers, blankWorker(delegate.workers.length)] })}
            size="sm"
            type="button"
            variant="outline"
          >
            Add worker
          </Button>
          <Button onClick={() => save('delegate')} size="sm" type="button">
            Save Delegate settings
          </Button>
        </div>
      </fieldset>
      <fieldset className="grid gap-4 rounded-lg border p-4">
        <legend className="px-1 text-sm font-medium">Debate</legend>
        {(['side_a', 'side_b', 'judge'] as const).map(role => {
          const label = role === 'side_a' ? 'Side A' : role === 'side_b' ? 'Side B' : 'Judge'

          return (
            <div className="grid gap-2" key={role}>
              <span className="text-xs font-semibold">{label}</span>
              <RoleEditor
                label={label}
                onChange={value => changeDebate({ [role]: value })}
                providers={providers}
                role={debate[role]}
              />
            </div>
          )
        })}
        <div className="w-28">
          <FieldBox label="Rounds (1–3)">
            <input
              aria-label="Rounds (1–3)"
              className={controlVariants({ size: 'sm' })}
              max={3}
              min={1}
              onChange={event => changeDebate({ rounds: Number(event.target.value) })}
              type="number"
              value={debate.rounds}
            />
          </FieldBox>
        </div>
        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <p className="text-xs text-muted-foreground">
            Both sides are read-only. The backend enforces execution limits when supported.
          </p>
          <Button onClick={() => save('debate')} size="sm" type="button">
            Save Debate settings
          </Button>
        </div>
      </fieldset>
    </section>
  )
}
