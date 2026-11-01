import assert from 'node:assert/strict'

import { test } from 'vitest'

import { applyDelta, etagFor, JsonEtagCache } from './json-etag-cache'

// Produced by hermes_cli/web_etag.py (build_delta/_etag_for) so this pins the
// wire contract between the Python server and this client, Arabic text included.
const SERVER_FIXTURE = {
  base: '{"rows":[{"id":"s0","title":"جلسة 0","n":0},{"id":"s1","title":"جلسة 1","n":1},{"id":"s2","title":"جلسة 2","n":2},{"id":"s3","title":"جلسة 3","n":3},{"id":"s4","title":"جلسة 4","n":4},{"id":"s5","title":"جلسة 5","n":5}]}',
  baseTag: 'W/"1232d81eae1ce5e597b1920246a5d2f2"',
  newTag: 'W/"8c911d43522488854d51c4e31966a222"',
  delta: '[[0,1],"{\\"id\\":\\"new\\",\\"title\\":\\"✓\\"},",[1,2],"{\\"id\\":\\"s2\\",\\"title\\":\\"جلسة 2\\",\\"n\\":9},",[4,3]]',
  next: '{"rows":[{"id":"new","title":"✓"},{"id":"s0","title":"جلسة 0","n":0},{"id":"s1","title":"جلسة 1","n":1},{"id":"s2","title":"جلسة 2","n":9},{"id":"s3","title":"جلسة 3","n":3},{"id":"s4","title":"جلسة 4","n":4},{"id":"s5","title":"جلسة 5","n":5}]}'
}

test('a cached tagged body is offered for revalidation (with delta opt-in) and replayed on 304', () => {
  const cache = new JsonEtagCache()
  const key = cache.key('http://127.0.0.1:1/api/sessions/x/messages?limit=120', 'tok-a')

  assert.deepEqual(cache.conditionalHeaders(key), {})
  cache.store(key, 'W/"abc"', '{"messages":[1]}')
  assert.deepEqual(cache.conditionalHeaders(key), { 'If-None-Match': 'W/"abc"', 'X-Hermes-Delta': '1' })
  assert.equal(cache.revalidated(key), '{"messages":[1]}')
})

test('client and server agree on the ETag hash and the delta format', () => {
  assert.equal(etagFor(SERVER_FIXTURE.base), SERVER_FIXTURE.baseTag)
  assert.equal(applyDelta(SERVER_FIXTURE.base, JSON.parse(SERVER_FIXTURE.delta)), SERVER_FIXTURE.next)
})

test('a server delta rebuilds the new body, verifies it and becomes the next base', () => {
  const cache = new JsonEtagCache()
  const key = cache.key('http://h/api/profiles/sessions/sidebar', 't')
  cache.store(key, SERVER_FIXTURE.baseTag, SERVER_FIXTURE.base)

  const text = cache.resolveDelta(key, SERVER_FIXTURE.baseTag, SERVER_FIXTURE.newTag, SERVER_FIXTURE.delta)

  assert.equal(text, SERVER_FIXTURE.next)
  assert.equal(cache.conditionalHeaders(key)['If-None-Match'], SERVER_FIXTURE.newTag)
})

test('a delta against a different base or one that fails verification is rejected and forgotten', () => {
  const cache = new JsonEtagCache()
  const key = cache.key('http://h/api/x', 't')

  cache.store(key, 'W/"other"', SERVER_FIXTURE.base)
  assert.equal(cache.resolveDelta(key, SERVER_FIXTURE.baseTag, SERVER_FIXTURE.newTag, SERVER_FIXTURE.delta), null)
  assert.deepEqual(cache.conditionalHeaders(key), {})

  cache.store(key, SERVER_FIXTURE.baseTag, SERVER_FIXTURE.base)
  assert.equal(cache.resolveDelta(key, SERVER_FIXTURE.baseTag, 'W/"wrong"', SERVER_FIXTURE.delta), null)
  assert.deepEqual(cache.conditionalHeaders(key), {})
})

test('the same path under another backend identity never shares an entry', () => {
  const cache = new JsonEtagCache()
  const url = 'http://127.0.0.1:1/api/cron/jobs'
  cache.store(cache.key(url, 'tok-a'), 'W/"a"', '[1]')

  assert.deepEqual(cache.conditionalHeaders(cache.key(url, 'tok-b')), {})
  assert.equal(cache.revalidated(cache.key(url, 'tok-b')), null)
})

test('an untagged response drops a stale entry so it is not replayed later', () => {
  const cache = new JsonEtagCache()
  const key = cache.key('http://h/api/x', 't')
  cache.store(key, 'W/"old"', '"old"')
  cache.store(key, undefined, '"new"')

  assert.deepEqual(cache.conditionalHeaders(key), {})
  assert.equal(cache.revalidated(key), null)
})

test('the cache stays bounded, evicting the least recently used entry', () => {
  const cache = new JsonEtagCache()
  const first = cache.key('http://h/api/0', 't')
  cache.store(first, 'W/"0"', '0')

  for (let i = 1; i <= 300; i++) {
    cache.store(cache.key(`http://h/api/${i}`, 't'), `W/"${i}"`, String(i))
  }

  assert.ok(cache.size <= 256)
  assert.equal(cache.revalidated(first), null)
  assert.equal(cache.revalidated(cache.key('http://h/api/300', 't')), '300')
})
