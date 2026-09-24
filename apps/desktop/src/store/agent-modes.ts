import { atom } from 'nanostores'

export type AgentMode = 'normal' | 'delegate' | 'debate'
export interface ModeRole {
  provider: string
  model: string
  effort?: string
}
export interface WorkerRole extends ModeRole {
  lane: string
}
export interface DelegateOptions {
  orchestrator: ModeRole
  workers: WorkerRole[]
  max_iterations: number
  max_concurrency: number
  permissions: 'read-only'
}
export interface DebateOptions {
  side_a: ModeRole
  side_b: ModeRole
  judge: ModeRole
  rounds: number
}
export interface ModeConfig {
  delegate?: DelegateOptions
  debate?: DebateOptions
}

const PREFIX = 'hermes.desktop.agent-modes.'
const MODES: AgentMode[] = ['normal', 'delegate', 'debate']
export const $modeRevision = atom(0)
const supported = new Map<string, Set<AgentMode>>()
const probeGeneration = new Map<string, number>()

export function modeScopeKey(connectionId: string | null, profile: string): string {
  return encodeURIComponent(JSON.stringify([connectionId ?? 'local', profile.trim() || 'default']))
}

function bump(): void {
  $modeRevision.set($modeRevision.get() + 1)
}

function parse<T>(key: string, fallback: T): T {
  try {
    return JSON.parse(localStorage.getItem(key) ?? '') as T
  } catch {
    return fallback
  }
}

export function selectedMode(scope: string): AgentMode {
  const value = parse<AgentMode>(`${PREFIX}choice.${scope}`, 'normal')

  return MODES.includes(value) ? value : 'normal'
}

export function setModeChoice(scope: string, mode: AgentMode): void {
  if (!MODES.includes(mode)) {
    throw new Error('Invalid agent mode')
  }
  if (mode === 'normal') {
    localStorage.removeItem(`${PREFIX}choice.${scope}`)
  } else {
    localStorage.setItem(`${PREFIX}choice.${scope}`, JSON.stringify(mode))
  }
  bump()
}

function validRole(role: ModeRole): boolean {
  return (
    !!role &&
    typeof role.provider === 'string' &&
    !!role.provider.trim() &&
    typeof role.model === 'string' &&
    !!role.model.trim() &&
    (role.effort === undefined || ['none', 'low', 'medium', 'high', 'xhigh'].includes(role.effort))
  )
}

function validConfig(config: ModeConfig): boolean {
  const d = config.delegate
  const b = config.debate

  return (
    (!d ||
      (validRole(d.orchestrator) &&
        Array.isArray(d.workers) &&
        d.workers.length >= 1 &&
        d.workers.length <= 4 &&
        d.workers.every(worker => validRole(worker) && !!worker.lane?.trim()) &&
        Number.isInteger(d.max_iterations) &&
        d.max_iterations >= 1 &&
        d.max_iterations <= 4 &&
        Number.isInteger(d.max_concurrency) &&
        d.max_concurrency >= 1 &&
        d.max_concurrency <= d.workers.length &&
        d.permissions === 'read-only')) &&
    (!b ||
      (validRole(b.side_a) &&
        validRole(b.side_b) &&
        validRole(b.judge) &&
        Number.isInteger(b.rounds) &&
        b.rounds >= 1 &&
        b.rounds <= 3))
  )
}

export function readModeConfig(scope: string): ModeConfig {
  const value = parse<ModeConfig>(`${PREFIX}config.${scope}`, {})

  return value && typeof value === 'object' && !Array.isArray(value) && validConfig(value) ? value : {}
}

export function saveModeConfig(scope: string, config: ModeConfig): void {
  if (!validConfig(config)) {
    throw new Error('Invalid agent mode configuration')
  }
  localStorage.setItem(`${PREFIX}config.${scope}`, JSON.stringify(config))
  bump()
}

export function modeAvailable(scope: string, mode: AgentMode): boolean {
  return mode === 'normal' || !!supported.get(scope)?.has(mode)
}

/** Capability is backend-advertised, never inferred from a successful RPC or version string. */
export async function probeModeSupport(scope: string, request: () => Promise<unknown>): Promise<void> {
  const generation = (probeGeneration.get(scope) ?? 0) + 1
  probeGeneration.set(scope, generation)
  supported.delete(scope)
  bump()

  try {
    const result = (await request()) as { execution_modes?: unknown }

    if (probeGeneration.get(scope) !== generation) {
      return
    }
    const modes = Array.isArray(result?.execution_modes) ? result.execution_modes : []
    supported.set(scope, new Set(modes.filter((mode): mode is AgentMode => MODES.includes(mode))))
  } catch {
    if (probeGeneration.get(scope) === generation) {
      supported.set(scope, new Set())
    }
  }

  bump()
}

export function invalidateModeSupport(scope: string): void {
  probeGeneration.set(scope, (probeGeneration.get(scope) ?? 0) + 1)
  supported.delete(scope)
  bump()
}

export function resetModeSupport(): void {
  for (const scope of probeGeneration.keys()) {
    invalidateModeSupport(scope)
  }
  supported.clear()
  bump()
}

export function dropModePreferences(profile: string, connectionId: string | null): void {
  const scope = modeScopeKey(connectionId, profile)
  localStorage.removeItem(`${PREFIX}choice.${scope}`)
  localStorage.removeItem(`${PREFIX}config.${scope}`)
  invalidateModeSupport(scope)
}

export function migrateModePreferences(from: string, to: string): void {
  const oldScope = modeScopeKey('local', from)
  const newScope = modeScopeKey('local', to)

  if (oldScope === newScope) {
    return
  }

  for (const field of ['choice', 'config']) {
    const oldKey = `${PREFIX}${field}.${oldScope}`
    const newKey = `${PREFIX}${field}.${newScope}`
    const value = localStorage.getItem(oldKey)

    if (value !== null && localStorage.getItem(newKey) === null) {
      localStorage.setItem(newKey, value)
    }
    localStorage.removeItem(oldKey)
  }

  invalidateModeSupport(oldScope)
}

/**
 * Delegate/Debate apply to one message: an accepted send returns the scope to
 * Normal so later follow-ups keep the main agent's tools, persona and history.
 * A refused send keeps the choice so the user can retry the same draft.
 */
export async function sendOnceWithMode(scope: string | undefined, send: () => boolean | Promise<boolean>): Promise<boolean> {
  const accepted = await send()

  if (accepted && scope && selectedMode(scope) !== 'normal') {
    setModeChoice(scope, 'normal')
  }

  return accepted
}

export function modePayload(scope: string, choice = selectedMode(scope)): Record<string, unknown> {
  if (choice === 'normal') {
    return {}
  }

  if (!modeAvailable(scope, choice)) {
    throw new Error(`${choice} mode is unavailable on this backend.`)
  }
  const options = readModeConfig(scope)[choice]

  if (!options) {
    throw new Error(`${choice} mode needs valid settings before sending.`)
  }

  return { execution_mode: choice, mode_options: structuredClone(options) }
}
