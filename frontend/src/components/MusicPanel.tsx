import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJson, postForm, postRefresh } from '@/lib/api'
import { useDebounce } from '@/hooks/useDebounce'
import { useDirectoryAutoRefresh } from '@/hooks/useDirectoryAutoRefresh'
import type { DirectoriesResponse, DirectoryEntry, MusicForm, RenameResponse } from '@/types'
import PanelLayout from '@/components/PanelLayout'
import LogPanel from '@/components/LogPanel'
import FormSection from '@/components/ui/FormSection'
import DirectorySelect from '@/components/ui/DirectorySelect'
import SegmentedControl from '@/components/ui/SegmentedControl'
import ToggleSwitch from '@/components/ui/ToggleSwitch'

interface MusicPanelProps {
  onLog: (log: string[]) => void
  onError: (error: string) => void
  onBack: () => void
  log: string[]
  error: string
  hasStarted: boolean
  showBaseLabel?: boolean
}

export default function MusicPanel({
  onLog,
  onError,
  onBack,
  log,
  error,
  hasStarted,
  showBaseLabel,
}: MusicPanelProps) {
  const [form, setForm] = useState<MusicForm>({
    artist: '',
    album: '',
    directory: '',
    base: '',
    dry_run: true,
    lyrics_action: 'rename',
  })
  const [directories, setDirectories] = useState<DirectoryEntry[]>([])
  const [isLoadingDirs, setIsLoadingDirs] = useState(false)
  const [isRenaming, setIsRenaming] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const directoryRequest = useRef(0)

  const debouncedArtist = useDebounce(form.artist, 500)
  const debouncedAlbum = useDebounce(form.album, 500)

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

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setIsRenaming(true)
    onError('')
    onLog([])

    try {
      const data = await postForm<RenameResponse>('/rename/music', {
        directory: form.directory,
        base: form.base,
        dry_run: form.dry_run,
        lyrics_action: form.lyrics_action,
      })
      if (data.error) onError(data.error)
      onLog(data.log ?? [])
      if (data.directories) setDirectories(data.directories)
    } catch (err) {
      onError(`Error renaming: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setIsRenaming(false)
    }
  }

  const update = <K extends keyof MusicForm>(key: K, value: MusicForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const busy = isLoadingDirs || isRenaming

  return (
    <PanelLayout title="Music Renamer" onBack={onBack}>
      <form onSubmit={(e) => void handleSubmit(e)}>
        <FormSection label="Search">
          <div className="flex gap-3">
            <div className="mb-3 flex-1">
              <label className="field-label">Artist</label>
              <input
                type="text"
                value={form.artist}
                onChange={(e) => update('artist', e.target.value)}
                placeholder="Artist name"
                className="input-field input-indigo"
              />
            </div>
            <div className="mb-3 flex-1">
              <label className="field-label">Album</label>
              <input
                type="text"
                value={form.album}
                onChange={(e) => update('album', e.target.value)}
                placeholder="Album name"
                className="input-field input-indigo"
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
            color="indigo"
            showBaseLabel={showBaseLabel}
          />
        </FormSection>

        <FormSection label="Options">
          <ToggleSwitch
            checked={form.dry_run}
            onChange={(v) => update('dry_run', v)}
            disabled={busy}
            color="indigo"
            label="Dry Run"
          />

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
                <label className="field-label">Lyrics Files (.lrc / .txt)</label>
                <SegmentedControl
                  options={[
                    { label: 'Rename', value: 'rename' },
                    { label: 'Delete', value: 'delete' },
                  ]}
                  value={form.lyrics_action}
                  onChange={(v) => update('lyrics_action', v as MusicForm['lyrics_action'])}
                  disabled={busy}
                  color="indigo"
                />
                <p className="mt-[0.35rem] text-[0.68rem] leading-snug text-[var(--text-tertiary)]">
                  {form.lyrics_action === 'rename'
                    ? 'Transcribed lyrics follow the song to its new name.'
                    : 'Transcribed lyrics are deleted and must be transcribed again.'}
                </p>
              </div>
            )}
          </div>
        </FormSection>

        <button type="submit" disabled={busy} className="btn-submit btn-indigo">
          {isRenaming ? <span className="spinner-md" /> : 'Rename'}
        </button>

        <LogPanel
          log={log}
          error={error}
          hasStarted={hasStarted}
          color="indigo"
          idleMessage="Ready for renaming..."
        />
      </form>
    </PanelLayout>
  )
}
