import dns from 'node:dns'
import net from 'node:net'

export interface PublicLookupAddress {
  address: string
  family: number | string
}

export type PublicLookup = (hostname: string) => Promise<readonly PublicLookupAddress[]>

const blockedAddresses = new net.BlockList()

for (const [network, prefix] of [
  ['0.0.0.0', 8],
  ['10.0.0.0', 8],
  ['100.64.0.0', 10],
  ['127.0.0.0', 8],
  ['169.254.0.0', 16],
  ['172.16.0.0', 12],
  ['192.0.0.0', 24],
  ['192.0.2.0', 24],
  ['192.168.0.0', 16],
  ['198.18.0.0', 15],
  ['198.51.100.0', 24],
  ['203.0.113.0', 24],
  ['224.0.0.0', 4],
  ['240.0.0.0', 4]
] as const) {
  blockedAddresses.addSubnet(network, prefix, 'ipv4')
}

for (const [network, prefix] of [
  ['::', 128],
  ['::1', 128],
  ['64:ff9b::', 96],
  ['100::', 64],
  ['2001:db8::', 32],
  ['fc00::', 7],
  ['fe80::', 10],
  ['ff00::', 8]
] as const) {
  blockedAddresses.addSubnet(network, prefix, 'ipv6')
}

const defaultLookup: PublicLookup = async hostname =>
  dns.promises.lookup(hostname, { all: true, verbatim: true }) as Promise<PublicLookupAddress[]>

function normalizedIp(value: string): string {
  return value.replace(/^\[|\]$/g, '').split('%', 1)[0] || value
}

function isPublicAddress(value: string): boolean {
  const address = normalizedIp(value)
  const family = net.isIP(address)

  return family !== 0 && !blockedAddresses.check(address, family === 6 ? 'ipv6' : 'ipv4')
}

export type SocketLookup = (
  hostname: string,
  options: dns.LookupAllOptions,
  callback: (error: Error | null, addresses: dns.LookupAddress[]) => void
) => void

// Node resolves the hostname again when the socket connects, so a validated
// URL alone cannot stop DNS rebinding: the second answer may point at
// loopback or private space. Wrapping the socket-level lookup re-checks the
// addresses actually being connected to, which closes the TOCTOU window.
export function publicSocketLookup(lookupImpl: SocketLookup = dns.lookup as unknown as SocketLookup): SocketLookup {
  return (hostname, options, callback) => {
    lookupImpl(hostname, { ...options, all: true }, (error, addresses) => {
      if (error) {
        callback(error, addresses)

        return
      }

      const resolved = Array.isArray(addresses) ? addresses : [addresses]

      if (resolved.length === 0 || resolved.some(entry => !isPublicAddress(entry.address))) {
        callback(new Error('File download URL must resolve only to public network addresses'), [])

        return
      }

      callback(null, resolved)
    })
  }
}

export interface NodePublicFetchOptions {
  abandonedBodyGraceMs?: number
  lookup?: PublicLookup
  signal?: AbortSignal
  socketLookup?: SocketLookup
}

// Downloads through an undici Agent whose socket-level lookup is guarded, so
// the address that is finally connected to is re-checked. Passing a validated
// URL to a plain fetch is not enough on its own: Chromium/Node resolve the
// hostname again, which is exactly the DNS-rebinding window.
export async function nodePublicFetch(rawUrl: string, options: NodePublicFetchOptions = {}): Promise<Response> {
  const socketLookup = options.socketLookup || publicSocketLookup()
  const { Agent } = await import('undici')
  const agent = new Agent({ connect: { lookup: socketLookup as never } })

  const closeAgent = () => {
    void agent.close().catch(() => undefined)
  }

  let response: Response

  try {
    response = await fetchPublicDownload(rawUrl, {
      fetchImpl: (input, init) => fetch(String(input), { ...init, dispatcher: agent } as RequestInit),
      lookup: options.lookup,
      signal: options.signal
    })
  } catch (error) {
    closeAgent()

    // undici reports connect-time failures as an opaque "fetch failed"; surface
    // the guard's reason so blocked private destinations are diagnosable.
    const cause = (error as { cause?: unknown })?.cause

    throw cause instanceof Error && /public network/i.test(cause.message) ? cause : error
  }

  if (!response.body) {
    closeAgent()

    return response
  }

  // The agent must outlive the response: closing it in a finally block tears
  // the connection down before a streamed body is consumed, which stalls every
  // download larger than one buffer. Close once the body is finished instead.
  //
  // A caller can also drop the response without reading it (the cache layer
  // does exactly that when a size or status check rejects a download). Nothing
  // would then close the agent, so the socket stays open. An idle watchdog
  // cancels a body that stops being read before it completes.
  const reader = response.body.getReader()
  const graceMs = options.abandonedBodyGraceMs ?? 30_000
  let settled = false
  let idleTimer: ReturnType<typeof setTimeout> | undefined

  const settle = () => {
    if (settled) {
      return
    }

    settled = true
    clearTimeout(idleTimer)
    closeAgent()
  }

  const armIdleWatchdog = () => {
    clearTimeout(idleTimer)
    idleTimer = setTimeout(() => {
      void reader.cancel(new Error('Response body was abandoned before completion')).catch(() => undefined)
      settle()
    }, graceMs)
    idleTimer.unref?.()
  }

  armIdleWatchdog()

  const body = new ReadableStream({
    cancel: reason => {
      settle()

      return reader.cancel(reason)
    },
    pull: async controller => {
      try {
        const { done, value } = await reader.read()

        if (done) {
          controller.close()
          settle()

          return
        }

        controller.enqueue(value)
        // Progress resets the watchdog, so only a stalled or dropped consumer
        // trips it.
        armIdleWatchdog()
      } catch (error) {
        controller.error(error)
        settle()
      }
    }
  })

  return new Response(body, {
    headers: response.headers,
    status: response.status,
    statusText: response.statusText
  })
}

export interface PublicFetchOptions {
  fetchImpl?: (input: string | URL, init?: RequestInit) => Promise<Response>
  lookup?: PublicLookup
  maxRedirects?: number
  signal?: AbortSignal
}

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308])

export async function fetchPublicDownload(rawUrl: string, options: PublicFetchOptions = {}): Promise<Response> {
  const fetchImpl = options.fetchImpl || fetch
  const maxRedirects = options.maxRedirects ?? 5
  let current = new URL(rawUrl)

  for (let redirects = 0; redirects <= maxRedirects; redirects += 1) {
    current = await assertPublicDownloadUrl(current.toString(), options.lookup)
    const response = await fetchImpl(current, { redirect: 'manual', signal: options.signal })

    if (!REDIRECT_STATUSES.has(response.status)) {
      return response
    }

    // Release the redirect hop's connection: an unread body keeps the socket
    // checked out of the pool for the lifetime of the process.
    await response.body?.cancel().catch(() => undefined)

    const location = response.headers.get('location')

    if (!location || redirects === maxRedirects) {
      throw new Error('Remote file download exceeded the redirect limit')
    }

    current = new URL(location, current)
  }

  throw new Error('Remote file download exceeded the redirect limit')
}

export async function assertPublicDownloadUrl(rawUrl: string, lookup: PublicLookup = defaultLookup): Promise<URL> {
  const url = new URL(rawUrl)

  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('File download URL must use an unauthenticated HTTP or HTTPS public network destination')
  }

  const hostname = normalizedIp(url.hostname).toLowerCase()

  if (!hostname || hostname === 'localhost' || hostname.endsWith('.localhost') || hostname.endsWith('.local')) {
    throw new Error('File download URL must use a public network destination')
  }

  const literalFamily = net.isIP(hostname)
  const addresses = literalFamily ? [{ address: hostname, family: literalFamily }] : await lookup(hostname)

  if (addresses.length === 0 || addresses.some(result => !isPublicAddress(result.address))) {
    throw new Error('File download URL must resolve only to public network addresses')
  }

  return url
}
