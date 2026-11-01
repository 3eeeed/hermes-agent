import { afterEach, describe, expect, it, vi } from 'vitest'

import { createViewedGate } from './use-background-sync'

// Change-driven refreshes (sessions.changed / cron.changed) must not pull data
// for a window nobody is looking at, and must catch up exactly once on return.
describe('createViewedGate', () => {
  const gates: Array<{ dispose: () => void }> = []

  afterEach(() => {
    gates.splice(0).forEach(gate => gate.dispose())
  })

  function gateWith(viewed: { value: boolean }) {
    const run = vi.fn()
    const gate = createViewedGate(run, () => viewed.value)
    gates.push(gate)

    return { gate, run }
  }

  it('runs immediately while the window is viewed', () => {
    const { gate, run } = gateWith({ value: true })

    gate.request()
    gate.request()

    expect(run).toHaveBeenCalledTimes(2)
  })

  it('holds every request while unviewed and catches up once on focus', () => {
    const viewed = { value: false }
    const { gate, run } = gateWith(viewed)

    gate.request()
    gate.request()
    gate.request()
    window.dispatchEvent(new Event('focus'))
    expect(run).not.toHaveBeenCalled()

    viewed.value = true
    window.dispatchEvent(new Event('focus'))
    window.dispatchEvent(new Event('focus'))

    expect(run).toHaveBeenCalledTimes(1)
  })

  it('returning to the window with nothing pending pulls nothing', () => {
    const viewed = { value: false }
    const { run } = gateWith(viewed)

    viewed.value = true
    document.dispatchEvent(new Event('visibilitychange'))

    expect(run).not.toHaveBeenCalled()
  })

  it('a disposed gate never fires a held request', () => {
    const viewed = { value: false }
    const { gate, run } = gateWith(viewed)

    gate.request()
    gate.dispose()
    viewed.value = true
    window.dispatchEvent(new Event('focus'))

    expect(run).not.toHaveBeenCalled()
  })
})
