/**
 * The proxy sends a fresh install to /setup by asking the API whether setup is
 * needed. That call runs on the server, so it has to use the address of the API
 * from inside the web container (API_INTERNAL_URL). NEXT_PUBLIC_API_URL is the
 * browser's address and is the relative path `/api` in the production image,
 * which a server-side fetch cannot resolve; the check then failed quietly and
 * the redirect never happened.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { NextRequest } from 'next/server'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('proxy setup redirect', () => {
  it('asks the internal API address and redirects to /setup when setup is needed', async () => {
    vi.stubEnv('API_INTERNAL_URL', 'http://api:8000')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ needs_setup: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.resetModules()
    const { proxy } = await import('../../proxy')

    const res = await proxy(new NextRequest('http://localhost:3000/projects'))

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('http://api:8000/setup/status')
    expect(res.status).toBe(307)
    expect(new URL(res.headers.get('location')!).pathname).toBe('/setup')
  })
})
