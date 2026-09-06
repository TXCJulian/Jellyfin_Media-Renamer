import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJson, fetchTranscriberHealth, fetchMusicFiles, postRefresh } from '@/lib/api'
import { connectSSE } from '@/lib/sse'
import { useDebounce } from '@/hooks/useDebounce'
import { useDirectoryAutoRefresh } from '@/hooks/useDirectoryAutoRefresh'
import { unfitModels, bestFittingModel } from '@/lib/modelFit'
import type {
  DirectoriesResponse,
  DirectoryEntry,
  LyricsForm,
  TranscriberHealth,
  MusicFileInfo,
} from '@/types'
import PanelLayout from '@/components/PanelLayout'
import LogPanel from '@/components/LogPanel'
import FormSection from '@/components/ui/FormSection'
import DirectorySelect from '@/components/ui/DirectorySelect'
import SegmentedControl from '@/components/ui/SegmentedControl'
import ToggleSwitch from '@/components/ui/ToggleSwitch'

interface LyricsPanelProps {
  onLog: (log: string[]) => void
  onError: (error: string) => void
  onBack: () => void
  log: string[]
  error: string
  hasStarted: boolean
  showBaseLabel?: boolean
}

// UI subset of the models the backend accepts, ordered fastest-to-most-accurate.
const WHISPER_MODELS = [
  { label: 'Small', value: 'small' },
  { label: 'Medium', value: 'medium' },
  { label: 'Turbo', value: 'large-v3-turbo' },
  { label: 'Large', value: 'large-v3' },
]

// UI subset of the vocal separation models the backend accepts, ordered
// fastest-to-most-accurate.
const DEMUCS_MODELS = [
  { label: 'htdemucs', value: 'htdemucs' },
  { label: 'htdemucs_ft', value: 'htdemucs_ft' },
  { label: 'mdx_extra_q', value: 'mdx_extra_q' },
]

function shortGpuName(gpu: string | null | undefined): string {
  if (!gpu) return ''
  return gpu.replace(/^NVIDIA GeForce /, '')
}

export default function LyricsPanel({
  onLog,
  onError,
  onBack,
  log,
  error,
  hasStarted,
  showBaseLabel,
}: LyricsPanelProps) {
  const [form, setForm] = useState<LyricsForm>({
    artist: '',
    album: '',
    directory: '',
    base: '',
    format: 'lrc',
    skip_existing: true,
    language: '',
    no_separation: false,
    no_correction: false,
    demucs_model: 'htdemucs',
    whisper_model: 'large-v3-turbo',
    artist_override: '',
    title_override: '',
  })
  const [directories, setDirectories] = useState<DirectoryEntry[]>([])
  const [isLoadingDirs, setIsLoadingDirs] = useState(false)
  const [isTranscribing, setIsTranscribing] = useState(false)
  const [health, setHealth] = useState<TranscriberHealth | null>(null)
  const [musicFiles, setMusicFiles] = useState<MusicFileInfo[]>([])
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set())
  const [isLoadingFiles, setIsLoadingFiles] = useState(false)
  const directoryRequest = useRef(0)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const abortSSERef = useRef<(() => void) | null>(null)

  const debouncedArtist = useDebounce(form.artist, 500)
  const debouncedAlbum = useDebounce(form.album, 500)

  const [isCheckingHealth, setIsCheckingHealth] = useState(false)

  const checkHealth = useCallback(() => {
    setIsCheckingHealth(true)
    setHealth(null)
    fetchTranscriberHealth()
      .then(setHealth)
      .catch(() => setHealth({ status: 'unreachable', error: 'Could not reach backend' }))
      .finally(() => setIsCheckingHealth(false))
  }, [])

  useEffect(() => {
    checkHealth()
  }, [checkHealth])

  // Auto-downgrade the selected Whisper model if it no longer fits in the
  // available VRAM (fresh health check, or a different GPU than before) --
  // there's no other field to fix this, so there's nothing to dim-and-leave
  // clickable like the codec/container pairs elsewhere in the app.
  useEffect(() => {
    const fixed = bestFittingModel(form.whisper_model, WHISPER_MODELS, health?.whisper_model_fit)
    if (fixed !== form.whisper_model) {
      setForm((prev) => ({ ...prev, whisper_model: fixed }))
    }
  }, [health, form.whisper_model])

  const fetchDirs = useCallback(
    async (artist: string, album: string, preserveSelection = false) => {
      const requestId = ++directoryRequest.current
      setIsLoadingDirs(true)
      onError('')
      try {
        const params: Record<string, string> = {}
        if (artist) params.artist = artist
        if (album) params.album = album
        const data = await fetchJson<DirectoriesResponse>('/directories/music', params)
        const dirs = data.directories ?? []
        if (requestId !== directoryRequest.current) return
        setDirectories(dirs)
        if (!preserveSelection) {
          setForm((prev) => {
            const stillPresent = dirs.some((d) => d.path === prev.directory && d.base === prev.base)
            return {
              ...prev,
              directory: dirs.length > 0 ? (stillPresent ? prev.directory : dirs[0]!.path) : '',
              base: dirs.length > 0 ? (stillPresent ? prev.base : dirs[0]!.base) : '',
            }
          })
        }
      } catch (err) {
        if (requestId === directoryRequest.current) {
          onError(`Error loading directories: ${err instanceof Error ? err.message : String(err)}`)
        }
      } finally {
        if (requestId === directoryRequest.current) setIsLoadingDirs(false)
      }
    },
    [onError],
  )

  useDirectoryAutoRefresh(() => fetchDirs(debouncedArtist, debouncedAlbum, true))

  useEffect(() => {
    void fetchDirs(debouncedArtist, debouncedAlbum)
  }, [debouncedArtist, debouncedAlbum, fetchDirs])

  const loadFiles = useCallback(
    (directory: string, base: string, selectAll: boolean, signal?: { cancelled: boolean }) => {
      setIsLoadingFiles(true)
      fetchMusicFiles(directory, base)
        .then((data) => {
          if (signal?.cancelled) return
          const files = data.files ?? []
          setMusicFiles(files)
          setSelectedFiles((prev) =>
            selectAll
              ? new Set(files.map((f) => f.name))
              : new Set([...prev].filter((n) => new Set(files.map((f) => f.name)).has(n))),
          )
        })
        .catch(() => {
          if (signal?.cancelled) return
          setMusicFiles([])
          setSelectedFiles(new Set())
        })
        .finally(() => {
          if (!signal?.cancelled) setIsLoadingFiles(false)
        })
    },
    [],
  )

  const refreshFiles = useCallback(() => {
    if (!form.directory) return
    loadFiles(form.directory, form.base, false)
  }, [form.directory, form.base, loadFiles])

  useEffect(() => {
    if (!form.directory) {
      setMusicFiles([])
      setSelectedFiles(new Set())
      return
    }
    const signal = { cancelled: false }
    loadFiles(form.directory, form.base, true, signal)
    return () => {
      signal.cancelled = true
    }
  }, [form.directory, form.base, loadFiles])

  const handleRefresh = async () => {
    setIsLoadingDirs(true)
    onError('')
    try {
      await postRefresh()
    } catch (err) {
      onError(`Error refreshing: ${err instanceof Error ? err.message : String(err)}`)
    }
    await fetchDirs(form.artist, form.album)
  }

  const toggleFile = (name: string) => {
    setSelectedFiles((prev) => {
      const next = new Set(prev)
      if (next.has(name)) {
        next.delete(name)
      } else {
        next.add(name)
      }
      return next
    })
  }

  const selectAll = () => setSelectedFiles(new Set(musicFiles.map((f) => f.name)))
  const deselectAll = () => setSelectedFiles(new Set())

  const isSingleSelection = selectedFiles.size === 1
  const selectedFileName = isSingleSelection ? [...selectedFiles][0] : null

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (selectedFiles.size === 0 || isTranscribing) return

    setIsTranscribing(true)
    onError('')
    onLog([])

    const logs: string[] = []

    const params: Record<string, string> = {
      directory: form.directory,
      base: form.base,
      output_format: form.format,
      skip_existing: String(form.skip_existing),
      no_separation: String(form.no_separation),
      no_correction: String(form.no_correction),
      demucs_model: form.demucs_model,
      whisper_model: form.whisper_model,
    }
    if (form.language) params.language = form.language

    // Overrides describe one specific song — the backend ignores them for
    // multi-file batches, so don't send them at all.
    if (isSingleSelection && !form.no_correction) {
      if (form.artist_override) params.artist_override = form.artist_override
      if (form.title_override) params.title_override = form.title_override
    }

    if (selectedFiles.size < musicFiles.length) {
      // JSON, not a delimited string — filenames may contain commas.
      params.files = JSON.stringify(Array.from(selectedFiles))
    }

    abortSSERef.current?.()
    abortSSERef.current = connectSSE('/transcribe/start', params, {
      onProgress: (data) => {
        if (data === 'heartbeat') return
        logs.push(data)
        onLog([...logs])
      },
      onError: (data) => {
        logs.push(`[ERROR]\t\t\t${data}`)
        onLog([...logs])
      },
      onDone: (data) => {
        logs.push(`\n${data}`)
        onLog([...logs])
        setIsTranscribing(false)
        abortSSERef.current = null
        refreshFiles()
      },
      onClose: () => {
        // Stream died without a `done` event — don't leave the button spinning.
        setIsTranscribing(false)
        abortSSERef.current = null
      },
    })
  }

  // Abort SSE stream on unmount
  useEffect(() => {
    return () => {
      abortSSERef.current?.()
    }
  }, [])

  const update = <K extends keyof LyricsForm>(key: K, value: LyricsForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const unfitWhisperModels = new Map(
    [...unfitModels(health?.whisper_model_fit)].map((name) => [
      name,
      `Doesn't fit in the available VRAM${health?.vram_total_mb ? ` (${(health.vram_total_mb / 1024).toFixed(1)} GB)` : ''}.`,
    ]),
  )
  const modelFit = health?.whisper_model_fit
  const noFittingWhisperModel =
    modelFit != null && WHISPER_MODELS.every((model) => modelFit[model.value] === false)
  const isServiceOk = health?.status === 'ok'
  const busy = isLoadingDirs || isTranscribing

  const healthPill = (
    <button
      type="button"
      onClick={checkHealth}
      disabled={isTranscribing || isCheckingHealth}
      title={isServiceOk ? 'Click to re-check connection' : 'Click to retry connection'}
      className="ml-auto inline-flex cursor-pointer items-center gap-[0.4rem] rounded-[20px] border border-[var(--border)] bg-[var(--bg-input)] px-[0.7rem] py-[0.3rem] text-[0.7rem] font-medium text-[var(--text-tertiary)] transition-all duration-200 hover:border-[var(--glass-border-hover)] hover:bg-[var(--bg-glass-hover)] hover:text-[var(--text-secondary)] disabled:cursor-not-allowed disabled:opacity-70"
    >
      <span
        className={`h-[7px] w-[7px] shrink-0 rounded-full transition-colors duration-200 ${
          health === null
            ? 'animate-pulse bg-yellow-500'
            : isServiceOk
              ? 'bg-[var(--success)] shadow-[0_0_8px_var(--success-glow)]'
              : 'bg-[var(--error)]'
        }`}
      />
      {isServiceOk && health?.gpu_name
        ? shortGpuName(health.gpu_name)
        : health === null
          ? 'Checking...'
          : 'Offline'}
    </button>
  )

  return (
    <PanelLayout title="Lyric Transcriber" onBack={onBack} rightElement={healthPill}>
      <form onSubmit={handleSubmit}>
        <FormSection label="Search">
          <div className="flex gap-3">
            <div className="mb-3 flex-1">
              <label className="field-label">Artist</label>
              <input
                type="text"
                value={form.artist}
                onChange={(e) => update('artist', e.target.value)}
                placeholder="Artist name"
                className="input-field input-rose"
              />
            </div>
            <div className="mb-3 flex-1">
              <label className="field-label">Album</label>
              <input
                type="text"
                value={form.album}
                onChange={(e) => update('album', e.target.value)}
                placeholder="Album name"
                className="input-field input-rose"
              />
            </div>
          </div>
        </FormSection>

        <FormSection label="Directory">
          <DirectorySelect
            directories={directories}
            value={form.directory}
            base={form.base}
            onChange={(val, base) => setForm((prev) => ({ ...prev, directory: val, base }))}
            onRefresh={() => void handleRefresh()}
            isLoading={isLoadingDirs}
            disabled={busy}
            color="rose"
            showBaseLabel={showBaseLabel}
          />
        </FormSection>

        {/* File List */}
        {form.directory && (
          <FormSection label="Songs">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[0.8rem] text-[var(--text-secondary)]">
                {selectedFiles.size} / {musicFiles.length} selected
              </span>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={refreshFiles}
                  disabled={isLoadingFiles}
                  title="Refresh file list"
                  className="flex h-[26px] w-[26px] cursor-pointer items-center justify-center rounded-[7px] border border-[var(--border)] bg-[var(--bg-input)] text-[0.8rem] text-[var(--text-secondary)] transition-all duration-200 hover:border-[var(--glass-border-hover)] hover:bg-[var(--bg-glass-hover)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isLoadingFiles ? <span className="spinner-sm" /> : '↻'}
                </button>
                <button
                  type="button"
                  onClick={selectAll}
                  disabled={busy}
                  className="cursor-pointer border-none bg-none font-[Geist,sans-serif] text-[0.68rem] font-semibold uppercase tracking-[0.06em] text-[var(--accent-3)] transition-opacity duration-200 hover:opacity-70"
                >
                  All
                </button>
                <button
                  type="button"
                  onClick={deselectAll}
                  disabled={busy}
                  className="cursor-pointer border-none bg-none font-[Geist,sans-serif] text-[0.68rem] font-semibold uppercase tracking-[0.06em] text-[var(--accent-3)] transition-opacity duration-200 hover:opacity-70"
                >
                  None
                </button>
              </div>
            </div>
            <div className="max-h-[210px] overflow-y-auto rounded-[10px] border border-[var(--border)] bg-[var(--bg-input)] p-[0.4rem]">
              {isLoadingFiles ? (
                <div className="py-4 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                  Loading files...
                </div>
              ) : musicFiles.length === 0 ? (
                <div className="py-4 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                  No music files found
                </div>
              ) : (
                musicFiles.map((file) => (
                  <label
                    key={file.name}
                    className="flex cursor-pointer items-center gap-[0.6rem] rounded-lg px-2 py-[0.4rem] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.025)]"
                  >
                    <input
                      type="checkbox"
                      checked={selectedFiles.has(file.name)}
                      onChange={() => toggleFile(file.name)}
                      disabled={busy}
                      className="shrink-0 accent-[var(--accent-3)]"
                    />
                    <span className="min-w-0 flex-1 truncate text-[0.8rem] text-[var(--text-primary)]">
                      {file.name}
                    </span>
                    <span className="flex shrink-0 gap-1">
                      {file.has_lrc && (
                        <span className="rounded-[5px] bg-[var(--success-glow)] px-[0.4rem] py-[0.15rem] text-[0.56rem] font-semibold uppercase tracking-[0.04em] text-[var(--success)]">
                          LRC
                        </span>
                      )}
                      {file.has_txt && (
                        <span className="rounded-[5px] bg-[rgba(96,165,250,0.15)] px-[0.4rem] py-[0.15rem] text-[0.56rem] font-semibold uppercase tracking-[0.04em] text-[#60a5fa]">
                          TXT
                        </span>
                      )}
                    </span>
                  </label>
                ))
              )}
            </div>
          </FormSection>
        )}

        <FormSection label="Options">
          <div className="mb-3">
            <label className="field-label">Format</label>
            <SegmentedControl
              options={[
                { label: 'LRC', value: 'lrc' },
                { label: 'TXT', value: 'txt' },
                { label: 'Both', value: 'all' },
              ]}
              value={form.format}
              onChange={(v) => update('format', v as LyricsForm['format'])}
              disabled={busy}
              color="rose"
            />
          </div>

          <div className="mt-2">
            <ToggleSwitch
              checked={form.skip_existing}
              onChange={(v) => update('skip_existing', v)}
              disabled={busy}
              color="rose"
              label="Skip Existing Lyrics"
            />
          </div>

          <div className="mt-[0.85rem]">
            <button
              type="button"
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="flex cursor-pointer items-center gap-2 border-none bg-none p-0 font-[Geist,sans-serif] text-[0.75rem] text-[var(--text-tertiary)] transition-colors duration-200 hover:text-[var(--text-secondary)]"
            >
              <span
                className={`text-[0.55rem] transition-transform duration-200 ${showAdvanced ? 'rotate-90' : ''}`}
              >
                ▶
              </span>
              Advanced Options
            </button>

            {showAdvanced && (
              <div className="mt-3 rounded-[10px] border border-[var(--border)] bg-[rgba(0,0,0,0.2)] p-4">
                <div className="mb-[0.65rem]">
                  <label className="field-label">Whisper Model</label>
                  <SegmentedControl
                    options={WHISPER_MODELS}
                    value={form.whisper_model}
                    onChange={(v) => update('whisper_model', v)}
                    disabled={busy}
                    unavailable={unfitWhisperModels}
                    color="rose"
                  />
                  <p className="mt-[0.35rem] text-[0.68rem] leading-snug text-[var(--text-tertiary)]">
                    Larger models are more accurate but slower. Turbo is the best all-round choice.
                  </p>
                </div>

                <div className="mb-[0.65rem]">
                  <label className="field-label">Vocal Separation Model</label>
                  <SegmentedControl
                    options={DEMUCS_MODELS}
                    value={form.demucs_model}
                    onChange={(v) => update('demucs_model', v)}
                    disabled={busy || form.no_separation}
                    color="rose"
                  />
                  <p className="mt-[0.35rem] text-[0.68rem] leading-snug text-[var(--text-tertiary)]">
                    Htdemucs_ft can give cleaner vocal isolation at similar VRAM cost, but runs
                    slower. Htdemucs is the best alround choice.
                  </p>
                </div>

                <div className="mb-[0.65rem]">
                  <label className="field-label">Language (empty = Auto)</label>
                  <input
                    type="text"
                    value={form.language}
                    onChange={(e) => update('language', e.target.value)}
                    placeholder="e.g. de, en, ja"
                    disabled={busy}
                    className="input-field input-rose !h-9 !text-[0.8rem]"
                  />
                </div>

                <div className="flex flex-col gap-[0.4rem]">
                  <ToggleSwitch
                    checked={form.no_separation}
                    onChange={(v) => update('no_separation', v)}
                    disabled={busy}
                    color="rose"
                    label="Skip Vocal Separation"
                  />
                  <ToggleSwitch
                    checked={form.no_correction}
                    onChange={(v) => update('no_correction', v)}
                    disabled={busy}
                    color="rose"
                    label="Skip Genius Correction"
                  />
                </div>

                {/* One artist/title pair can only describe one song, so these
                    overrides are offered for single-song runs only. */}
                {isSingleSelection && !form.no_correction && (
                  <div className="mt-[0.85rem] border-t border-[var(--border)] pt-[0.85rem]">
                    <label className="field-label">Genius Lookup Override</label>
                    <p className="mb-[0.5rem] text-[0.68rem] leading-snug text-[var(--text-tertiary)]">
                      Defaults to the tags in{' '}
                      <span className="text-[var(--text-secondary)]">{selectedFileName}</span>. Set
                      these if the tags are wrong or missing.
                    </p>
                    <div className="flex gap-3">
                      <div className="flex-1">
                        <input
                          type="text"
                          value={form.artist_override}
                          onChange={(e) => update('artist_override', e.target.value)}
                          placeholder="Artist"
                          disabled={busy}
                          className="input-field input-rose !h-9 !text-[0.8rem]"
                        />
                      </div>
                      <div className="flex-1">
                        <input
                          type="text"
                          value={form.title_override}
                          onChange={(e) => update('title_override', e.target.value)}
                          placeholder="Title"
                          disabled={busy}
                          className="input-field input-rose !h-9 !text-[0.8rem]"
                        />
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </FormSection>

        {noFittingWhisperModel && (
          <p className="mb-3 text-[0.75rem] text-[var(--error)]" role="alert">
            No available Whisper model fits this GPU.
          </p>
        )}

        <button
          type="submit"
          disabled={busy || !isServiceOk || selectedFiles.size === 0 || noFittingWhisperModel}
          className="btn-submit btn-rose"
        >
          {isTranscribing ? <span className="spinner-md" /> : 'Transcribe'}
        </button>

        <LogPanel
          log={log}
          error={error}
          hasStarted={hasStarted}
          color="rose"
          idleMessage="Ready for transcription..."
        />
      </form>
    </PanelLayout>
  )
}
