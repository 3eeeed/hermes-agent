import assert from 'node:assert/strict'

import { test } from 'vitest'

import { JsonEtagCache } from './json-etag-cache'

test('a cached tagged body is offered for revalidation and replayed on 304', () => {
  const cache = new JsonEtagCache()
  const key = cache.key('http://127.0.0.1:1/api/sessions/x/messages?limit=120', 'tok-a')

  assert.deepEqual(cache.conditionalHeaders(key), {})
  cache.store(key, 'W/"abc"', '{"messages":[1]}')
  assert.deepEqual(cache.conditionalHeaders(key), { 'If-None-Match': 'W/"abc"' })
  assert.equal(cache.revalidated(key), '{"messages":[1]}')
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
