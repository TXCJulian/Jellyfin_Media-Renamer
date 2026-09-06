import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { describe, expect, it, vi } from 'vitest'
import DownloaderPanel, { parseUrls } from '@/components/DownloaderPanel'
import * as api from '@/lib/api'

vi.mock('@/hooks/useDownloadStream', () => ({
  useDownloadStream: () => ({ jobs: [], connected: true }),
}))

vi.mock('@/lib/api', () => ({
  cancelDownloadJob: vi.fn(),
  createDownloads: vi.fn(),
  deleteCookies: vi.fn(),
  deleteDownloadJob: vi.fn(),
  fetchDownloaderStatus: vi.fn().mockResolvedValue({
    yt_dlp_version: 'test',
    cookies_present: false,
    downloads_dir: '/downloads',
    queue_depth: 0,
    workers: 1,
  }),
  fetchDownloadDirectories: vi.fn().mockResolvedValue({ directories: [] }),
  postCookies: vi.fn(),
  postRefresh: vi.fn().mockResolvedValue(undefined),
  startDownloadJob: vi.fn(),
}))

describe('parseUrls', () => {
  it('returns a single url unchanged', () => {
    expect(parseUrls('https://example.com/a')).toEqual(['https://example.com/a'])
  })

  it('splits one url per line and drops blanks', () => {
    expect(parseUrls('https://a\n\n  https://b  \n')).toEqual(['https://a', 'https://b'])
  })

  it('returns an empty array for empty input', () => {
    expect(parseUrls('   \n  ')).toEqual([])
  })
})

it('clears the backend cache before manually reloading downloader directories', async () => {
  const order: string[] = []
  vi.mocked(api.postRefresh).mockImplementation(async () => {
    order.push('clear')
  })
  vi.mocked(api.fetchDownloadDirectories).mockImplementation(async () => {
    order.push('fetch')
    return { directories: [] }
  })
  render(createElement(DownloaderPanel, { onError: vi.fn(), onBack: vi.fn(), error: '' }))
  await waitFor(() => expect(api.fetchDownloadDirectories).toHaveBeenCalledTimes(1))
  order.length = 0

  fireEvent.click(screen.getByTitle('Refresh'))

  await waitFor(() => expect(order).toEqual(['clear', 'fetch']))
})

it('reports a manual refresh failure without fetching or leaving the control loading', async () => {
  const onError = vi.fn()
  vi.clearAllMocks()
  vi.mocked(api.postRefresh).mockRejectedValueOnce(new Error('cache clear failed'))
  vi.mocked(api.fetchDownloadDirectories).mockResolvedValue({ directories: [] })

  render(createElement(DownloaderPanel, { onError, onBack: vi.fn(), error: '' }))
  await waitFor(() => expect(api.fetchDownloadDirectories).toHaveBeenCalledTimes(1))

  fireEvent.click(screen.getByTitle('Refresh'))

  await waitFor(() => expect(onError).toHaveBeenCalledWith('cache clear failed'))
  expect(api.fetchDownloadDirectories).toHaveBeenCalledTimes(1)
  expect((screen.getByTitle('Refresh') as HTMLButtonElement).disabled).toBe(false)
})
