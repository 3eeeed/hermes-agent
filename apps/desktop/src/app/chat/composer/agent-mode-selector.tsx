import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import {
  $modeRevision,
  invalidateModeSupport,
  modeAvailable,
  modeScopeKey,
  probeModeSupport,
  readModeConfig,
  selectedMode,
  setModeChoice
} from '@/store/agent-modes'
import { requestGatewayForAgent } from '@/store/gateway'
import { $gatewayState } from '@/store/session'

export function AgentModeSelector({
  connectionId,
  profile,
  disabled = false
}: {
  connectionId: string | null
  profile: string
  disabled?: boolean
}) {
  useStore($modeRevision)
  const gatewayState = useStore($gatewayState)
  const scope = modeScopeKey(connectionId, profile)
  useEffect(() => {
    if (gatewayState !== 'open') {
      invalidateModeSupport(scope)

      return
    }

    void probeModeSupport(scope, () => requestGatewayForAgent(connectionId, profile, 'gateway.capabilities'))
  }, [scope, connectionId, profile, gatewayState])
  const choice = selectedMode(scope)
  const config = readModeConfig(scope)
  const available = (mode: 'delegate' | 'debate') => modeAvailable(scope, mode) && !!config[mode]

  return (
    <label className="flex items-center gap-1 text-xs">
      Agent mode
      <select
        aria-label="Agent mode"
        disabled={disabled}
        onChange={event => setModeChoice(scope, event.target.value as typeof choice)}
        value={choice}
      >
        <option value="normal">Normal</option>
        <option disabled={!available('delegate') && choice !== 'delegate'} value="delegate">
          Delegate{available('delegate') ? '' : ' (unavailable)'}
        </option>
        <option disabled={!available('debate') && choice !== 'debate'} value="debate">
          Debate{available('debate') ? '' : ' (unavailable)'}
        </option>
      </select>
    </label>
  )
}
