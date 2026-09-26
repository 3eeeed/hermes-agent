import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { modeScopeKey, readModeConfig, resetModeSupport } from '@/store/agent-modes'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({
    providers: [
      { slug: 'nous', name: 'Nous', authenticated: true, models: ['hermes-4'] },
      { slug: 'anthropic', name: 'Anthropic', authenticated: true, models: ['claude-sonnet-5'] }
    ]
  }),
  getCredentialPool: vi.fn().mockResolvedValue({
    providers: [
      {
        provider: 'anthropic',
        entries: [
          { id: 'acc-eid', label: 'Eid', email: null, auth_type: 'oauth', last_status: 'ok', priority: 0, request_count: 1, source: 'x' },
          { id: 'acc-hadota', label: 'Hadota', email: null, auth_type: 'oauth', last_status: null, priority: 1, request_count: 0, source: 'x' }
        ]
      }
    ]
  })
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

  it('lets a worker pin one registered account of its provider', async () => {
    const scope = modeScopeKey('local', 'default')
    render(<AgentModesSettings connectionId="local" profile="default" />)
    await screen.findAllByRole('option', { name: 'Nous' })

    fireEvent.change(screen.getByLabelText('Worker 1 provider'), { target: { value: 'nous' } })
    expect(screen.queryByLabelText('Worker 1 account')).toBeNull() // no accounts registered for Nous

    fireEvent.change(screen.getByLabelText('Worker 1 provider'), { target: { value: 'anthropic' } })
    fireEvent.change(screen.getByLabelText('Worker 1 model'), { target: { value: 'claude-sonnet-5' } })
    const account = await screen.findByLabelText('Worker 1 account')
    expect(screen.getByRole('option', { name: 'Automatic (rotate accounts)' })).toBeTruthy()
    fireEvent.change(account, { target: { value: 'acc-hadota' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save Delegate settings' }))
    await waitFor(() => expect(readModeConfig(scope).delegate?.workers[0]).toMatchObject({ credential_id: 'acc-hadota' }))

    // Changing provider drops the account: it belonged to the old provider's pool.
    fireEvent.change(screen.getByLabelText('Worker 1 provider'), { target: { value: 'nous' } })
    fireEvent.change(screen.getByLabelText('Worker 1 model'), { target: { value: 'hermes-4' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Delegate settings' }))
    await waitFor(() => expect(readModeConfig(scope).delegate?.workers[0]?.credential_id).toBeUndefined())
  })
})
