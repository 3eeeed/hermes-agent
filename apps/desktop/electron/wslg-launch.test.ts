import { describe, expect, it } from 'vitest'

import {
  shouldRequestWslgX11Fallback,
  WSLG_AUTO_WAYLAND_ENV,
  wslgLaunchArgs,
  wslgX11FallbackArgs
} from './wslg-launch'

const env = { WSL_DISTRO_NAME: 'Ubuntu', WAYLAND_DISPLAY: 'wayland-0', DISPLAY: ':0' }

describe('WSLg launch arguments', () => {
  it('preserves the invocation and restarts only once with native Wayland', () => {
    const args = ['.', '--inspect=9229', 'hermes://session/example']
    const next = wslgLaunchArgs(args, env, 'linux')!

    expect(next).toEqual([...args, '--ozone-platform=wayland'])
    expect(wslgLaunchArgs(next, env, 'linux')).toBeNull()
    expect(args).toEqual(['.', '--inspect=9229', 'hermes://session/example'])
  })

  it('respects explicit backends and the existing config-bridged X11 hint', () => {
    for (const args of [['--ozone-platform=x11'], ['--ozone-platform', 'x11']]) {
      expect(wslgLaunchArgs(args, env, 'linux')).toBeNull()
    }

    expect(wslgLaunchArgs(['--ozone-platform-hint=x11'], env, 'linux')).toEqual([
      '--ozone-platform-hint=x11',
      '--ozone-platform=x11'
    ])
    expect(wslgLaunchArgs([], { ...env, HERMES_DESKTOP_DISABLE_GPU: 'true' }, 'linux')).toEqual([
      '--ozone-platform=wayland'
    ])
    expect(wslgLaunchArgs([], { ...env, ELECTRON_OZONE_PLATFORM_HINT: 'x11' }, 'linux')).toEqual([
      '--ozone-platform=x11'
    ])
  })

  it('leaves other platforms, missing Wayland displays and forwarded sessions alone', () => {
    expect(wslgLaunchArgs([], env, 'win32')).toBeNull()
    expect(wslgLaunchArgs([], env, 'darwin')).toBeNull()
    expect(wslgLaunchArgs([], { DISPLAY: ':0' }, 'linux', false)).toBeNull()
    expect(wslgLaunchArgs([], { WSL_DISTRO_NAME: 'Ubuntu' }, 'linux')).toBeNull()
    expect(wslgLaunchArgs([], { ...env, SSH_CONNECTION: 'remote' }, 'linux')).toBeNull()
    expect(wslgLaunchArgs([], { ...env, DISPLAY: 'localhost:10.0' }, 'linux')).toBeNull()
  })
})

describe('WSLg X11 fallback', () => {
  it('retries a default Wayland pick once on X11 and never overrides an explicit hint', () => {
    const args = ['.', '--inspect=9229', 'hermes://session/example']
    const wayland = wslgLaunchArgs(args, env, 'linux')!
    const retry = wslgX11FallbackArgs(wayland, env)!

    expect(retry).toEqual([...args, '--ozone-platform=x11'])
    // The retry carries an explicit platform: it goes straight into main and
    // has no further fallback of its own.
    expect(wslgLaunchArgs(retry, env, 'linux')).toBeNull()
    expect(wslgX11FallbackArgs(retry, env)).toBeNull()

    expect(wslgX11FallbackArgs(wslgLaunchArgs(['--ozone-platform-hint=wayland'], env, 'linux')!, env)).toBeNull()
    const hinted = { ...env, ELECTRON_OZONE_PLATFORM_HINT: 'wayland' }

    expect(wslgX11FallbackArgs(wslgLaunchArgs([], hinted, 'linux')!, hinted)).toBeNull()
  })

  it('lets only a marked child hand back a renderer that never launched', () => {
    const marked = { [WSLG_AUTO_WAYLAND_ENV]: '1' }

    expect(shouldRequestWslgX11Fallback({ reason: 'launch-failed' }, marked)).toBe(true)
    expect(shouldRequestWslgX11Fallback({ reason: 'launch-failed' }, {})).toBe(false)
    expect(shouldRequestWslgX11Fallback({ reason: 'crashed' }, marked)).toBe(false)
    expect(shouldRequestWslgX11Fallback({ reason: 'killed' }, marked)).toBe(false)
    expect(shouldRequestWslgX11Fallback(undefined, marked)).toBe(false)
  })
})
