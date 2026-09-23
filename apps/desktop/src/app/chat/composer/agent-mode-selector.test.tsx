import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

import { modeScopeKey, readModeConfig, resetModeSupport, saveModeConfig, selectedMode } from '@/store/agent-modes'

vi.mock('@/store/gateway', () => ({ requestGatewayForAgent: vi.fn() }))
import { requestGatewayForAgent } from '@/store/gateway'
import { $gatewayState } from '@/store/session'

import { AgentModeSelector } from './agent-mode-selector'

beforeEach(() => {
  localStorage.clear()
  resetModeSupport()
  $gatewayState.set('open')
  vi.mocked(requestGatewayForAgent).mockReset()
})

it('keeps unsupported modes disabled and does not change a draft', async () => {
  vi.mocked(requestGatewayForAgent).mockResolvedValue({})
  const scope = modeScopeKey('local', 'default')
  render(
    <>
      <input aria-label="Draft" defaultValue="do not touch" />
      <AgentModeSelector connectionId="local" profile="default" />
    </>
  )
  await waitFor(() =>
    expect((screen.getByRole('option', { name: /delegate/i }) as HTMLOptionElement).disabled).toBe(true)
  )
  expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Draft' }).value).toBe('do not touch')
  expect(selectedMode(scope)).toBe('normal')
})

it('lets an advertised configured mode be chosen for future turns', async () => {
  const scope = modeScopeKey('remote', 'research')
  saveModeConfig(scope, {
    debate: {
      side_a: { provider: 'nous', model: 'a' },
      side_b: { provider: 'nous', model: 'b' },
      judge: { provider: 'nous', model: 'j' },
      rounds: 1
    }
  })
  vi.mocked(requestGatewayForAgent).mockResolvedValue({ execution_modes: ['normal', 'debate'] })
  render(<AgentModeSelector connectionId="remote" profile="research" />)
  await waitFor(() =>
    expect((screen.getByRole('option', { name: /debate/i }) as HTMLOptionElement).disabled).toBe(false)
  )
  fireEvent.change(screen.getByRole('combobox', { name: 'Agent mode' }), { target: { value: 'debate' } })
  expect(selectedMode(scope)).toBe('debate')
  expect(readModeConfig(scope).debate?.rounds).toBe(1)
})
