import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import EpisodePanel from '@/components/EpisodePanel'
import MusicPanel from '@/components/MusicPanel'
import LyricsPanel from '@/components/LyricsPanel'
import CutterPanel from '@/components/CutterPanel'
import DownloaderPanel from '@/components/DownloaderPanel'
import type { CutterPersistedState, CutterSourceState } from '@/types'

const testState = vi.hoisted(() => ({
  autoRefresh: undefined as undefined | (() => void | PromiseLike<unknown>),
}))

const api = vi.hoisted(() => ({
  fetchJson: vi.fn(),
  fetchTranscriberHealth: vi.fn(),
  fetchMusicFiles: vi.fn(),
  fetchCutterFiles: vi.fn(),
  fetchCutterStatus: vi.fn(),
  fetchDownloaderStatus: vi.fn(),
  fetchDownloadDirectories: vi.fn(),
  postRefresh: vi.fn(),
}))

vi.mock('@/hooks/useDirectoryAutoRefresh', () => ({
  useDirectoryAutoRefresh: (refresh: () => void | PromiseLike<unknown>) => {
    testState.autoRefresh = refresh
  },
}))

vi.mock('@/hooks/useDownloadStream', () => ({
  useDownloadStream: () => ({ jobs: [], connected: true }),
}))

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, ...api }
})

const selected = { path: 'Selected', base: 'media' }
const replacement = { path: 'Replacement', base: 'media' }

const commonProps = {
  onLog: vi.fn(),
  onError: vi.fn(),
  onBack: vi.fn(),
  log: [],
  error: '',
  hasStarted: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  testState.autoRefresh = undefined
  api.fetchTranscriberHealth.mockResolvedValue({ status: 'ok' })
  api.fetchMusicFiles.mockResolvedValue({ files: [] })
  api.fetchCutterFiles.mockResolvedValue({ files: [] })
  api.fetchCutterStatus.mockResolvedValue({
    ffmpeg_available: true,
    ffmpeg_version: 'test',
    ffmpeg_build: 'standard',
    ffmpeg_path: '/usr/bin/ffmpeg',
  })
  api.fetchDownloaderStatus.mockResolvedValue({
    yt_dlp_version: 'test',
    cookies_present: false,
    downloads_dir: '/downloads',
    queue_depth: 0,
    workers: 1,
  })
  api.postRefresh.mockResolvedValue(undefined)
})

async function runCapturedAutoRefresh() {
  expect(testState.autoRefresh).toBeTypeOf('function')
  await act(async () => {
    await testState.autoRefresh!()
  })
}

async function expectInitialSelection() {
  await waitFor(() => expect(screen.getByRole('button', { name: selected.path })).toBeTruthy())
}

describe('directory panel automatic refresh', () => {
  it('does not replace the Episode selection when refreshed results omit it', async () => {
    api.fetchJson.mockResolvedValueOnce({ directories: [selected] })
    render(<EpisodePanel {...commonProps} />)
    await expectInitialSelection()

    api.fetchJson.mockResolvedValueOnce({ directories: [replacement] })
    await runCapturedAutoRefresh()

    expect(screen.getByRole('button', { name: selected.path })).toBeTruthy()
  })

  it('does not replace the Music selection when refreshed results omit it', async () => {
    api.fetchJson.mockResolvedValueOnce({ directories: [selected] })
    render(<MusicPanel {...commonProps} />)
    await expectInitialSelection()

    api.fetchJson.mockResolvedValueOnce({ directories: [replacement] })
    await runCapturedAutoRefresh()

    expect(screen.getByRole('button', { name: selected.path })).toBeTruthy()
  })

  it('does not replace the Lyrics selection when refreshed results omit it', async () => {
    api.fetchJson.mockResolvedValueOnce({ directories: [selected] })
    render(<LyricsPanel {...commonProps} />)
    await expectInitialSelection()

    api.fetchJson.mockResolvedValueOnce({ directories: [replacement] })
    await runCapturedAutoRefresh()

    expect(screen.getByRole('button', { name: selected.path })).toBeTruthy()
  })

  it('does not replace the Cutter selection when refreshed results omit it', async () => {
    const emptySource: CutterSourceState = {
      probe: null,
      peaks: [],
      filePath: '',
      fileId: '',
      thumbnailUrl: '',
      files: [],
      jobId: '',
      outputFiles: [],
      isLoadingFile: false,
    }
    const initial: CutterPersistedState = {
      form: {
        source: 'server',
        directory: selected.path,
        base: selected.base,
        filename: '',
        inPoint: 0,
        outPoint: 0,
        outputName: '',
        streamCopy: true,
        codec: 'libx264',
        container: 'mp4',
        audioTracks: [],
        keepQuality: false,
      },
      directories: [selected],
      search: '',
      serverState: emptySource,
      uploadState: { ...emptySource },
    }

    function Harness() {
      const [persisted, setPersisted] = useState(initial)
      return <CutterPanel {...commonProps} persisted={persisted} onPersistedChange={setPersisted} />
    }

    render(<Harness />)
    await expectInitialSelection()
    api.fetchJson.mockResolvedValueOnce({ directories: [replacement] })

    await runCapturedAutoRefresh()

    expect(screen.getByRole('button', { name: selected.path })).toBeTruthy()
  })

  it('uses debounced search without replacing the Downloader destination', async () => {
    localStorage.setItem(
      'downloader-settings',
      JSON.stringify({ output_dir: selected.path, base: selected.base }),
    )
    api.fetchDownloadDirectories.mockResolvedValueOnce({ directories: [selected] })
    render(<DownloaderPanel onError={vi.fn()} onBack={vi.fn()} error="" />)
    await expectInitialSelection()
    fireEvent.change(screen.getByPlaceholderText('Filter directories...'), {
      target: { value: 'raw-search' },
    })

    api.fetchDownloadDirectories.mockResolvedValueOnce({ directories: [replacement] })
    await runCapturedAutoRefresh()

    expect(api.fetchDownloadDirectories).toHaveBeenLastCalledWith('')
    expect(screen.getByRole('button', { name: selected.path })).toBeTruthy()
  })
})
