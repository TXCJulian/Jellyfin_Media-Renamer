import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import PanelLayout from './PanelLayout'
import DownloadJobCard from './downloader/DownloadJobCard'
import DownloadOptions from './downloader/DownloadOptions'
import FormSection from './ui/FormSection'
import { useDebounce } from '@/hooks/useDebounce'
import { useDirectoryAutoRefresh } from '@/hooks/useDirectoryAutoRefresh'
import { useDownloadStream } from '@/hooks/useDownloadStream'
import {
  cancelDownloadJob,
  createDownloads,
  deleteCookies,
  deleteDownloadJob,
  fetchDownloaderStatus,
  fetchDownloadDirectories,
  postCookies,
  postRefresh,
  startDownloadJob,
} from '@/lib/api'
import type { DirectoryEntry, DownloadForm, DownloaderStatus } from '@/types'

const STORAGE_KEY = 'downloader-settings'

const DEFAULT_FORM: Omit<DownloadForm, 'url'> = {
  type: 'video',
  codec: 'auto',
  format: 'auto',
  quality: 'best',
  output_dir: '',
  base: '',
  auto_start: true,
  sub_folder: '',
  custom_prefix: '',
  custom_filename: '',
  item_limit: 0,
}

/**
 * Fields that describe a durable preference and are worth remembering across
 * page loads. Everything else describes a single, one-off download (a
 * subfolder, a custom filename, a playlist limit) and must start empty every
 * time — see PERSISTED_KEYS usage below for the explicit split.
 */
const PERSISTED_KEYS = [
  'type',
  'format',
  'quality',
  'codec',
  'base',
  'output_dir',
  'auto_start',
] as const satisfies readonly (keyof Omit<DownloadForm, 'url'>)[]

type PersistedSettings = Pick<DownloadForm, (typeof PERSISTED_KEYS)[number]>

function loadSettings(): Omit<DownloadForm, 'url'> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_FORM
    const parsed = JSON.parse(raw) as Partial<Record<string, unknown>>
    const persisted: Partial<PersistedSettings> = {}
    for (const key of PERSISTED_KEYS) {
      if (key in parsed) (persisted as Record<string, unknown>)[key] = parsed[key]
    }
    return { ...DEFAULT_FORM, ...persisted }
  } catch {
    return DEFAULT_FORM
  }
}

/** Split pasted input into URLs — one per line, blanks ignored. */
export function parseUrls(input: string): string[] {
  return input
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

interface DownloaderPanelProps {
  onError: (err: string) => void
  onBack: () => void
  error: string
  showBaseLabel?: boolean
}

export default function DownloaderPanel({
  onError,
  onBack,
  error,
  showBaseLabel,
}: DownloaderPanelProps) {
  const { jobs, connected } = useDownloadStream()
  const [status, setStatus] = useState<DownloaderStatus | null>(null)
  const [directories, setDirectories] = useState<DirectoryEntry[]>([])
  const [isRefreshingDirs, setIsRefreshingDirs] = useState(false)
  const [localError, setLocalError] = useState('')
  const [form, setForm] = useState<DownloadForm>(() => ({ url: '', ...loadSettings() }))
  const [search, setSearch] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const directoryRequest = useRef(0)

  const urls = useMemo(() => parseUrls(form.url), [form.url])
  const debouncedSearch = useDebounce(search, 500)

  useEffect(() => {
    const id = window.setTimeout(() => {
      const settings: PersistedSettings = {
        type: form.type,
        format: form.format,
        quality: form.quality,
        codec: form.codec,
        base: form.base,
        output_dir: form.output_dir,
        auto_start: form.auto_start,
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
    }, 300)
    return () => window.clearTimeout(id)
  }, [
    form.type,
    form.format,
    form.quality,
    form.codec,
    form.base,
    form.output_dir,
    form.auto_start,
  ])

  const refreshStatus = useCallback(async () => {
    setStatus(await fetchDownloaderStatus())
  }, [])

  /**
   * Unlike the other panels' directory pickers, an empty output_dir/base is a
   * meaningful selection here — "use the server's default downloads folder" —
   * and the user can deliberately clear it via DirectorySelect. So a fetch
   * triggered by opening the panel or clearing the search must never
   * overwrite that. Auto-selecting the first match is only applied while the
   * user is actively filtering (non-empty search text); on initial load or
   * once the search is cleared, the current selection — including "none" —
   * is left alone.
   */
  const refreshDirectories = useCallback(
    async (searchText?: string, clearCache = false, preserveSelection = false) => {
      const requestId = ++directoryRequest.current
      setIsRefreshingDirs(true)
      try {
        if (clearCache) await postRefresh()
        const dirs = (await fetchDownloadDirectories(searchText)).directories
        if (requestId !== directoryRequest.current) return
        setDirectories(dirs)
        if (searchText && !preserveSelection) {
          setForm((prev) => {
            const stillPresent = dirs.some(
              (d) => d.path === prev.output_dir && d.base === prev.base,
            )
            if (stillPresent) return prev
            return {
              ...prev,
              output_dir: dirs.length > 0 ? dirs[0]!.path : '',
              base: dirs.length > 0 ? dirs[0]!.base : '',
            }
          })
        }
      } finally {
        if (requestId === directoryRequest.current) setIsRefreshingDirs(false)
      }
    },
    [],
  )

  useDirectoryAutoRefresh(() => refreshDirectories(debouncedSearch, false, true))

  useEffect(() => {
    void refreshStatus().catch(() => {})
    void refreshDirectories().catch(() => {})
  }, [refreshDirectories, refreshStatus])

  // Re-fetch directories when the debounced search changes, but not on mount.
  const prevSearch = useRef(debouncedSearch)
  useEffect(() => {
    if (prevSearch.current !== debouncedSearch) {
      prevSearch.current = debouncedSearch
      void refreshDirectories(debouncedSearch).catch(() => {})
    }
  }, [debouncedSearch, refreshDirectories])

  const patchForm = useCallback((patch: Partial<DownloadForm>) => {
    setForm((prev) => ({ ...prev, ...patch }))
  }, [])

  const submit = async (override?: string[]) => {
    const targets = override ?? urls
    if (targets.length === 0) {
      setLocalError('Please enter at least one URL')
      return
    }
    setLocalError('')
    onError('')
    const { url: _ignored, ...options } = form
    try {
      await createDownloads(targets, options)
      if (!override) setForm((prev) => ({ ...prev, url: '' }))
      await refreshStatus()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Failed to create download')
    }
  }

  const guard = (action: Promise<unknown>, message: string) =>
    action.catch((err) => onError(err instanceof Error ? err.message : message))

  const active = jobs.filter((j) => ['queued', 'downloading', 'transcoding'].includes(j.stage))
  const history = jobs.filter((j) => ['done', 'error', 'cancelled'].includes(j.stage))

  return (
    <PanelLayout title="Downloader" onBack={onBack} maxWidth="920px">
      <div>
        <FormSection label="Add downloads">
          <div className="space-y-3">
            <textarea
              value={form.url}
              placeholder="One link per line"
              rows={urls.length > 1 ? Math.min(urls.length + 1, 8) : 2}
              onChange={(e) => patchForm({ url: e.target.value })}
              className="input-field input-cyan min-h-[76px] resize-y leading-relaxed"
            />
            <p className="text-[0.72rem] text-[var(--text-tertiary)]">
              Paste or enter one link per line, then select Download.
            </p>
            <button type="button" onClick={() => void submit()} className="btn-submit btn-cyan">
              {urls.length > 1 ? `Download ${urls.length} links` : 'Download'}
            </button>
          </div>
        </FormSection>

        {(localError || error) && (
          <div
            className="mb-6 flex items-center justify-between rounded-lg border border-red-500/15 bg-red-500/[0.06] px-4 py-2.5"
            role="alert"
          >
            <p className="text-[0.8rem] text-red-400">{localError || error}</p>
            <button
              type="button"
              className="ml-3 shrink-0 text-[0.7rem] text-red-400/50"
              onClick={() => {
                setLocalError('')
                onError('')
              }}
              aria-label="Dismiss error"
            >
              dismiss
            </button>
          </div>
        )}

        <DownloadOptions
          form={form}
          onChange={patchForm}
          directories={directories}
          onRefreshDirectories={() =>
            void refreshDirectories(search, true).catch((err) =>
              onError(err instanceof Error ? err.message : 'Failed to refresh directories'),
            )
          }
          isRefreshingDirectories={isRefreshingDirs}
          showBaseLabel={showBaseLabel}
          search={search}
          onSearchChange={setSearch}
        />

        <FormSection label="Cookies">
          <div className="flex flex-wrap items-center gap-3">
            <label className="cursor-pointer rounded-lg border border-[var(--glass-border)] bg-[var(--bg-glass)] px-3 py-1.5 text-[0.8rem] text-[var(--text-secondary)] transition-all hover:border-[var(--glass-border-hover)] hover:text-[var(--text-primary)]">
              Upload cookies.txt
              <input
                type="file"
                className="hidden"
                accept=".txt"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) {
                    void guard(postCookies(file).then(refreshStatus), 'Failed to upload cookies')
                  }
                  e.target.value = ''
                }}
              />
            </label>
            {status?.cookies_present && (
              <button
                type="button"
                onClick={() =>
                  void guard(deleteCookies().then(refreshStatus), 'Failed to delete cookies')
                }
                className="rounded-lg border border-red-500/20 px-3 py-1.5 text-[0.8rem] text-red-400 transition-all hover:bg-red-500/10"
              >
                Remove
              </button>
            )}
            <span className="text-[0.75rem] text-[var(--text-tertiary)]">
              {status?.cookies_present ? 'cookies.txt loaded' : 'No cookies configured'}
            </span>
          </div>
        </FormSection>

        <FormSection label={active.length > 0 ? `Active — ${active.length}` : 'Active'}>
          <div className="space-y-3">
            {active.length > 0 ? (
              active.map((job) => (
                <DownloadJobCard
                  key={job.job_id}
                  job={job}
                  onCancel={(id) => void guard(cancelDownloadJob(id), 'Failed to cancel')}
                  onDelete={(id) => void guard(deleteDownloadJob(id), 'Failed to delete')}
                  onStart={(id) => void guard(startDownloadJob(id), 'Failed to start')}
                  onRetry={(url) => void submit([url])}
                />
              ))
            ) : (
              <p className="rounded-[14px] border border-white/6 bg-white/[0.02] py-5 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                Nothing downloading right now.
              </p>
            )}
          </div>
        </FormSection>

        <div className="mb-6">
          <button
            type="button"
            onClick={() => setHistoryOpen((o) => !o)}
            className={`flex w-full cursor-pointer items-center gap-2 border border-[var(--glass-border)] bg-[var(--glass-bg)] px-4 py-2.5 text-left text-[0.8rem] font-medium text-white/70 backdrop-blur-sm transition hover:border-cyan-400/30 hover:text-white/90 ${historyOpen ? 'rounded-t-xl' : 'rounded-xl'}`}
          >
            <span className={`inline-block transition-transform ${historyOpen ? 'rotate-90' : ''}`}>
              &#9654;
            </span>
            History {history.length > 0 && `(${history.length})`}
          </button>

          {historyOpen && (
            <div className="space-y-3 rounded-b-xl border border-t-0 border-[var(--glass-border)] bg-black/20 p-4">
              {history.length > 0 ? (
                history.map((job) => (
                  <DownloadJobCard
                    key={job.job_id}
                    job={job}
                    onCancel={(id) => void guard(cancelDownloadJob(id), 'Failed to cancel')}
                    onDelete={(id) => void guard(deleteDownloadJob(id), 'Failed to delete')}
                    onStart={(id) => void guard(startDownloadJob(id), 'Failed to start')}
                    onRetry={(url) => void submit([url])}
                  />
                ))
              ) : (
                <p className="rounded-[14px] border border-white/6 bg-white/[0.02] py-5 text-center text-[0.82rem] text-[var(--text-tertiary)]">
                  Finished downloads will be listed here.
                </p>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center justify-center">
          <div className="inline-flex items-center gap-3 rounded-full border border-white/6 bg-white/[0.03] px-5 py-2 text-[0.72rem] text-[var(--text-tertiary)]">
            <span>yt-dlp {status?.yt_dlp_version ?? '...'}</span>
            <span className="text-white/10">·</span>
            <span>{connected ? 'Live' : 'Reconnecting...'}</span>
            <span className="text-white/10">·</span>
            <span>Queue: {status?.queue_depth ?? 0}</span>
          </div>
        </div>
      </div>
    </PanelLayout>
  )
}
