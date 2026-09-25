import { type ModelOptionProvider, REASONING_EFFORT_VALUES } from '@hermes/shared'
import { useEffect, useState } from 'react'

import { getGlobalModelOptions } from '@/hermes'
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

const blankRole = (): ModeRole => ({ provider: '', model: '' })
const blankWorker = (index = 0): WorkerRole => ({ ...blankRole(), lane: index ? `worker-${index + 1}` : 'worker' })
const defaultDelegate = (): DelegateOptions => ({ workers: [blankWorker()] })

function EffortSelect({ label, value, onChange }: { label: string; value?: string; onChange: (effort?: string) => void }) {
  return (
    <label>
      {label} effort{' '}
      <select aria-label={`${label} effort`} onChange={event => onChange(event.target.value || undefined)} value={value ?? ''}>
        <option value="">Default</option>
        {REASONING_EFFORT_VALUES.map(effort => (
          <option key={effort} value={effort}>
            {effort}
          </option>
        ))}
      </select>
    </label>
  )
}
const defaultDebate = (): DebateOptions => ({ side_a: blankRole(), side_b: blankRole(), judge: blankRole(), rounds: 1 })

function RoleEditor({
  label,
  role,
  providers,
  onChange
}: {
  label: string
  role: ModeRole
  providers: ModelOptionProvider[]
  onChange: (role: ModeRole) => void
}) {
  const models = providers.find(provider => provider.slug === role.provider)?.models ?? []

  return (
    <div className="flex flex-wrap gap-2 py-2">
      <label>
        {label} provider{' '}
        <select
          aria-label={`${label} provider`}
          onChange={event => onChange({ ...role, provider: event.target.value, model: '' })}
          value={role.provider}
        >
          <option value="">Choose provider</option>
          {providers.map(provider => (
            <option key={provider.slug} value={provider.slug}>
              {provider.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        {label} model{' '}
        <select
          aria-label={`${label} model`}
          onChange={event => onChange({ ...role, model: event.target.value })}
          value={role.model}
        >
          <option value="">Choose model</option>
          {models.map(model => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </select>
      </label>
      <EffortSelect label={label} onChange={effort => onChange({ ...role, effort })} value={role.effort} />
    </div>
  )
}

export function AgentModesSettings({ connectionId, profile }: { connectionId: string | null; profile: string }) {
  const scope = modeScopeKey(connectionId, profile)
  const [config, setConfig] = useState<ModeConfig>(() => readModeConfig(scope))
  const [providers, setProviders] = useState<ModelOptionProvider[]>([])
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

  return (
    <section aria-label="Agent modes" className="grid gap-5">
      <p>
        Normal uses your current chat model and settings. Delegate and Debate are available only when this backend
        advertises native execution support. Preferences are saved on this device for this connection and profile; they
        do not sync across devices.
      </p>
      {error && <p role="alert">{error}</p>}
      {saved && <p role="status">{saved}</p>}
      <fieldset className="grid gap-2 rounded-lg border p-3">
        <legend>Delegate</legend>
        <p>
          The orchestrator is this chat&apos;s model: it reads, plans, briefs a worker, then reviews and runs the checks.
          It cannot write files itself. Workers do the implementation with the normal tools and approvals. Delegate
          stays on for the chat until you switch back to Normal.
        </p>
        <EffortSelect
          label="Orchestrator"
          onChange={effort => changeDelegate({ orchestrator: effort ? { effort } : undefined })}
          value={delegate.orchestrator?.effort}
        />
        {delegate.workers.map((worker, index) => (
          <div className="rounded border p-2" key={index}>
            <label>
              Lane name{' '}
              <input
                aria-label={`Worker ${index + 1} lane`}
                onChange={event => changeWorker(index, { ...worker, lane: event.target.value })}
                value={worker.lane}
              />
            </label>{' '}
            <label>
              Responsibility{' '}
              <input
                aria-label={`Worker ${index + 1} responsibility`}
                onChange={event => changeWorker(index, { ...worker, responsibility: event.target.value || undefined })}
                placeholder="e.g. frontend, tests"
                value={worker.responsibility ?? ''}
              />
            </label>
            <RoleEditor
              label={`Worker ${index + 1}`}
              onChange={role => changeWorker(index, { ...worker, ...role })}
              providers={providers}
              role={worker}
            />
            <label>
              <input
                checked={(delegate.default_lane ?? delegate.workers[0]?.lane) === worker.lane}
                name="delegate-default-lane"
                onChange={() => changeDelegate({ default_lane: worker.lane })}
                type="radio"
              />{' '}
              Default lane
            </label>{' '}
            <button
              disabled={delegate.workers.length === 1}
              onClick={() => {
                const workers = delegate.workers.filter((_, i) => i !== index)
                changeDelegate({
                  workers,
                  default_lane: delegate.default_lane === worker.lane ? undefined : delegate.default_lane
                })
              }}
              type="button"
            >
              Remove worker
            </button>
          </div>
        ))}
        <button
          disabled={delegate.workers.length >= 4}
          onClick={() => changeDelegate({ workers: [...delegate.workers, blankWorker(delegate.workers.length)] })}
          type="button"
        >
          Add worker
        </button>
        <button onClick={() => save('delegate')} type="button">
          Save Delegate settings
        </button>
      </fieldset>
      <fieldset className="grid gap-2 rounded-lg border p-3">
        <legend>Debate</legend>
        {(['side_a', 'side_b', 'judge'] as const).map(role => (
          <RoleEditor
            key={role}
            label={role === 'side_a' ? 'Side A' : role === 'side_b' ? 'Side B' : 'Judge'}
            onChange={value => changeDebate({ [role]: value })}
            providers={providers}
            role={debate[role]}
          />
        ))}
        <label>
          Rounds (1–3){' '}
          <input
            max={3}
            min={1}
            onChange={event => changeDebate({ rounds: Number(event.target.value) })}
            type="number"
            value={debate.rounds}
          />
        </label>
        <p>Both sides are read-only. The backend enforces execution limits when supported.</p>
        <button onClick={() => save('debate')} type="button">
          Save Debate settings
        </button>
      </fieldset>
    </section>
  )
}
