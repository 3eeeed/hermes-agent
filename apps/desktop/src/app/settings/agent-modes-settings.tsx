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
const blankWorker = (): WorkerRole => ({ ...blankRole(), lane: 'research' })
const defaultDelegate = (): DelegateOptions => ({
  orchestrator: blankRole(),
  workers: [blankWorker()],
  max_iterations: 2,
  max_concurrency: 1,
  permissions: 'read-only'
})
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
      <label>
        {label} effort{' '}
        <select
          aria-label={`${label} effort`}
          onChange={event => onChange({ ...role, effort: event.target.value || undefined })}
          value={role.effort ?? ''}
        >
          <option value="">Default</option>
          {REASONING_EFFORT_VALUES.map(effort => (
            <option key={effort} value={effort}>
              {effort}
            </option>
          ))}
        </select>
      </label>
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
      setError('Choose a provider and model for every role and stay within the displayed bounds.')
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
        <RoleEditor
          label="Orchestrator"
          onChange={orchestrator => changeDelegate({ orchestrator })}
          providers={providers}
          role={delegate.orchestrator}
        />
        {delegate.workers.map((worker, index) => (
          <div className="rounded border p-2" key={index}>
            <label>
              Worker {index + 1} lane{' '}
              <input
                aria-label={`Worker ${index + 1} lane`}
                onChange={event => changeWorker(index, { ...worker, lane: event.target.value })}
                value={worker.lane}
              />
            </label>
            <RoleEditor
              label={`Worker ${index + 1}`}
              onChange={role => changeWorker(index, { ...worker, ...role })}
              providers={providers}
              role={worker}
            />
            <button
              disabled={delegate.workers.length === 1}
              onClick={() =>
                changeDelegate({
                  workers: delegate.workers.filter((_, i) => i !== index),
                  max_concurrency: Math.min(delegate.max_concurrency, delegate.workers.length - 1)
                })
              }
              type="button"
            >
              Remove worker
            </button>
          </div>
        ))}
        <button
          disabled={delegate.workers.length >= 4}
          onClick={() => changeDelegate({ workers: [...delegate.workers, blankWorker()] })}
          type="button"
        >
          Add worker
        </button>
        <label>
          Iterations (1–4){' '}
          <input
            max={4}
            min={1}
            onChange={event => changeDelegate({ max_iterations: Number(event.target.value) })}
            type="number"
            value={delegate.max_iterations}
          />
        </label>
        <label>
          Concurrency (1–{delegate.workers.length}){' '}
          <input
            max={delegate.workers.length}
            min={1}
            onChange={event => changeDelegate({ max_concurrency: Number(event.target.value) })}
            type="number"
            value={delegate.max_concurrency}
          />
        </label>
        <p>Permissions: read-only. Write/action approvals are not enabled by this editor. No spend cap is promised.</p>
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
