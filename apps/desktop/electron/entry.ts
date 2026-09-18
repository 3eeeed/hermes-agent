import { app } from 'electron'

import { wslgLaunchArgs, wslgX11FallbackArgs } from './wslg-launch'
import { superviseWslgLaunch } from './wslg-launch-process'

const args = wslgLaunchArgs(process.argv.slice(1), process.env, process.platform)

if (args) {
  // Keep the launcher alive until the child exits: npm's concurrently must not
  // tear down Vite during this handoff. No backend, windows or single-instance
  // lock are created in this parent. The child has an explicit platform flag,
  // so it goes straight into main on its first pass.
  superviseWslgLaunch(args, wslgX11FallbackArgs(args, process.env), code => app.exit(code))
} else {
  await import('./main')
}
