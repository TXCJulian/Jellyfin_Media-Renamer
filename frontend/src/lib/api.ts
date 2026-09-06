import { API_BASE, assertAuthenticated } from '@/lib/http'
import { openEventStream } from '@/lib/eventStream'

export { API_BASE, assertAuthenticated }

const DEFAULT_TIMEOUT_MS = 30_000

async function extractErrorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (body.detail) return String(body.detail)
    if (body.error) return String(body.error)
    if (body.reason) return String(body.reason)
  } catch {
    // Response body is not JSON
  }
  return `HTTP ${res.status}: ${res.statusText}`
}

function composeSignals(
  signalA?: AbortSignal | null,
  signalB?: AbortSignal | null,
): AbortSignal | undefined {
  if (!signalA) return signalB ?? undefined
  if (!signalB) return signalA ?? undefined
  if (typeof AbortSignal.any === 'function') {
    return AbortSignal.any([signalA, signalB])
  }
  const controller = new AbortController()
  const onAbort = () => controller.abort()
  if (signalA.aborted) return signalA
  if (signalB.aborted) return signalB
  signalA.addEventListener('abort', onAbort, { once: true })
  signalB.addEventListener('abort', onAbort, { once: true })
  return controller.signal
}

export async function fetchJson<T>(
  path: string,
  params?: Record<string, string>,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  init?: RequestInit,
): Promise<T> {
  const url = new URL(path, API_BASE)
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v) url.searchParams.set(k, v)
    }
  }
  const timeoutSignal = AbortSignal.timeout(timeoutMs)
  const signal = composeSignals(init?.signal, timeoutSignal)
  const res = await fetch(url.toString(), {
    credentials: 'include',
    ...init,
    signal,
  })
  assertAuthenticated(res)
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res))
  }
  const contentType = res.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    const body = await res.text()
    const preview = body.trim().slice(0, 80)
    if (preview.startsWith('<!DOCTYPE') || preview.startsWith('<html')) {
      throw new Error(
        `Expected JSON from ${path}, but received HTML. Check the frontend API proxy.`,
      )
    }
    throw new Error(
      `Expected JSON from ${path}, but received ${contentType || 'unknown content type'}.`,
    )
  }
  return res.json() as Promise<T>
}

export async function postForm<T>(
  path: string,
  data: Record<string, string | number | boolean>,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const formData = new FormData()
  for (const [k, v] of Object.entries(data)) {
    formData.append(k, String(v))
  }
  const url = new URL(path, API_BASE)
  const res = await fetch(url, {
    method: 'POST',
    body: formData,
    signal: AbortSignal.timeout(timeoutMs),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) {
    throw new Error(await extractErrorMessage(res))
  }
  return res.json() as Promise<T>
}

export async function postRefresh(): Promise<void> {
  await postForm('/directories/refresh', {})
}

export async function fetchTranscriberHealth(): Promise<import('@/types').TranscriberHealth> {
  return fetchJson('/transcribe/health')
}

export async function fetchMusicFiles(
  directory: string,
  base: string,
): Promise<import('@/types').MusicFilesResponse> {
  return fetchJson('/transcribe/files', { directory, base })
}

export async function fetchConfig(): Promise<{ features: string[]; base_paths: string[] }> {
  return fetchJson('/config')
}

export async function fetchDownloadDirectories(
  search?: string,
): Promise<import('@/types').DirectoriesResponse> {
  return fetchJson('/directories/download', search ? { search } : undefined)
}

export function uploadFile(
  file: File,
  onProgress?: (pct: number) => void,
): Promise<{
  job_id: string
  file_id: string
  filename: string
}> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', new URL('/cutter/upload', API_BASE).href)
    xhr.withCredentials = true
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name))

    if (onProgress) {
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      })
    }

    xhr.addEventListener('load', () => {
      try {
        assertAuthenticated(xhr)
      } catch (e) {
        reject(e)
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText))
        } catch {
          reject(new Error('Invalid JSON response'))
        }
      } else {
        try {
          const body = JSON.parse(xhr.responseText)
          reject(new Error(body.detail ?? body.error ?? `HTTP ${xhr.status}`))
        } catch {
          reject(new Error(`HTTP ${xhr.status}: ${xhr.statusText}`))
        }
      }
    })

    xhr.addEventListener('error', () => reject(new Error('Upload failed')))
    xhr.addEventListener('abort', () => reject(new Error('Upload aborted')))

    xhr.send(file)
  })
}

export function fetchProbe(
  path: string,
  source: string,
  jobId = '',
  base = '',
  timeoutMs = 60_000,
): Promise<import('@/types').ProbeResult> {
  const params: Record<string, string> = { path, source }
  if (jobId) params.job_id = jobId
  if (base) params.base = base
  return fetchJson<import('@/types').ProbeResult>('/cutter/probe', params, timeoutMs)
}

export function fetchWaveform(
  path: string,
  source: string,
  peaks?: number,
  jobId = '',
  base = '',
  timeoutMs = 120_000,
): Promise<{ peaks: number[] }> {
  const params: Record<string, string> = { path, source }
  if (peaks) params.peaks = String(peaks)
  if (jobId) params.job_id = jobId
  if (base) params.base = base
  return fetchJson<{ peaks: number[] }>('/cutter/waveform', params, timeoutMs)
}

export function fetchCutterFiles(
  directory: string,
  base: string,
): Promise<{ files: import('@/types').CutterFileInfo[] }> {
  return fetchJson<{ files: import('@/types').CutterFileInfo[] }>('/cutter/files', {
    directory,
    base,
  })
}

export function createJob(
  path: string,
  source = 'server',
  base = '',
): Promise<{ job_id: string; file_id: string }> {
  return postForm<{ job_id: string; file_id: string }>('/cutter/jobs', { path, source, base })
}

export function getStreamUrl(
  fileId: string,
  audioStreamIndex?: number | null,
  transcode = false,
): string {
  const base = `/cutter/stream/${encodeURIComponent(fileId)}`
  const params = new URLSearchParams()
  if (audioStreamIndex != null) params.set('audio_stream', String(audioStreamIndex))
  if (transcode) params.set('transcode', 'true')
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

export function getAudioStreamUrl(
  fileId: string,
  audioStreamIndex: number,
  transcode = false,
): string {
  const base = `/cutter/stream/${encodeURIComponent(fileId)}`
  const params = new URLSearchParams()
  params.set('audio_stream', String(audioStreamIndex))
  params.set('audio_only', 'true')
  if (transcode) params.set('transcode', 'true')
  return `${base}?${params.toString()}`
}

export function getAudioOnlyTranscodeUrl(fileId: string, audioStreamIndex: number): string {
  const base = `/cutter/stream/${encodeURIComponent(fileId)}`
  const params = new URLSearchParams()
  params.set('audio_stream', String(audioStreamIndex))
  params.set('transcode_audio_only', 'true')
  return `${base}?${params}`
}

export function fetchPreviewStatus(
  fileId: string,
  options?: { audioTranscodeStream?: number },
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<import('@/types').CutterPreviewStatus> {
  const params: Record<string, string> = {}
  if (options?.audioTranscodeStream != null) {
    params.audio_transcode_stream = String(options.audioTranscodeStream)
  }
  return fetchJson<import('@/types').CutterPreviewStatus>(
    `/cutter/preview-status/${encodeURIComponent(fileId)}`,
    Object.keys(params).length > 0 ? params : undefined,
    timeoutMs,
  )
}

export function getThumbnailUrl(
  path: string,
  source: string,
  jobId = '',
  count = 30,
  base = '',
): string {
  const params = new URLSearchParams({ path, source, count: String(count) })
  if (jobId) params.set('job_id', jobId)
  if (base) params.set('base', base)
  return `/cutter/thumbnails?${params.toString()}`
}

export function getDownloadUrl(jobId: string, filename: string): string {
  return `/cutter/jobs/${encodeURIComponent(jobId)}/download/${encodeURIComponent(filename)}`
}

export async function listJobs(): Promise<{ jobs: import('@/types').CutterJob[] }> {
  return fetchJson<{ jobs: import('@/types').CutterJob[] }>('/cutter/jobs')
}

export async function deleteJob(jobId: string): Promise<void> {
  const url = new URL(`/cutter/jobs/${encodeURIComponent(jobId)}`, API_BASE)
  const res = await fetch(url, {
    method: 'DELETE',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}

export async function getJob(jobId: string): Promise<import('@/types').CutterJob> {
  return fetchJson<import('@/types').CutterJob>(`/cutter/jobs/${encodeURIComponent(jobId)}`)
}

export async function saveToSource(
  jobId: string,
  filename: string,
): Promise<{ status: string; filename: string }> {
  const url = new URL(
    `/cutter/jobs/${encodeURIComponent(jobId)}/save/${encodeURIComponent(filename)}`,
    API_BASE,
  )
  const res = await fetch(url, {
    method: 'POST',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
  return res.json()
}

export async function fetchAuthStatus(): Promise<{
  auth_enabled: boolean
  authenticated: boolean
}> {
  return fetchJson('/auth/status')
}

export async function fetchCutterStatus(): Promise<import('@/types').CutterStatus> {
  return fetchJson('/cutter/status')
}

export async function fetchDownloaderStatus(): Promise<import('@/types').DownloaderStatus> {
  return fetchJson('/download/status')
}

export async function fetchDownloadJobs(): Promise<{ jobs: import('@/types').DownloadJob[] }> {
  return fetchJson('/download/jobs')
}

export async function createDownloads(
  urls: string[],
  options: Omit<import('@/types').DownloadForm, 'url'> | Record<string, unknown>,
): Promise<{ job_ids: string[] }> {
  return fetchJson('/download', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls, options }),
  })
}

export async function startDownloadJob(jobId: string): Promise<{ status: string }> {
  return fetchJson(
    `/download/jobs/${encodeURIComponent(jobId)}/start`,
    undefined,
    DEFAULT_TIMEOUT_MS,
    {
      method: 'POST',
    },
  )
}

export async function cancelDownloadJob(jobId: string): Promise<{ status: string }> {
  return fetchJson(
    `/download/jobs/${encodeURIComponent(jobId)}/cancel`,
    undefined,
    DEFAULT_TIMEOUT_MS,
    {
      method: 'POST',
    },
  )
}

export function openDownloadStream(
  onEvent: (data: string) => void,
  onStateChange?: (connected: boolean) => void,
): () => void {
  return openEventStream('/download/events', onEvent, { onStateChange })
}

export function getDownloadItemFileUrl(jobId: string, index: number): string {
  return `/download/jobs/${encodeURIComponent(jobId)}/items/${index}/file`
}

export async function deleteDownloadJob(jobId: string): Promise<void> {
  const url = new URL(`/download/jobs/${encodeURIComponent(jobId)}`, API_BASE)
  const res = await fetch(url, {
    method: 'DELETE',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}

export function fetchEncoderHealth(): Promise<import('@/types').EncoderHealth> {
  return fetchJson('/api/encoder/health')
}

export function fetchEncoderConfig(): Promise<import('@/types').EncoderConfig> {
  return fetchJson('/api/encoder/config')
}

export function saveEncoderConfig(watchPaths: string[]): Promise<{ watch_paths: string[] }> {
  return fetchJson('/api/encoder/config', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ watch_paths: watchPaths }),
  })
}

export function fetchEncoderDirectories(
  search?: string,
): Promise<{ directories: import('@/types').EncoderDirectory[] }> {
  return fetchJson('/api/encoder/directories', search ? { search } : undefined)
}

export function fetchEncoderFiles(
  directory: string,
  search?: string,
): Promise<{ files: import('@/types').EncoderFile[] }> {
  return fetchJson('/api/encoder/files', {
    directory,
    ...(search ? { search } : {}),
  })
}

export function fetchEncoderPresets(): Promise<import('@/types').EncoderPreset[]> {
  return fetchJson('/api/encoder/presets')
}

export function fetchEncoderPreset(name: string): Promise<{ body: Record<string, unknown> }> {
  return fetchJson(`/api/encoder/presets/${encodeURIComponent(name)}`)
}

export function saveEncoderPreset(
  name: string,
  body: Record<string, unknown>,
): Promise<{ body: Record<string, unknown> }> {
  return fetchJson(
    `/api/encoder/presets/${encodeURIComponent(name)}`,
    undefined,
    DEFAULT_TIMEOUT_MS,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body }),
    },
  )
}

export function previewEncoderPresets(
  document: Record<string, unknown>,
): Promise<import('@/types').EncoderPresetPreviewResult> {
  return fetchJson('/api/encoder/presets/preview', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document }),
  })
}

export function importEncoderPresets(
  document: Record<string, unknown>,
  includeNames?: string[],
): Promise<import('@/types').EncoderPresetImportResult> {
  return fetchJson('/api/encoder/presets', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      document,
      ...(includeNames != null && { include_names: includeNames }),
    }),
  })
}

export async function deleteEncoderPreset(name: string): Promise<void> {
  const url = new URL(`/api/encoder/presets/${encodeURIComponent(name)}`, API_BASE)
  const res = await fetch(url, {
    method: 'DELETE',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}

export function fetchEncoderRules(): Promise<{
  rules: import('@/types').EncoderRule[]
  fallback: string
}> {
  return fetchJson('/api/encoder/rules')
}

export function saveEncoderRules(rules: {
  rules: import('@/types').EncoderRule[]
  fallback: string
}): Promise<{ saved: number }> {
  return fetchJson('/api/encoder/rules', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rules),
  })
}

export function testEncoderFile(path: string): Promise<import('@/types').EncoderTestResult> {
  return fetchJson('/api/encoder/test', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
}

export function reprocessEncoderFile(path: string): Promise<import('@/types').ReprocessResult> {
  return fetchJson('/api/encoder/reprocess', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
}

export function reprocessEncoderJob(jobId: string): Promise<import('@/types').ReprocessResult> {
  return fetchJson(
    `/api/encoder/jobs/${encodeURIComponent(jobId)}/reprocess`,
    undefined,
    DEFAULT_TIMEOUT_MS,
    { method: 'POST' },
  )
}

export function startEncoderReprocessAll(): Promise<import('@/types').EncoderReprocessRun> {
  return fetchJson('/api/encoder/reprocess-all', undefined, DEFAULT_TIMEOUT_MS, { method: 'POST' })
}

export function stopEncoderReprocessAll(): Promise<import('@/types').EncoderReprocessStopResult> {
  return fetchJson('/api/encoder/reprocess-all', undefined, DEFAULT_TIMEOUT_MS, {
    method: 'DELETE',
  })
}

export function fetchEncoderReprocessStatus(
  signal?: AbortSignal,
): Promise<import('@/types').EncoderReprocessState> {
  return fetchJson('/api/encoder/reprocess-all/status', undefined, DEFAULT_TIMEOUT_MS, { signal })
}

export function fetchEncoderJobs(): Promise<import('@/types').EncoderJob[]> {
  return fetchJson('/api/encoder/jobs')
}

export function approveEncoderJob(jobId: string): Promise<{ stage: string }> {
  return fetchJson(
    `/api/encoder/jobs/${encodeURIComponent(jobId)}/approve`,
    undefined,
    DEFAULT_TIMEOUT_MS,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    },
  )
}

export async function deleteEncoderJob(jobId: string): Promise<void> {
  const url = new URL(`/api/encoder/jobs/${encodeURIComponent(jobId)}`, API_BASE)
  const res = await fetch(url, {
    method: 'DELETE',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}

export function openEncoderStream(
  onEvent: (data: string) => void,
  onStateChange?: (connected: boolean) => void,
): () => void {
  return openEventStream('/api/encoder/events', onEvent, { onStateChange })
}

export async function postCookies(file: File): Promise<void> {
  const formData = new FormData()
  formData.append('file', file)
  const url = new URL('/download/cookies', API_BASE)
  const res = await fetch(url, {
    method: 'POST',
    body: formData,
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}

export async function deleteCookies(): Promise<void> {
  const url = new URL('/download/cookies', API_BASE)
  const res = await fetch(url, {
    method: 'DELETE',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  assertAuthenticated(res)
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}

export async function postLogout(): Promise<void> {
  const url = new URL('/auth/logout', API_BASE)
  const res = await fetch(url, {
    method: 'POST',
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await extractErrorMessage(res))
}
