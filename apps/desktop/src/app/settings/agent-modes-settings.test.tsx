import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { modeScopeKey, readModeConfig, resetModeSupport } from '@/store/agent-modes'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi
    .fn()
    .mockResolvedValue({ providers: [{ slug: 'nous', name: 'Nous', authenticated: true, models: ['hermes-4'] }] })
}))
vi.mock('@/store/gateway', () => ({
  requestGatewayForAgent: vi.fn().mockResolvedValue({ execution_modes: ['delegate', 'debate'] })
}))

import { AgentModesSettings } from './agent-modes-settings'

beforeEach(() => {
  localStorage.clear()
  resetModeSupport()
})

describe('agent mode settings', () => {
  it('keeps an incomplete edit locally until an explicit valid save', async () => {
    const scope = modeScopeKey('local', 'default')
    render(<AgentModesSettings connectionId="local" profile="default" />)
    await screen.findAllByRole('option', { name: 'Nous' })
    fireEvent.click(screen.getByRole('button', { name: 'Save Delegate settings' }))
    expect(readModeConfig(scope)).toEqual({})
    expect(screen.getByRole('alert').textContent).toMatch(/choose/i)
  })

  it('persists only this connection/profile after valid role selections', async () => {
    const scope = modeScopeKey('remote', 'research')
    render(<AgentModesSettings connectionId="remote" profile="research" />)
    await screen.findAllByRole('option', { name: 'Nous' })

    fireEvent.change(screen.getByLabelText('Worker 1 provider'), { target: { value: 'nous' } })
    fireEvent.change(screen.getByLabelText('Worker 1 model'), { target: { value: 'hermes-4' } })
    fireEvent.change(screen.getByLabelText('Worker 1 effort'), { target: { value: 'low' } })
    fireEvent.change(screen.getByLabelText('Orchestrator effort'), { target: { value: 'high' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save Delegate settings' }))
    await waitFor(() => expect(readModeConfig(scope).delegate?.workers).toHaveLength(1))
    expect(readModeConfig(scope).delegate).toMatchObject({ orchestrator: { effort: 'high' }, workers: [{ effort: 'low' }] })
    expect(readModeConfig(modeScopeKey('local', 'research'))).toEqual({})
  })
})
