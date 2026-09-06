import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import LyricsPanel from '@/components/LyricsPanel'

const api = vi.hoisted(() => ({
  fetchJson: vi.fn(),
  fetchTranscriberHealth: vi.fn(),
  fetchMusicFiles: vi.fn(),
  postRefresh: vi.fn(),
}))

vi.mock('@/hooks/useDirectoryAutoRefresh', () => ({
  useDirectoryAutoRefresh: vi.fn(),
}))

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, ...api }
})

beforeEach(() => {
  vi.clearAllMocks()
  api.fetchJson.mockResolvedValue({
    directories: [{ path: 'Music/Artist/Album', base: 'media3' }],
  })
  api.fetchMusicFiles.mockResolvedValue({
    files: [{ name: 'song.flac', has_lrc: false, has_txt: false }],
  })
  api.postRefresh.mockResolvedValue(undefined)
})

describe('LyricsPanel model availability', () => {
  it('blocks transcription and disables the active model when none of the offered models fit', async () => {
    api.fetchTranscriberHealth.mockResolvedValue({
      status: 'ok',
      vram_total_mb: 1024,
      whisper_model_fit: {
        small: false,
        medium: false,
        'large-v3-turbo': false,
        'large-v3': false,
      },
    })

    render(
      <LyricsPanel
        onLog={vi.fn()}
        onError={vi.fn()}
        onBack={vi.fn()}
        log={[]}
        error=""
        hasStarted={false}
      />,
    )

    await waitFor(() => expect(screen.getByText('1 / 1 selected')).toBeTruthy())
    expect(screen.getByText('No available Whisper model fits this GPU.')).toBeTruthy()

    const submit = screen.getByRole('button', { name: 'Transcribe' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: /Advanced Options/ }))
    const activeModel = screen.getByRole('radio', { name: 'Turbo' }) as HTMLButtonElement
    expect(activeModel.disabled).toBe(true)
    expect(activeModel.title).toContain("Doesn't fit in the available VRAM")
  })
})
