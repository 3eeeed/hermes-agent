import { describe, expect, it } from 'vitest'

import { activePoolStatusbarProvider, cacheHitLabel, pooledAccountUsageLabel, tokensPerSecondLabel } from '@/lib/statusbar'

const base = { calls: 0, input: 0, output: 0, total: 0 }

describe('statusbar usage readouts', () => {
  it('paints the backend cache-hit and throughput fields, and stays blank when they are absent', () => {
    // The backend omits both fields (rather than sending 0) when it has no data
    // — a provider with no cache reads, or a session before its first call.
    expect(cacheHitLabel(base)).toBe('')
    expect(tokensPerSecondLabel(base)).toBe('')

    expect(cacheHitLabel({ ...base, cache_hit_pct: 87 })).toBe('87%')
    expect(tokensPerSecondLabel({ ...base, avg_tps: 41.6 })).toBe('42 t/s')
  })

  it('selects only the focused chat provider for the account status item', () => {
    expect(activePoolStatusbarProvider('openai-codex')).toBe('openai-codex')
    expect(activePoolStatusbarProvider('anthropic')).toBe('anthropic')
    expect(activePoolStatusbarProvider('ohh')).toBeNull()
    expect(activePoolStatusbarProvider(null)).toBeNull()
  })

  it('shows the active account and its consumed current-session percentage', () => {
    expect(
      pooledAccountUsageLabel('Claude', 'Team - Ahmed', [
        { detail: null, label: 'Current session', reset_at: null, used_percent: 1 }
      ])
    ).toBe('Claude · Team - Ahmed · 1% used')

    expect(
      pooledAccountUsageLabel('Codex', 'batal', [
        { detail: null, label: 'Session', reset_at: null, used_percent: 5.4 }
      ])
    ).toBe('Codex · batal · 5.4% used')
  })
})
