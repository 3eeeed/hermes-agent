import { spawn, type StdioOptions } from 'node:child_process'
import { closeSync, fstatSync } from 'node:fs'
import * as inspector from 'node:inspector'

import { LAUNCHER_READY_FD_ENV } from './linux-launcher-ready'
import { WSLG_AUTO_WAYLAND_ENV, WSLG_X11_FALLBACK_EXIT_CODE } from './wslg-launch'

/**
 * Spawn the app while leaving this process alive only as its supervisor.
 *
 * `retainReadyFd` keeps our copy of the launcher ready pipe (and its env
 * value) for a later respawn; the final spawn must release it.
 */
export function spawnWslgLaunch(args: string[], extraEnv: NodeJS.ProcessEnv = {}, retainReadyFd = false) {
  const env = { ...process.env, ...extraEnv }
  const raw = env[LAUNCHER_READY_FD_ENV]
  delete env[LAUNCHER_READY_FD_ENV]

  if (!retainReadyFd) {
    delete process.env[LAUNCHER_READY_FD_ENV]
  }

  let readyFd: number | undefined

  // Do not reinterpret empty/hex/fractional values, inherit stdio as a ready
  // pipe, or allocate a sparse stdio array from an untrusted descriptor number.
  if (raw !== undefined && /^\d+$/.test(raw)) {
    const fd = Number(raw)

    if (Number.isSafeInteger(fd) && fd >= 3) {
      try {
        fstatSync(fd)
        readyFd = fd
      } catch {
        // A stale descriptor must not prevent the desktop from starting.
      }
    }
  }

  const stdio: StdioOptions = ['inherit', 'inherit', 'inherit']

  if (readyFd !== undefined) {
    stdio.push(readyFd)
    env[LAUNCHER_READY_FD_ENV] = '3'
  }

  try {
    // Keep the original inspector argv/env for the app, but release the port
    // synchronously first: the waiting supervisor must not own its debugger.
    if (inspector.url()) {
      inspector.close()
    }

    return spawn(process.execPath, args, { stdio, env })
  } finally {
    // spawn duplicates the fd synchronously. Keeping our copy would suppress
    // EOF at the outer launcher until the entire desktop exits (also on error).
    if (readyFd !== undefined && !retainReadyFd) {
      closeSync(readyFd)
    }
  }
}

/**
 * Run the app under this supervisor and report its exit code.
 *
 * A default Wayland pick gets one X11 retry when the child's renderer never
 * launches (#114615). This supervisor is the only process that outlives the
 * child, so the child reports that with WSLG_X11_FALLBACK_EXIT_CODE and the
 * retry happens here. The marker env is what lets the child ask; the retry
 * runs without it, so the fallback cannot loop.
 */
export function superviseWslgLaunch(args: string[], fallback: string[] | null, exit: (code: number) => void) {
  let child = spawnWslgLaunch(args, fallback ? { [WSLG_AUTO_WAYLAND_ENV]: '1' } : {}, fallback !== null)

  const supervise = () => {
    child.once('error', error => {
      console.error('[hermes] WSLg launch failed:', error)
      exit(1)
    })
    child.once('exit', code => {
      if (fallback && code === WSLG_X11_FALLBACK_EXIT_CODE) {
        console.warn('[hermes] renderer never launched under WSLg Wayland; retrying once with --ozone-platform=x11 (#114615)')
        child = spawnWslgLaunch(fallback)
        fallback = null
        supervise()

        return
      }

      exit(code ?? 1)
    })
  }

  supervise()

  for (const signal of ['SIGINT', 'SIGTERM'] as const) {
    process.once(signal, () => child.kill(signal))
  }
}
