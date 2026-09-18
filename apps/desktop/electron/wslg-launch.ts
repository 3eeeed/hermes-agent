import { detectRemoteDisplay, isWslEnvironment } from './bootstrap-platform'

/** Set on the supervised child when Wayland was picked by default, not by a hint. */
export const WSLG_AUTO_WAYLAND_ENV = 'HERMES_WSLG_AUTO_WAYLAND'

/** Child exit code asking the supervisor for one X11 re-exec: the renderer never launched under Wayland. */
export const WSLG_X11_FALLBACK_EXIT_CODE = 76

function ozoneHint(argv: readonly string[], env: NodeJS.ProcessEnv): string | undefined {
  const hintArg = argv.findLast(arg => arg.startsWith('--ozone-platform-hint='))

  return hintArg?.split('=')[1] ?? env.ELECTRON_OZONE_PLATFORM_HINT
}

// Ozone is selected before application JavaScript. Never appendSwitch here:
// that leaves the browser on X11 while GPU children receive Wayland.
export function wslgLaunchArgs(
  argv: readonly string[],
  env: NodeJS.ProcessEnv,
  platform: NodeJS.Platform,
  isWsl = isWslEnvironment(env, platform)
): string[] | null {
  const displayEnv = { ...env, HERMES_DESKTOP_DISABLE_GPU: undefined }

  if (platform !== 'linux' || !isWsl || !env.WAYLAND_DISPLAY || detectRemoteDisplay({ env: displayEnv, platform })) {
    return null
  }

  if (argv.some(arg => arg === '--ozone-platform' || arg.startsWith('--ozone-platform='))) {
    return null
  }

  const backend = ozoneHint(argv, env) === 'x11' ? 'x11' : 'wayland'

  return [...argv, `--ozone-platform=${backend}`]
}

/**
 * The one X11 retry for a supervised launch whose renderer never started
 * (#114615). Only a default Wayland pick is retried: an explicit hint, from
 * argv or the config-bridged env, is the user's decision and is kept.
 */
export function wslgX11FallbackArgs(args: readonly string[], env: NodeJS.ProcessEnv): string[] | null {
  if (!args.includes('--ozone-platform=wayland') || ozoneHint(args, env) === 'wayland') {
    return null
  }

  return args.map(arg => (arg === '--ozone-platform=wayland' ? '--ozone-platform=x11' : arg))
}

/**
 * Child side: a renderer that failed to launch under a default Wayland pick
 * hands the process back to the supervisor. The X11 retry runs without the
 * marker, so it can never ask again.
 */
export function shouldRequestWslgX11Fallback(details: { reason?: string } | undefined, env: NodeJS.ProcessEnv): boolean {
  return env[WSLG_AUTO_WAYLAND_ENV] === '1' && details?.reason === 'launch-failed'
}
