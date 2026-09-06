import { useCallback, useEffect, useRef, useState } from 'react'
import PanelLayout from '@/components/PanelLayout'
import LogPanel from '@/components/LogPanel'
import MediaPlayer from '@/components/cutter/MediaPlayer'
import TrimControls from '@/components/cutter/TrimControls'
import OutputSettings from '@/components/cutter/OutputSettings'
import AudioTrackSelect from '@/components/cutter/AudioTrackSelect'
import JobManager from '@/components/cutter/JobManager'
import SegmentedControl from '@/components/ui/SegmentedControl'
import FormSection from '@/components/ui/FormSection'
import { connectSSE } from '@/lib/sse'
import DirectorySelect from '@/components/ui/DirectorySelect'
import {
  fetchJson,
  uploadFile,
  fetchProbe,
  fetchWaveform,
  fetchCutterFiles,
  getStreamUrl,
  getAudioStreamUrl,
  getAudioOnlyTranscodeUrl,
  fetchPreviewStatus,
  getThumbnailUrl,
  getDownloadUrl,
  createJob,
  postRefresh,
  saveToSource,
  fetchCutterStatus,
} from '@/lib/api'
import {
  getBrowserCompatibilityMessage,
  getBrowserCompatibilityReport,
} from '@/lib/mediaCompatibility'
import { useDebounce } from '@/hooks/useDebounce'
import { useDirectoryAutoRefresh } from '@/hooks/useDirectoryAutoRefresh'
import type {
  CutterForm,
  CutterFileInfo,
  CutterJob,
  CutterPersistedState,
  CutterSourceState,
  CutterPreviewStatus,
  CutterStatus,
  DirectoriesResponse,
  AudioTrackConfig,
  ProbeResult,
} from '@/types'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

const SOURCE_CODEC_TO_ENCODER: Record<string, string> = {
  h264: 'libx264',
  hevc: 'libx265',
  h265: 'libx265',
  vp9: 'libvpx-vp9',
  av1: 'libsvtav1',
  mpeg2video: 'libx264',
}

const EXT_TO_CONTAINER: Record<string, string> = {
  '.mp4': 'mp4',
  '.mkv': 'mkv',
  '.webm': 'webm',
  '.mov': 'mov',
  '.mka': 'mka',
  '.flac': 'flac',
  '.ogg': 'ogg',
  '.mp3': 'mp3',
  '.m4a': 'mp4',
}

interface CutterPanelProps {
  onLog: (log: string[]) => void
  onError: (error: string) => void
  onBack: () => void
  log: string[]
  error: string
  hasStarted: boolean
  persisted: CutterPersistedState
  onPersistedChange: (state: CutterPersistedState) => void
  showBaseLabel?: boolean
}

export default function CutterPanel({
  onLog,
  onError,
  onBack,
  log,
  error,
  hasStarted,
  persisted,
  onPersistedChange,
  showBaseLabel,
}: CutterPanelProps) {
  // Shared state
  const { form, directories, search } = persisted

  // Active source state - derives from whichever source is selected
  const sourceKey = form.source === 'server' ? 'serverState' : 'uploadState'
  const { probe, peaks, filePath, fileId, thumbnailUrl, files, jobId, outputFiles, isLoadingFile } =
    persisted[sourceKey]

  // Keep a ref to the latest persisted state so setPersisted never goes stale
  const persistedRef = useRef(persisted)
  persistedRef.current = persisted

  const setPersisted = useCallback(
    (
      updater:
        | Partial<CutterPersistedState>
        | ((prev: CutterPersistedState) => Partial<CutterPersistedState>),
    ) => {
      const current = persistedRef.current
      const partial = typeof updater === 'function' ? updater(current) : updater
      const next = { ...current, ...partial }
      persistedRef.current = next
      onPersistedChange(next)
    },
    [onPersistedChange],
  )

  // Helper: update fields on the currently active source state
  const setSource = useCallback(
    (
      updater:
        Partial<CutterSourceState> | ((prev: CutterSourceState) => Partial<CutterSourceState>),
    ) => {
      const current = persistedRef.current
      const key = current.form.source === 'server' ? 'serverState' : 'uploadState'
      const prev = current[key]
      const partial = typeof updater === 'function' ? updater(prev) : updater
      const next = { ...current, [key]: { ...prev, ...partial } }
      persistedRef.current = next
      onPersistedChange(next)
    },
    [onPersistedChange],
  )

  const setForm = useCallback(
    (updater: CutterForm | ((prev: CutterForm) => CutterForm)) => {
      const currentForm = persistedRef.current.form
      const next = typeof updater === 'function' ? updater(currentForm) : updater
      setPersisted({ form: next })
    },
    [setPersisted],
  )

  // Transient state - resets on navigation (that's fine)
  const [isLoadingDirs, setIsLoadingDirs] = useState(false)
  const directoryRequest = useRef(0)
  const [isLoadingFiles, setIsLoadingFiles] = useState(false)
  const [isCutting, setIsCutting] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(-1)
  const [isDragOver, setIsDragOver] = useState(false)
  const [previewStatus, setPreviewStatus] = useState<CutterPreviewStatus | null>(null)
  const [transcodeMode, setTranscodeMode] = useState<'off' | 'audio_only' | 'full'>('off')
  const [previewAudioStreamIndex, setPreviewAudioStreamIndex] = useState<number | null>(null)
  const [status, setStatus] = useState<CutterStatus | null>(null)
  // The ffmpeg build is fixed for the server's lifetime, so this is fetched
  // once. A failure is not worth an error banner — the footer just stays quiet.
  const [statusFailed, setStatusFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetchCutterStatus()
      .then((data) => {
        if (!cancelled) setStatus(data)
      })
      .catch(() => {
        if (!cancelled) setStatusFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const debouncedSearch = useDebounce(search, 500)
  const abortSSERef = useRef<(() => void) | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const update = <K extends keyof CutterForm>(key: K, value: CutterForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const buildProbeSelectionPatch = useCallback((path: string, duration: number, probeData: any) => {
    const ext = path.substring(path.lastIndexOf('.')).toLowerCase()
    const sourceVideoCodec = probeData.video_codec?.toLowerCase() ?? ''
    return {
      inPoint: 0,
      outPoint: duration,
      codec: SOURCE_CODEC_TO_ENCODER[sourceVideoCodec] ?? 'libx264',
      container: EXT_TO_CONTAINER[ext] ?? 'mp4',
      audioTracks: (probeData.audio_streams ?? []).map((stream: { index: number }) => ({
        streamIndex: stream.index,
        mode: 'passthru' as const,
        codec: 'aac',
      })),
      keepQuality: false,
    }
  }, [])

  // ── Fetch directories with optional search filter ──────────
  const fetchDirs = useCallback(
    async (searchText: string, preserveSelection = false) => {
      const requestId = ++directoryRequest.current
      setIsLoadingDirs(true)
      onError('')
      try {
        const params: Record<string, string> = {}
        if (searchText) params.search = searchText
        const data = await fetchJson<DirectoriesResponse>('/directories/media', params)
        const dirs = data.directories ?? []
        if (requestId !== directoryRequest.current) return
        setPersisted((prev) => {
          if (preserveSelection) return { directories: dirs }
          const stillPresent = dirs.some(
            (d) => d.path === prev.form.directory && d.base === prev.form.base,
          )
          return {
            directories: dirs,
            form: {
              ...prev.form,
              directory:
                dirs.length > 0 ? (stillPresent ? prev.form.directory : dirs[0]!.path) : '',
              base: dirs.length > 0 ? (stillPresent ? prev.form.base : dirs[0]!.base) : '',
            },
          }
        })
      } catch (err) {
        if (requestId === directoryRequest.current) {
          onError(`Error loading directories: ${err instanceof Error ? err.message : String(err)}`)
        }
      } finally {
        if (requestId === directoryRequest.current) setIsLoadingDirs(false)
      }
    },
    [onError, setPersisted],
  )

  useDirectoryAutoRefresh(() => fetchDirs(debouncedSearch, true))

  // Only fetch on mount if directories are empty (first visit)
  const initialFetchDone = useRef(directories.length > 0)
  useEffect(() => {
    if (!initialFetchDone.current) {
      initialFetchDone.current = true
      void fetchDirs(debouncedSearch)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Re-fetch when search changes (but not on mount)
  const prevSearch = useRef(debouncedSearch)
  useEffect(() => {
    if (prevSearch.current !== debouncedSearch) {
      prevSearch.current = debouncedSearch
      void fetchDirs(debouncedSearch)
    }
  }, [debouncedSearch, fetchDirs])

  const handleRefresh = async () => {
    setIsLoadingDirs(true)
    onError('')
    try {
      await postRefresh()
    } catch (err) {
      onError(`Error refreshing: ${err instanceof Error ? err.message : String(err)}`)
    }
    await fetchDirs(search)
  }

  // ── Fetch files when directory changes ────────────────────────
  const prevDir = useRef(form.directory)
  useEffect(() => {
    if (form.source !== 'server') return
    // Skip if directory hasn't changed (e.g., on remount with persisted state)
    if (prevDir.current === form.directory && files.length > 0) return
    prevDir.current = form.directory

    if (!form.directory) {
      setSource({ files: [] })
      return
    }
    const signal = { cancelled: false }
    setIsLoadingFiles(true)
    fetchCutterFiles(form.directory, form.base)
      .then((data) => {
        if (signal.cancelled) return
        setSource({ files: data.files ?? [] })
      })
      .catch((err) => {
        if (signal.cancelled) return
        onError(`Error loading files: ${err instanceof Error ? err.message : String(err)}`)
        setSource({ files: [] })
      })
      .finally(() => {
        if (!signal.cancelled) setIsLoadingFiles(false)
      })
    return () => {
      signal.cancelled = true
    }
  }, [form.directory, form.source, setSource]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Load probe + waveform for a file ──────────────────────────
  const loadFileData = useCallback(
    async (
      path: string,
      source: 'server' | 'upload',
      jid = '',
      base = '',
    ): Promise<ProbeResult | null> => {
      onError('')
      setSource({ probe: null, peaks: [], thumbnailUrl: '', isLoadingFile: true })
      try {
        const probeData = await fetchProbe(path, source, jid, base)
        const isVideo = probeData.video_codec != null
        // Skip waveform for videos - they use the thumbnail strip instead.
        const peaks = isVideo ? [] : (await fetchWaveform(path, source, 800, jid, base)).peaks
        setSource({
          probe: probeData,
          peaks,
          isLoadingFile: false,
          thumbnailUrl: isVideo ? getThumbnailUrl(path, source, jid, 30, base) : '',
        })
        const probePatch = buildProbeSelectionPatch(path, probeData.duration, probeData)
        setPersisted((prev) => ({
          form: { ...prev.form, ...probePatch },
        }))
        setPreviewStatus(null)
        setTranscodeMode('off')
        setPreviewAudioStreamIndex(null)
        return probeData
      } catch (err) {
        onError(`Error loading file: ${err instanceof Error ? err.message : String(err)}`)
        setSource({ isLoadingFile: false })
        setPreviewStatus(null)
        setTranscodeMode('off')
        setPreviewAudioStreamIndex(null)
        return null
      }
    },
    [onError, setSource, setPersisted, buildProbeSelectionPatch],
  )

  // ── Server file selection ─────────────────────────────────────
  const handleFileSelect = useCallback(
    async (file: CutterFileInfo) => {
      const path = `${form.directory}/${file.name}`
      setSource({ isLoadingFile: true, probe: null, peaks: [], thumbnailUrl: '', outputFiles: [] })
      try {
        const { job_id, file_id } = await createJob(path, 'server', form.base)
        setSource({
          filePath: path,
          fileId: file_id,
          jobId: job_id,
        })
        setPersisted((prev) => ({
          form: { ...prev.form, filename: file.name },
        }))
        await loadFileData(path, 'server', job_id, form.base)
      } catch (err) {
        onError(`Error creating job: ${err instanceof Error ? err.message : String(err)}`)
        setSource({ isLoadingFile: false })
      }
    },
    [form.directory, form.base, loadFileData, setSource, setPersisted, onError],
  )

  // ── Directory selection from DirectorySelect ─────────────────
  const handleDirectoryChange = (dir: string, base: string) => {
    setPersisted({ form: { ...form, directory: dir, base: base || form.base, filename: '' } })
    setSource({
      probe: null,
      peaks: [],
      filePath: '',
      fileId: '',
      thumbnailUrl: '',
      jobId: '',
      outputFiles: [],
    })
    setPreviewStatus(null)
    setTranscodeMode('off')
    setPreviewAudioStreamIndex(null)
    // Reset prevDir so the files effect fires
    prevDir.current = ''
  }

  // ── Reopen job from job manager ───────────────────────────────
  const handleOpenJob = useCallback(
    async (job: CutterJob) => {
      const hasFullTranscodedPreview = !!job.preview_transcoded
      const hasAudioOnlyTranscode = !!job.audio_transcoded_tracks?.length
      const reopenTranscodeMode: 'off' | 'audio_only' | 'full' = hasFullTranscodedPreview
        ? 'full'
        : hasAudioOnlyTranscode
          ? 'audio_only'
          : 'off'
      const reopenAudioStreamIndex = hasAudioOnlyTranscode
        ? (job.audio_transcoded_tracks?.[0] ?? null)
        : null

      const source: CutterForm['source'] = job.source
      const jobBase = source === 'server' ? (job.base ?? '') : ''
      const filePath = source === 'server' ? job.original_path : job.original_name
      const directory = job.original_path
        ? job.original_path.substring(0, job.original_path.lastIndexOf('/'))
        : ''
      const settings = job.cut_settings ?? null
      const sourceStatePatch = {
        filePath,
        fileId: job.source_file_id ?? '',
        jobId: job.job_id,
        outputFiles: job.output_files,
        probe: null,
        peaks: [] as number[],
        thumbnailUrl: '',
        isLoadingFile: true,
      }
      setPersisted((prev) => ({
        form: {
          ...prev.form,
          source,
          directory: source === 'server' ? directory : prev.form.directory,
          base: source === 'server' ? jobBase : prev.form.base,
          filename: job.original_name,
          inPoint: settings?.in_point ?? 0,
          outPoint: settings?.out_point ?? 0,
          streamCopy: settings?.stream_copy ?? true,
          codec: settings?.codec ?? 'libx264',
          container: settings?.container ?? 'mp4',
          outputName: settings?.output_name ?? '',
          audioTracks: (settings?.audio_tracks ?? []).map(
            (track: { index: number; mode: string; codec: string | null }) => ({
              streamIndex: track.index,
              mode: track.mode as AudioTrackConfig['mode'],
              codec: track.codec ?? 'aac',
            }),
          ),
          keepQuality: settings?.keep_quality ?? false,
        },
        serverState:
          source === 'server' ? { ...prev.serverState, ...sourceStatePatch } : prev.serverState,
        uploadState:
          source === 'upload' ? { ...prev.uploadState, ...sourceStatePatch } : prev.uploadState,
      }))
      setPreviewStatus(null)
      setTranscodeMode('off')
      setPreviewAudioStreamIndex(null)
      const probeResult = await loadFileData(filePath, source, job.job_id, jobBase)
      if (!probeResult) return
      if (reopenTranscodeMode === 'audio_only') {
        setPreviewAudioStreamIndex(reopenAudioStreamIndex)
      }
      setTranscodeMode(reopenTranscodeMode)
      // Restore saved in/out points - loadFileData resets them to 0/duration
      if (settings) {
        setPersisted((prev) => ({
          form: { ...prev.form, inPoint: settings.in_point, outPoint: settings.out_point },
        }))
      }
    },
    [loadFileData, setPersisted, onError],
  )

  // ── Upload handling ───────────────────────────────────────────
  const handleUpload = useCallback(
    async (file: File) => {
      setUploadProgress(0)
      onError('')
      setSource({
        probe: null,
        peaks: [],
        filePath: '',
        fileId: '',
        thumbnailUrl: '',
        isLoadingFile: true,
        jobId: '',
        outputFiles: [],
      })
      try {
        const result = await uploadFile(file, setUploadProgress)
        // Network upload is complete at this point; switch UI out of upload-progress mode.
        setUploadProgress(-1)

        let probeData: Awaited<ReturnType<typeof fetchProbe>> | null = null
        let lastProbeError: unknown = null
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            probeData = await fetchProbe(result.filename, 'upload', result.job_id)
            break
          } catch (err) {
            lastProbeError = err
            if (attempt < 2) {
              await sleep(200 * (attempt + 1))
            }
          }
        }

        if (!probeData) {
          throw new Error(
            `Upload completed, but loading file metadata failed: ${lastProbeError instanceof Error ? lastProbeError.message : String(lastProbeError)}`,
          )
        }

        const targetJobId = result.job_id

        setSource({
          probe: probeData,
          filePath: result.filename,
          fileId: result.file_id,
          jobId: result.job_id,
          peaks: [],
          thumbnailUrl:
            probeData.video_codec != null
              ? getThumbnailUrl(result.filename, 'upload', result.job_id)
              : '',
          isLoadingFile: false,
        })
        setPersisted((prev) => ({
          form: {
            ...prev.form,
            filename: result.filename,
            ...buildProbeSelectionPatch(result.filename, probeData.duration, probeData),
          },
        }))
        setPreviewStatus(null)
        setTranscodeMode('off')
        setPreviewAudioStreamIndex(null)

        // Generate waveform lazily after UI is ready so uploads feel instant.
        void fetchWaveform(result.filename, 'upload', 800, result.job_id)
          .then((waveData) => {
            setSource((prev) => (prev.jobId === targetJobId ? { peaks: waveData.peaks } : {}))
          })
          .catch(() => {
            // Keep upload successful even if waveform extraction fails.
          })
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        onError(message.startsWith('Upload completed,') ? message : `Upload failed: ${message}`)
        setSource({ isLoadingFile: false })
      } finally {
        setUploadProgress(-1)
      }
    },
    [onError, setSource, setPersisted, buildProbeSelectionPatch],
  )

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      setIsDragOver(false)
      const file = e.dataTransfer.files[0]
      if (file) void handleUpload(file)
    },
    [handleUpload],
  )

  const handleFileInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      if (file) void handleUpload(file)
      e.target.value = ''
    },
    [handleUpload],
  )

  // ── Cut execution ─────────────────────────────────────────────
  const handleCut = (e: React.FormEvent) => {
    e.preventDefault()
    if (!filePath || !probe || isCutting || !jobId) return
    if (form.inPoint >= form.outPoint) {
      onError('In-point must be before out-point')
      return
    }

    setIsCutting(true)
    onError('')
    onLog([])
    setSource({ outputFiles: [] })

    const logs: string[] = []

    const params: Record<string, string> = {
      path: filePath,
      source: form.source,
      job_id: jobId,
      in_point: String(form.inPoint),
      out_point: String(form.outPoint),
      stream_copy: String(form.streamCopy),
    }
    if (form.source === 'server' && form.base) params.base = form.base
    if (form.outputName) params.output_name = form.outputName
    if (!form.streamCopy && form.codec && isVideo) {
      params.codec = form.codec
    }
    params.container = form.container
    params.keep_quality = String(form.keepQuality)
    params.audio_tracks = JSON.stringify(
      form.audioTracks.map((track) => ({
        index: track.streamIndex,
        mode: track.mode,
        codec: track.mode === 'reencode' ? track.codec : null,
      })),
    )

    abortSSERef.current?.()
    abortSSERef.current = connectSSE('/cutter/cut', params, {
      onProgress: (data) => {
        if (data === 'heartbeat') return
        logs.push(data)
        onLog([...logs])
      },
      onError: (data) => {
        logs.push(`[ERROR]\t\t\t${data}`)
        onLog([...logs])
        setIsCutting(false)
      },
      onDone: (data) => {
        logs.push(`\n${data}`)
        onLog([...logs])
        setIsCutting(false)
        abortSSERef.current = null

        // Parse output filename from "Output: filename.ext"
        const match = data.match(/^Output:\s*(.+)$/)
        if (match) {
          setSource((prev) => ({
            outputFiles: [...prev.outputFiles, match[1]!],
          }))
        }
      },
    })
  }

  // Abort SSE on unmount
  useEffect(() => {
    return () => {
      abortSSERef.current?.()
    }
  }, [])

  // Source tab switch - just update the form, state is preserved per-source
  const handleSourceChange = (source: string) => {
    setPersisted({ form: { ...form, source: source as CutterForm['source'] } })
    setUploadProgress(-1)
  }

  const locked = isCutting || isLoadingFile || uploadProgress >= 0
  const busy = isLoadingDirs || locked
  const isVideo = probe?.video_codec != null
  const hasFile = !!probe && !!filePath
  const defaultAudioStreamIndex = probe?.audio_streams?.[0]?.index ?? null
  const selectedPreviewAudioStreamIndex = (() => {
    if (!probe?.audio_streams?.length) return null
    if (
      previewAudioStreamIndex != null &&
      probe.audio_streams.some((stream) => stream.index === previewAudioStreamIndex)
    ) {
      return previewAudioStreamIndex
    }
    return defaultAudioStreamIndex
  })()
  const streamAudioIndex = (() => {
    if (selectedPreviewAudioStreamIndex == null) return null
    if (transcodeMode === 'off' && selectedPreviewAudioStreamIndex === defaultAudioStreamIndex) {
      return null
    }
    return selectedPreviewAudioStreamIndex
  })()

  const compatibilityReport = hasFile ? getBrowserCompatibilityReport(filePath, probe) : null
  const showAudioOnlyButton = isVideo && compatibilityReport && !compatibilityReport.videoIssue
  const compatibilityMessage = (() => {
    if (!hasFile || !probe) return null

    if (compatibilityReport?.hasIssues) {
      return getBrowserCompatibilityMessage(compatibilityReport)
    }

    // Backend compatibility rules are authoritative for whether original
    // playback may fail. Show a clear warning even if frontend heuristics
    // did not classify this file as problematic.
    if (probe.needs_transcoding && transcodeMode === 'off') {
      return 'This file is likely not browser-compatible in original playback mode. Use "Transcode Audio Only" or "Full Transcode" for reliable playback.'
    }

    return null
  })()

  useEffect(() => {
    if (!hasFile || !probe?.needs_transcoding || !fileId || transcodeMode === 'off') {
      setPreviewStatus(null)
      return
    }

    let cancelled = false
    let timeoutId: ReturnType<typeof setTimeout> | null = null
    let lastState: CutterPreviewStatus['state'] = 'running'

    const poll = async () => {
      try {
        const status = await fetchPreviewStatus(
          fileId,
          transcodeMode === 'audio_only' && selectedPreviewAudioStreamIndex != null
            ? { audioTranscodeStream: selectedPreviewAudioStreamIndex }
            : undefined,
        )
        if (cancelled) return
        setPreviewStatus(status)
        lastState = status.state
        if (status.state === 'error' && status.message) {
          onError(status.message)
        }
      } catch (err) {
        if (cancelled) return
        setPreviewStatus(
          (prev) =>
            prev ?? {
              state: 'error',
              ready: false,
              percent: 0,
              eta_seconds: null,
              elapsed_seconds: 0,
              message: err instanceof Error ? err.message : String(err),
            },
        )
      } finally {
        if (!cancelled && lastState !== 'done' && lastState !== 'error') {
          timeoutId = setTimeout(poll, 1200)
        }
      }
    }

    void poll()

    return () => {
      cancelled = true
      if (timeoutId) clearTimeout(timeoutId)
    }
  }, [
    hasFile,
    probe?.needs_transcoding,
    fileId,
    transcodeMode,
    selectedPreviewAudioStreamIndex,
    onError,
  ])

  return (
    <PanelLayout title="Media Cutter" onBack={onBack} maxWidth="1100px">
      <form onSubmit={handleCut}>
        {/* Source selector */}
        <FormSection label="Source">
          <SegmentedControl
            options={[
              { label: 'Server', value: 'server' },
              { label: 'Upload', value: 'upload' },
            ]}
            value={form.source}
            onChange={handleSourceChange}
            disabled={locked}
            color="emerald"
          />
        </FormSection>

        {/* ── Server tab ────────────────────────────────────── */}
        {form.source === 'server' && (
          <>
            <FormSection label="Search">
              <input
                type="text"
                value={search}
                onChange={(e) => setPersisted({ search: e.target.value })}
                placeholder="Filter directories..."
                className="input-field input-emerald"
                disabled={locked}
              />
            </FormSection>

            <FormSection label="Directory">
              <DirectorySelect
                directories={directories}
                value={form.directory}
                base={form.base}
                onChange={handleDirectoryChange}
                onRefresh={() => void handleRefresh()}
                isLoading={isLoadingDirs}
                disabled={locked}
                color="emerald"
                showBaseLabel={showBaseLabel}
              />
            </FormSection>

            {/* File list */}
            {form.directory && (
              <FormSection label="Files">
                <div className="max-h-[260px] overflow-y-auto rounded-[10px] border border-[var(--border)] bg-[var(--bg-input)] p-[0.4rem]">
                  {isLoadingFiles ? (
                    <div className="py-4 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                      Loading files...
                    </div>
                  ) : files.length === 0 ? (
                    <div className="py-4 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                      No media files found
                    </div>
                  ) : (
                    files.map((file) => {
                      const isSelected = form.filename === file.name
                      return (
                        <button
                          key={file.name}
                          type="button"
                          onClick={() => handleFileSelect(file)}
                          disabled={locked}
                          className={`flex w-full cursor-pointer items-center gap-[0.6rem] rounded-lg border-none bg-transparent px-3 py-[0.5rem] text-left font-[Geist,sans-serif] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.025)] ${
                            isSelected
                              ? 'bg-[rgba(52,211,153,0.08)] text-[var(--accent-4)]'
                              : 'text-[var(--text-primary)]'
                          } ${locked ? 'cursor-not-allowed opacity-50' : ''}`}
                        >
                          <span className="min-w-0 flex-1 truncate text-[0.8rem]">{file.name}</span>
                          <span className="shrink-0 text-[0.68rem] text-[var(--text-tertiary)]">
                            {formatFileSize(file.size)}
                          </span>
                          <span className="shrink-0 rounded-[5px] bg-[rgba(52,211,153,0.1)] px-[0.4rem] py-[0.12rem] text-[0.56rem] font-semibold uppercase tracking-[0.04em] text-[var(--accent-4)]">
                            {file.extension}
                          </span>
                        </button>
                      )
                    })
                  )}
                </div>
              </FormSection>
            )}
          </>
        )}

        {/* ── Upload tab ────────────────────────────────────── */}
        {form.source === 'upload' && (
          <FormSection label="Upload File">
            {uploadProgress >= 0 ? (
              <div className="rounded-[10px] border border-[var(--border)] bg-[var(--bg-input)] p-6">
                <div className="mb-2 text-center text-[0.8rem] text-[var(--text-secondary)]">
                  Uploading... {uploadProgress}%
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-[rgba(255,255,255,0.05)]">
                  <div
                    className="h-full rounded-full bg-[var(--accent-4)] transition-all duration-300"
                    style={{ width: `${uploadProgress}%` }}
                  />
                </div>
              </div>
            ) : (
              <div
                onDragOver={(e) => {
                  e.preventDefault()
                  setIsDragOver(true)
                }}
                onDragLeave={() => setIsDragOver(false)}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`flex cursor-pointer flex-col items-center justify-center gap-3 rounded-[10px] border-2 border-dashed py-12 transition-all duration-200 ${
                  isDragOver
                    ? 'border-[var(--accent-4)] bg-[rgba(52,211,153,0.06)]'
                    : 'border-[var(--border)] bg-[var(--bg-input)] hover:border-[var(--glass-border-hover)] hover:bg-[rgba(255,255,255,0.015)]'
                }`}
              >
                <span className="text-[1.5rem] text-[var(--text-tertiary)]">
                  {isDragOver ? '\u2B07' : '\uD83D\uDCC1'}
                </span>
                <span className="text-[0.85rem] text-[var(--text-secondary)]">
                  {isDragOver ? 'Drop file here' : 'Drag & drop a file, or click to browse'}
                </span>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="audio/*,video/*"
                  onChange={handleFileInputChange}
                  className="hidden"
                />
              </div>
            )}
            {/* Show selected filename after upload */}
            {form.filename && probe && uploadProgress < 0 && (
              <div className="mt-3 flex items-center gap-2 rounded-lg bg-[rgba(52,211,153,0.08)] px-3 py-2">
                <span className="text-[0.8rem] text-[var(--accent-4)]">{form.filename}</span>
              </div>
            )}
          </FormSection>
        )}

        {/* ── Loading skeleton ─────────────────────────────── */}
        {isLoadingFile && !probe && (
          <FormSection label="Preview">
            <div className="flex items-center justify-center gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-input)] py-16">
              <span className="spinner-md" />
              <span className="text-[0.85rem] text-[var(--text-tertiary)]">Loading file...</span>
            </div>
          </FormSection>
        )}

        {/* ── Player section (shown after file is loaded) ─── */}
        {hasFile && (
          <>
            {compatibilityMessage && (
              <div className="mb-3 rounded-xl border border-amber-400/30 bg-amber-400/8 px-4 py-3 text-[0.78rem] text-amber-200">
                <p>{compatibilityMessage}</p>
                {probe?.needs_transcoding && (
                  <div className="mt-2 flex items-center gap-2">
                    {transcodeMode === 'off' ? (
                      <>
                        {showAudioOnlyButton && (
                          <button
                            type="button"
                            onClick={() => setTranscodeMode('audio_only')}
                            className="rounded-md border border-amber-300/40 bg-amber-300/12 px-2.5 py-1 text-[0.72rem] font-semibold text-amber-100 transition hover:bg-amber-300/18"
                          >
                            Transcode Audio Only
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => setTranscodeMode('full')}
                          className="rounded-md border border-amber-300/40 bg-amber-300/12 px-2.5 py-1 text-[0.72rem] font-semibold text-amber-100 transition hover:bg-amber-300/18"
                        >
                          Full Transcode
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setTranscodeMode('off')}
                        className="rounded-md border border-white/20 bg-white/8 px-2.5 py-1 text-[0.72rem] font-semibold text-white/80 transition hover:bg-white/12"
                      >
                        Use Original Playback
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            <FormSection label="Preview">
              <MediaPlayer
                streamUrl={
                  transcodeMode === 'full' && isVideo
                    ? getStreamUrl(fileId, null, true)
                    : getStreamUrl(
                        fileId,
                        transcodeMode === 'off' ? streamAudioIndex : null,
                        transcodeMode === 'full',
                      )
                }
                audioUrl={
                  transcodeMode === 'audio_only' && selectedPreviewAudioStreamIndex != null
                    ? getAudioOnlyTranscodeUrl(fileId, selectedPreviewAudioStreamIndex)
                    : isVideo &&
                        transcodeMode === 'full' &&
                        selectedPreviewAudioStreamIndex != null &&
                        selectedPreviewAudioStreamIndex !== defaultAudioStreamIndex
                      ? getAudioStreamUrl(fileId, selectedPreviewAudioStreamIndex, true)
                      : undefined
                }
                isVideo={isVideo}
                peaks={peaks}
                duration={probe.duration}
                sourceAspectRatio={probe.display_aspect_ratio}
                videoWidth={probe.width}
                videoHeight={probe.height}
                inPoint={form.inPoint}
                outPoint={form.outPoint}
                onInPointChange={(t) => update('inPoint', t)}
                onOutPointChange={(t) => update('outPoint', t)}
                thumbnailUrl={thumbnailUrl || undefined}
                needsTranscoding={probe.needs_transcoding && transcodeMode !== 'off'}
                transcodePercent={previewStatus?.percent}
                transcodeEtaSeconds={previewStatus?.eta_seconds ?? null}
                transcodeState={previewStatus?.state}
                transcodeMessage={previewStatus?.message}
              />
            </FormSection>

            {probe.audio_streams &&
              probe.audio_streams.length > 1 &&
              selectedPreviewAudioStreamIndex != null && (
                <FormSection label="Preview Audio Track">
                  <AudioTrackSelect
                    streams={probe.audio_streams}
                    value={selectedPreviewAudioStreamIndex}
                    onChange={setPreviewAudioStreamIndex}
                    disabled={locked}
                  />
                </FormSection>
              )}

            <FormSection label="Trim">
              <TrimControls
                inPoint={form.inPoint}
                outPoint={form.outPoint}
                duration={probe.duration}
                onInPointChange={(t) => update('inPoint', t)}
                onOutPointChange={(t) => update('outPoint', t)}
              />
            </FormSection>

            <OutputSettings
              outputName={form.outputName}
              streamCopy={form.streamCopy}
              codec={form.codec}
              container={form.container}
              keepQuality={form.keepQuality}
              audioTracks={form.audioTracks}
              audioStreams={probe.audio_streams ?? []}
              isVideo={isVideo}
              sourceVideoCodec={probe.video_codec ?? null}
              sourceVideoBitrate={probe.video_bitrate ?? null}
              onOutputNameChange={(v) => update('outputName', v)}
              onStreamCopyChange={(v) => {
                update('streamCopy', v)
                if (v) update('keepQuality', false)
              }}
              onCodecChange={(v) => update('codec', v)}
              onContainerChange={(v) => update('container', v)}
              onKeepQualityChange={(v) => update('keepQuality', v)}
              onAudioTracksChange={(v) => update('audioTracks', v)}
            />

            <button type="submit" disabled={busy || !hasFile} className="btn-submit btn-emerald">
              {isCutting ? <span className="spinner-md" /> : 'Cut'}
            </button>
          </>
        )}

        <LogPanel
          log={log}
          error={error}
          hasStarted={hasStarted}
          color="emerald"
          idleMessage="Ready to cut..."
        />

        {/* Download links below log */}
        {outputFiles.length > 0 && jobId && (
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg)] px-5 py-2.5 backdrop-blur-sm">
            {outputFiles.map((file) => (
              <div key={file} className="inline-flex items-center gap-2">
                <a
                  href={getDownloadUrl(jobId, file)}
                  download
                  className="inline-flex items-center gap-1.5 font-mono text-xs text-emerald-400 underline decoration-emerald-400/30 transition-colors hover:decoration-emerald-400"
                >
                  &darr; {file}
                </a>
                {form.source === 'server' && (
                  <button
                    type="button"
                    onClick={() => {
                      saveToSource(jobId, file)
                        .then((r) => onLog([...log, `Saved ${r.filename} to source directory`]))
                        .catch((err) =>
                          onError(
                            `Save failed: ${err instanceof Error ? err.message : String(err)}`,
                          ),
                        )
                    }}
                    className="inline-flex items-center gap-1 rounded-md border border-emerald-400/20 bg-emerald-400/5 px-2 py-0.5 text-[0.65rem] text-emerald-400/70 transition-colors hover:border-emerald-400/40 hover:bg-emerald-400/10 hover:text-emerald-400"
                    title="Save to original file directory"
                  >
                    <svg
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z" />
                      <polyline points="17 21 17 13 7 13 7 21" />
                      <polyline points="7 3 7 8 15 8" />
                    </svg>
                    Save to Source
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </form>
      <JobManager
        activeJobId={jobId}
        onLog={(msg) => onLog([...log, msg])}
        onOpenJob={handleOpenJob}
        showBaseLabel={showBaseLabel}
      />

      <div className="mt-6 flex items-center justify-center">
        <div className="inline-flex items-center gap-3 rounded-full border border-white/6 bg-white/[0.03] px-5 py-2 text-[0.72rem] text-[var(--text-tertiary)]">
          <span>ffmpeg {ffmpegVersionLabel(status, statusFailed)}</span>
          {status?.ffmpeg_available && status.ffmpeg_build && (
            <>
              <span className="text-white/10">·</span>
              <span>
                {status.ffmpeg_build === 'jellyfin' ? 'Jellyfin build' : 'Standard build'}
              </span>
            </>
          )}
        </div>
      </div>
    </PanelLayout>
  )
}

/**
 * Text for the ffmpeg footer pill. Every branch says something true: an
 * unreachable or ffmpeg-less backend is reported, never rendered as a version.
 */
function ffmpegVersionLabel(status: CutterStatus | null, failed: boolean): string {
  if (failed) return 'unavailable'
  if (!status) return '...'
  if (!status.ffmpeg_available) return 'not found'
  return status.ffmpeg_version || 'unknown version'
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}
