import { app } from 'electron'

import { WSLG_AUTO_WAYLAND_ENV, WSLG_X11_FALLBACK_EXIT_CODE, wslgLaunchArgs, wslgX11FallbackArgs } from './wslg-launch'
import { spawnWslgLaunch } from './wslg-launch-process'

const args = wslgLaunchArgs(process.argv.slice(1), process.env, process.platform)

if (args) {
  // Keep the launcher alive until the child exits: npm's concurrently must not
  // tear down Vite during this handoff. No backend, windows or single-instance
  // lock are created in this parent. The child has an explicit platform flag,
  // so it goes straight into main on its first pass.
  //
  // A default Wayland pick gets one X11 retry when the child's renderer never
  // launches (#114615). This supervisor is the only process that outlives the
  // child, so the child reports that with WSLG_X11_FALLBACK_EXIT_CODE and the
  // retry happens here. The marker env is what lets the child ask; the retry
  // runs without it, so the fallback cannot loop.
  let fallback = wslgX11FallbackArgs(args, process.env)
  let child = spawnWslgLaunch(args, fallback ? { [WSLG_AUTO_WAYLAND_ENV]: '1' } : {})

  const supervise = () => {
    child.once('error', error => {
      console.error('[hermes] WSLg launch failed:', error)
      app.exit(1)
    })
    child.once('exit', code => {
      if (fallback && code === WSLG_X11_FALLBACK_EXIT_CODE) {
        console.warn('[hermes] renderer never launched under WSLg Wayland; retrying once with --ozone-platform=x11 (#114615)')
        child = spawnWslgLaunch(fallback)
        fallback = null
        supervise()

        return
      }

      app.exit(code ?? 1)
    })
  }

  supervise()

  for (const signal of ['SIGINT', 'SIGTERM'] as const) {
    process.once(signal, () => child.kill(signal))
  }
} else {
  await import('./main')
}
