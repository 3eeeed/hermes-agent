// Conditional-GET cache for Hermes backend JSON (ETag / 304 / chunk delta).
//
// The renderer re-polls JSON (transcript tails, the sidebar, cron jobs, usage)
// every few seconds. The backend tags JSON GETs with a weak ETag; we replay the
// tag as If-None-Match and:
//  - on 304, hand back the body we already hold;
//  - on a 200 carrying X-Hermes-Delta-Base, rebuild the new body from the held
//    base plus the delta ops, and accept it only if its SHA-256 matches the new
//    ETag (otherwise the caller refetches in full).
// Over a remote SSH tunnel this turned ~90 MB/hour of repeated bodies into
// empty 304s and ~1 KB deltas. Bounded LRU, keyed by URL + auth identity so two
// backends/profiles sharing a path never cross-serve.

import { createHash } from 'node:crypto'

const MAX_ENTRIES = 256
const MAX_BODY_CHARS = 4 * 1024 * 1024
// Must match hermes_cli/web_etag.py::_CHUNK_BOUNDARY.
const CHUNK_BOUNDARY = /(?=\{"id":)/

interface Entry {
  etag: string
  text: string
}

type DeltaOp = [number, number] | string

export function etagFor(text: string): string {
  return `W/"${createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 32)}"`
}

export function applyDelta(base: string, ops: DeltaOp[]): string {
  const chunks = base.split(CHUNK_BOUNDARY).filter(Boolean)
  let out = ''

  for (const op of ops) {
    if (typeof op === 'string') {
      out += op
    } else {
      out += chunks.slice(op[0], op[0] + op[1]).join('')
    }
  }

  return out
}

export class JsonEtagCache {
  private entries = new Map<string, Entry>()

  key(url: string, authIdentity: string | null | undefined): string {
    return `${authIdentity || ''}\u0000${url}`
  }

  /** Headers to send for this GET: the held tag plus the delta opt-in. */
  conditionalHeaders(key: string): Record<string, string> {
    const hit = this.entries.get(key)

    return hit ? { 'If-None-Match': hit.etag, 'X-Hermes-Delta': '1' } : {}
  }

  /** Cached body text for a 304, refreshing its LRU position; null if evicted. */
  revalidated(key: string): string | null {
    const hit = this.entries.get(key)

    if (!hit) {
      return null
    }

    this.entries.delete(key)
    this.entries.set(key, hit)

    return hit.text
  }

  /**
   * Rebuild a delta response. Returns the new body (and stores it) only when the
   * held base is the one the server diffed against and the result hashes to the
   * new ETag; null means "refetch without the tag".
   */
  resolveDelta(
    key: string,
    baseTag: string | string[] | undefined,
    newTag: string | string[] | undefined,
    deltaText: string
  ): string | null {
    const base = Array.isArray(baseTag) ? baseTag[0] : baseTag
    const tag = Array.isArray(newTag) ? newTag[0] : newTag
    const hit = this.entries.get(key)

    if (!hit || !base || !tag || hit.etag !== base) {
      this.forget(key)

      return null
    }

    let text: string

    try {
      text = applyDelta(hit.text, JSON.parse(deltaText))
    } catch {
      this.forget(key)

      return null
    }

    if (etagFor(text) !== tag) {
      this.forget(key)

      return null
    }

    this.store(key, tag, text)

    return text
  }

  /** Remember a fresh 200 body when the server tagged it. */
  store(key: string, etag: string | string[] | undefined, text: string): void {
    const tag = Array.isArray(etag) ? etag[0] : etag

    if (!tag || text.length > MAX_BODY_CHARS) {
      this.entries.delete(key)

      return
    }

    this.entries.delete(key)
    this.entries.set(key, { etag: tag, text })

    while (this.entries.size > MAX_ENTRIES) {
      const oldest = this.entries.keys().next().value

      if (oldest === undefined) {
        break
      }

      this.entries.delete(oldest)
    }
  }

  forget(key: string): void {
    this.entries.delete(key)
  }

  get size(): number {
    return this.entries.size
  }
}
