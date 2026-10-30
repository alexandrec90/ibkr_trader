import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchJson } from './api.ts'

afterEach(() => vi.unstubAllGlobals())

function answer(body: string, init: ResponseInit) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body, init)))
}

describe('fetchJson', () => {
  it('returns the parsed body', async () => {
    answer('{"a": 1}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    expect(await fetchJson('/api/x')).toEqual({ a: 1 })
  })

  it("raises the app's detail on an error", async () => {
    answer('{"detail": "start the db"}', {
      status: 503,
      statusText: 'Service Unavailable',
      headers: { 'Content-Type': 'application/json' },
    })
    await expect(fetchJson('/api/x')).rejects.toThrow('start the db')
  })

  it('falls back to the status line for a non-JSON error page', async () => {
    answer('<h1>Bad Gateway</h1>', {
      status: 502,
      statusText: 'Bad Gateway',
      headers: { 'Content-Type': 'text/html' },
    })
    await expect(fetchJson('/api/x')).rejects.toThrow('502 Bad Gateway')
  })
})
