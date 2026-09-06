import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJson, postForm, postRefresh } from '@/lib/api'
import { useDebounce } from '@/hooks/useDebounce'
import { useDirectoryAutoRefresh } from '@/hooks/useDirectoryAutoRefresh'
import type { DirectoriesResponse, DirectoryEntry, EpisodeForm, RenameResponse } from '@/types'
import PanelLayout from '@/components/PanelLayout'
import LogPanel from '@/components/LogPanel'
import FormSection from '@/components/ui/FormSection'
import DirectorySelect from '@/components/ui/DirectorySelect'
import SegmentedControl from '@/components/ui/SegmentedControl'
import ToggleSwitch from '@/components/ui/ToggleSwitch'

interface EpisodePanelProps {
  onLog: (log: string[]) => void
  onError: (error: string) => void
  onBack: () => void
  log: string[]
  error: string
  hasStarted: boolean
  showBaseLabel?: boolean
}

export default function EpisodePanel({
  onLog,
  onError,
  onBack,
  log,
  error,
  hasStarted,
  showBaseLabel,
}: EpisodePanelProps) {
  const [form, setForm] = useState<EpisodeForm>({
    series: '',
    season: 1,
    directory: '',
    base: '',
    dry_run: true,
    assign_seq: false,
    threshold: 0.75,
    lang: 'de',
  })
  const [directories, setDirectories] = useState<DirectoryEntry[]>([])
  const [isLoadingDirs, setIsLoadingDirs] = useState(false)
  const [isRenaming, setIsRenaming] = useState(false)
  const [touched, setTouched] = useState(false)
  const directoryRequest = useRef(0)

  const debouncedSeries = useDebounce(form.series, 500)
  const debouncedSeason = useDebounce(form.season, 500)

  const fetchDirs = useCallback(
    async (series: string, season: number, preserveSelection = false) => {
      const requestId = ++directoryRequest.current
      setIsLoadingDirs(true)
      onError('')
      try {
        const params: Record<string, string> = {}
        if (series) params.series = series
        if (season) params.season = String(season)
        const data = await fetchJson<DirectoriesResponse>('/directories/tvshows', params)
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

  useDirectoryAutoRefresh(() => fetchDirs(debouncedSeries, debouncedSeason, true))

  useEffect(() => {
    void fetchDirs(debouncedSeries, debouncedSeason)
  }, [debouncedSeries, debouncedSeason, fetchDirs])

  const handleRefresh = async () => {
    setIsLoadingDirs(true)
    onError('')
    try {
      await postRefresh()
    } catch (err) {
      onError(`Error refreshing: ${err instanceof Error ? err.message : String(err)}`)
    }
    await fetchDirs(form.series, form.season)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!form.series.trim() || !form.season) return
    setIsRenaming(true)
    onError('')
    onLog([])

    try {
      const data = await postForm<RenameResponse>('/rename/episodes', {
        series: form.series,
        season: form.season,
        directory: form.directory,
        base: form.base,
        dry_run: form.dry_run,
        assign_seq: form.assign_seq,
        threshold: form.threshold,
        lang: form.lang,
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

  const update = <K extends keyof EpisodeForm>(key: K, value: EpisodeForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const busy = isLoadingDirs || isRenaming
  const showSeriesError = touched && !form.series.trim()
  const showSeasonError = touched && !form.season

  return (
    <PanelLayout title="Episode Renamer" onBack={onBack}>
      <form noValidate onSubmit={(e) => void handleSubmit(e)}>
        <FormSection label="Search">
          <div className="flex gap-3">
            <div className="mb-3 flex-1">
              <label className="field-label">Series</label>
              <input
                type="text"
                value={form.series}
                onChange={(e) => update('series', e.target.value)}
                placeholder="Series name"
                className={`input-field input-blue ${showSeriesError ? 'input-error' : ''}`}
              />
              {showSeriesError && (
                <span className="mt-[0.3rem] block text-[0.7rem] text-[var(--error)]">
                  Series name is required
                </span>
              )}
            </div>
            <div className="mb-3 max-w-[110px]">
              <label className="field-label">Season</label>
              <input
                type="number"
                value={form.season}
                onChange={(e) => update('season', Number(e.target.value))}
                min={1}
                className={`input-field input-blue ${showSeasonError ? 'input-error' : ''}`}
              />
              {showSeasonError && (
                <span className="mt-[0.3rem] block text-[0.7rem] text-[var(--error)]">
                  Required
                </span>
              )}
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
            color="blue"
            showBaseLabel={showBaseLabel}
          />
        </FormSection>

        <FormSection label="Options">
          <div className="mb-3">
            <label className="field-label">Language</label>
            <SegmentedControl
              options={[
                { label: 'DE', value: 'de' },
                { label: 'EN', value: 'en' },
              ]}
              value={form.lang}
              onChange={(v) => update('lang', v)}
              disabled={busy}
              color="blue"
            />
          </div>

          <div className="mt-[0.35rem] flex flex-col gap-2">
            <ToggleSwitch
              checked={form.dry_run}
              onChange={(v) => update('dry_run', v)}
              disabled={busy}
              color="blue"
              label="Dry Run"
            />
            <ToggleSwitch
              checked={form.assign_seq}
              onChange={(v) => update('assign_seq', v)}
              disabled={busy}
              color="blue"
              label="Assign Sequence"
            />
          </div>

          <div className="mt-[0.85rem]">
            <label className="field-label">Match Threshold</label>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={form.threshold}
                onChange={(e) => update('threshold', Number(e.target.value))}
                className="h-1 flex-1 cursor-pointer appearance-none rounded-sm bg-[rgba(255,255,255,0.07)] outline-none [&::-webkit-slider-thumb]:h-[18px] [&::-webkit-slider-thumb]:w-[18px] [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-[var(--accent)] [&::-webkit-slider-thumb]:shadow-[0_0_14px_var(--accent-glow-strong)] [&::-webkit-slider-thumb]:transition-transform [&::-webkit-slider-thumb]:duration-150 hover:[&::-webkit-slider-thumb]:scale-115"
              />
              <span className="min-w-[2.5rem] text-right font-[JetBrains_Mono,monospace] text-[0.8rem] text-[var(--accent-light)]">
                {form.threshold}
              </span>
            </div>
          </div>
        </FormSection>

        <button type="submit" disabled={busy} className="btn-submit btn-blue">
          {isRenaming ? <span className="spinner-md" /> : 'Rename'}
        </button>

        <LogPanel
          log={log}
          error={error}
          hasStarted={hasStarted}
          color="blue"
          idleMessage="Ready for renaming..."
        />
      </form>
    </PanelLayout>
  )
}
