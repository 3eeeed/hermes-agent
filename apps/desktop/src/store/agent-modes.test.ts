import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  dropModePreferences,
  migrateModePreferences,
  modePayload,
  modeScopeKey,
  probeModeSupport,
  readModeConfig,
  resetModeSupport,
  saveModeConfig,
  selectedMode,
  setModeChoice
} from './agent-modes'

beforeEach(() => {
  localStorage.clear()
  resetModeSupport()
})

describe('agent mode scope and wire contract', () => {
  it('keeps connection and profile preferences independent, with normal omitted on the wire', () => {
    const a = modeScopeKey('local', 'alpha')
    const b = modeScopeKey('remote', 'alpha')
    const c = modeScopeKey('local', 'beta')
    setModeChoice(a, 'delegate')
    expect(selectedMode(a)).toBe('delegate')
    expect(selectedMode(b)).toBe('normal')
    expect(selectedMode(c)).toBe('normal')
    expect(modePayload(a, 'normal')).toEqual({})
    setModeChoice(a, 'normal')
    expect(localStorage.getItem(`hermes.desktop.agent-modes.choice.${a}`)).toBeNull()
  })

  it('only exposes a configured mode after this scoped backend advertises support', async () => {
    const key = modeScopeKey('remote', 'alpha')
    saveModeConfig(key, {
      delegate: {
        orchestrator: { effort: 'high' },
        workers: [{ lane: 'impl', provider: 'nous', model: 'model', effort: 'low' }]
      }
    })
    setModeChoice(key, 'delegate')
    await probeModeSupport(key, vi.fn().mockResolvedValue({ execution_modes: ['normal', 'delegate'] }))
    expect(modePayload(key)).toEqual({ execution_mode: 'delegate', mode_options: readModeConfig(key).delegate })
    expect(modePayload(modeScopeKey('local', 'alpha'))).toEqual({})
  })

  it('rejects malformed persisted config, invalid bounds and unsupported backend without silently sending normal', async () => {
    const key = modeScopeKey('local', 'alpha')
    localStorage.setItem(`hermes.desktop.agent-modes.config.${key}`, '{broken')
    expect(readModeConfig(key)).toEqual({})
    expect(() => saveModeConfig(key, { debate: { rounds: 4 } as never })).toThrow()
    setModeChoice(key, 'debate')
    await probeModeSupport(key, vi.fn().mockResolvedValue({ per_session_exclusive_submit: true }))
    expect(() => modePayload(key)).toThrow(/unavailable/i)
  })

  it('migrates and drops only local profile preferences, never same-name remote preferences', () => {
    const oldLocal = modeScopeKey('local', 'old')
    const newLocal = modeScopeKey('local', 'new')
    const remote = modeScopeKey('remote', 'old')
    setModeChoice(oldLocal, 'delegate')
    setModeChoice(remote, 'debate')
    migrateModePreferences('old', 'new')
    expect(selectedMode(newLocal)).toBe('delegate')
    expect(selectedMode(oldLocal)).toBe('normal')
    expect(selectedMode(remote)).toBe('debate')
    dropModePreferences('new', 'local')
    expect(selectedMode(newLocal)).toBe('normal')
    expect(selectedMode(remote)).toBe('debate')
  })
  it('ignores an older capability response when a later probe reports no support', async () => {
    const key = modeScopeKey('remote', 'alpha')
    let resolveOld!: (value: unknown) => void
    const old = probeModeSupport(
      key,
      () =>
        new Promise(resolve => {
          resolveOld = resolve
        })
    )
    const recent = probeModeSupport(key, async () => ({}))
    await recent
    resolveOld({ execution_modes: ['delegate'] })
    await old
    expect(() => modePayload(key, 'delegate')).toThrow(/unavailable/i)
  })
})
