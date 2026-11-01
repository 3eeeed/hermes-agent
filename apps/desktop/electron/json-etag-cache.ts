// Conditional-GET cache for Hermes backend JSON (ETag / 304).
//
// The renderer re-polls identical JSON (transcript tails, cron jobs, usage)
// every few seconds. The backend tags JSON GETs with a weak ETag; we replay the
// tag as If-None-Match and, on 304, hand back the body we already parsed. Over
// a remote SSH tunnel this turned ~90 MB/hour of identical bodies into empty
// 304s. Bounded LRU, keyed by URL + auth identity so two backends/profiles
// sharing a path never cross-serve.

const MAX_ENTRIES = 256
const MAX_BODY_CHARS = 4 * 1024 * 1024

interface Entry {
  etag: string
  text: string
}

export class JsonEtagCache {
  private entries = new Map<string, Entry>()

  key(url: string, authIdentity: string | null | undefined): string {
    return `${authIdentity || ''}\u0000${url}`
  }

  /** Header to send for this GET, or null when nothing is cached. */
  conditionalHeaders(key: string): Record<string, string> {
    const hit = this.entries.get(key)

    return hit ? { 'If-None-Match': hit.etag } : {}
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
