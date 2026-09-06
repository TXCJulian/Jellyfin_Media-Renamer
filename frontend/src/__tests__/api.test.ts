import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockFetch = vi.fn()
const mockConnectSSE = vi.fn(() => vi.fn())

vi.stubGlobal('fetch', mockFetch)
vi.mock('@/lib/sse', () => ({
  connectSSE: mockConnectSSE,
}))

const {
  fetchJson,
  fetchDownloadDirectories,
  fetchPreviewStatus,
  postCookies,
  postForm,
  postRefresh,
} = await import('@/lib/api')

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  mockFetch.mockReset()
  mockConnectSSE.mockClear()
})

describe('fetchJson', () => {
  it('returns parsed JSON on success', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ directories: ['a', 'b'] }))
    const result = await fetchJson<{ directories: string[] }>('/test')
    expect(result.directories).toEqual(['a', 'b'])
  })

  it('appends query params', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({}))
    await fetchJson('/test', { key: 'value' })
    const calledUrl = mockFetch.mock.calls[0]![0] as URL
    expect(calledUrl.toString()).toContain('key=value')
  })

  it('throws on non-ok response with detail', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Invalid path' }), {
        status: 400,
        statusText: 'Bad Request',
      }),
    )
    await expect(fetchJson('/test')).rejects.toThrow('Invalid path')
  })

  it('throws a proxy hint when HTML is returned for a JSON request', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response('<!DOCTYPE html><html><body>app</body></html>', {
        status: 200,
        statusText: 'OK',
        headers: { 'Content-Type': 'text/html' },
      }),
    )

    await expect(fetchJson('/download/status')).rejects.toThrow(
      'Expected JSON from /download/status',
    )
  })
})

describe('postForm', () => {
  it('sends FormData and returns JSON', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ success: true }))
    const result = await postForm<{ success: boolean }>('/test', { key: 'val', num: 42 })
    expect(result.success).toBe(true)
    const [, options] = mockFetch.mock.calls[0]!
    expect(options.method).toBe('POST')
    expect(options.body).toBeInstanceOf(FormData)
  })
})

describe('directory APIs', () => {
  it('posts to refresh endpoint', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ status: 'ok' }))
    await postRefresh()
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })

  it('throws on postRefresh error', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response('', { status: 500, statusText: 'Internal Server Error' }),
    )
    await expect(postRefresh()).rejects.toThrow()
  })

  it('fetches unfiltered downloader destinations from the download endpoint', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ directories: [{ path: 'Empty', base: 'media' }] }),
    )

    const result = await fetchDownloadDirectories('emp')

    expect(result.directories).toEqual([{ path: 'Empty', base: 'media' }])
    const calledUrl = new URL(mockFetch.mock.calls[0]![0] as string)
    expect(calledUrl.pathname).toContain('/directories/download')
    expect(calledUrl.searchParams.get('search')).toBe('emp')
  })
})

describe('fetchPreviewStatus', () => {
  it('calls preview-status endpoint and returns payload', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        state: 'running',
        ready: false,
        percent: 12.5,
        eta_seconds: 22,
        elapsed_seconds: 3,
        message: 'Transcoding preview',
      }),
    )

    const result = await fetchPreviewStatus('abc123')
    expect(result.percent).toBe(12.5)

    const calledUrl = mockFetch.mock.calls[0]![0] as URL
    expect(calledUrl.toString()).toContain('/cutter/preview-status/abc123')
  })
})

describe('downloader APIs', () => {
  it('uploads cookies as multipart form data', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ status: 'ok' }))
    const file = new File(['cookie-data'], 'cookies.txt', { type: 'text/plain' })

    await postCookies(file)

    const [calledUrl, options] = mockFetch.mock.calls[0]!
    expect((calledUrl as URL).toString()).toContain('/download/cookies')
    expect(options.method).toBe('POST')
    expect(options.body).toBeInstanceOf(FormData)
  })
})
