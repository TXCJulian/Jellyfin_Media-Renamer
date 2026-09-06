export interface DirectoryEntry {
  path: string
  base: string
}

export interface EpisodeForm {
  series: string
  season: number
  directory: string
  base: string
  dry_run: boolean
  assign_seq: boolean
  threshold: number
  lang: string
}

/** What to do with .lrc/.txt files next to a renamed audio file. */
export type LyricsAction = 'rename' | 'delete'

export interface MusicForm {
  artist: string
  album: string
  directory: string
  base: string
  dry_run: boolean
  lyrics_action: LyricsAction
}

export interface DirectoriesResponse {
  directories: DirectoryEntry[]
}

export interface RenameResponse {
  success?: boolean
  error?: string
  log?: string[]
  directories?: DirectoryEntry[]
}

export interface LyricsForm {
  artist: string
  album: string
  directory: string
  base: string
  format: 'lrc' | 'txt' | 'all'
  skip_existing: boolean
  language: string
  no_separation: boolean
  no_correction: boolean
  demucs_model: string
  whisper_model: string
  /** Genius lookup overrides — only sent when exactly one song is selected. */
  artist_override: string
  title_override: string
}

export interface TranscriberHealth {
  status: string
  gpu_backend?: string
  gpu_name?: string | null
  transcription_engine?: string
  vram_total_mb?: number | null
  whisper_model_fit?: Record<string, boolean> | null
  error?: string
}

export interface MusicFileInfo {
  name: string
  has_lrc: boolean
  has_txt: boolean
}

export interface MusicFilesResponse {
  files: MusicFileInfo[]
  error?: string
}

export interface CutterForm {
  source: 'server' | 'upload'
  directory: string
  base: string
  filename: string
  inPoint: number
  outPoint: number
  outputName: string
  streamCopy: boolean
  codec: string
  container: string
  audioTracks: AudioTrackConfig[]
  keepQuality: boolean
}

export interface AudioStreamInfo {
  index: number
  codec: string
  channels: number
  sample_rate: number
  bit_rate: number
  language: string
  title: string
}

export interface AudioTrackConfig {
  streamIndex: number
  mode: 'passthru' | 'reencode' | 'remove'
  codec: string
}

export interface ProbeResult {
  duration: number
  video_codec: string | null
  audio_codec: string
  container: string
  bitrate: number
  video_bitrate: number | null
  width: number | null
  height: number | null
  display_aspect_ratio: string | null
  sample_rate: number
  needs_transcoding: boolean
  audio_streams: AudioStreamInfo[]
}

export interface CutterPreviewStatus {
  state: 'idle' | 'running' | 'done' | 'error'
  ready: boolean
  percent: number
  eta_seconds: number | null
  elapsed_seconds: number
  message: string
}

export interface CutterFileInfo {
  name: string
  size: number
  extension: string
  file_id: string
}

export interface CutterSourceState {
  probe: ProbeResult | null
  peaks: number[]
  filePath: string
  fileId: string
  thumbnailUrl: string
  files: CutterFileInfo[]
  jobId: string
  outputFiles: string[]
  isLoadingFile: boolean
}

export interface CutJobSettings {
  in_point: number
  out_point: number
  stream_copy: boolean
  codec: string | null
  container: string | null
  audio_tracks: { index: number; mode: string; codec: string | null }[]
  keep_quality: boolean
  output_name: string | null
}

export interface CutterJob {
  job_id: string
  source: 'server' | 'upload'
  original_name: string
  original_path: string
  created_at: string
  status:
    | 'uploading'
    | 'ready'
    | 'full_transcoding'
    | 'audio_transcoding'
    | 'transcoding'
    | 'cutting'
    | 'done'
    | 'error'
  output_files: string[]
  cut_settings?: CutJobSettings | null
  preview_transcoded?: boolean
  audio_transcoded_tracks?: number[]
  browser_ready?: boolean
  transcode_error?: string | null
  base?: string
  source_file_id?: string
}

export interface CutterPersistedState {
  form: CutterForm
  directories: DirectoryEntry[]
  search: string
  serverState: CutterSourceState
  uploadState: CutterSourceState
}

export type DownloadStage =
  'queued' | 'downloading' | 'transcoding' | 'done' | 'cancelled' | 'error'

export interface DownloadItem {
  index: number
  title: string
  path: string | null
  size: number | null
  progress: number
  stage: DownloadStage
  error: string | null
}

export interface DownloadJob {
  job_id: string
  url: string
  stage: DownloadStage
  error: string | null
  created_at: string
  updated_at: string
  items: DownloadItem[]
  has_transcode: boolean
}

export interface DownloadForm {
  url: string
  type: 'video' | 'audio' | 'thumbnail'
  codec: string
  format: string
  quality: string
  output_dir: string
  base: string
  auto_start: boolean
  sub_folder: string
  custom_prefix: string
  custom_filename: string
  item_limit: number
}

export interface CutterStatus {
  ffmpeg_available: boolean
  ffmpeg_version: string
  /** 'jellyfin' | 'standard', or '' when ffmpeg is unavailable. */
  ffmpeg_build: string
  ffmpeg_path: string
}

export interface DownloaderStatus {
  yt_dlp_version: string
  cookies_present: boolean
  downloads_dir: string
  queue_depth: number
  workers: number
}

export interface EncoderHealth {
  status: string
  vendor?: 'NVENC' | 'QSV' | 'VCE' | 'CPU'
  gpu_name?: string | null
  handbrake_version?: string
  encoders?: string[]
  encoder_presets?: Record<string, string[]>
  allowed_roots?: string[]
  workers?: number
  error?: string
}

export interface EncoderConfig {
  watch_paths: string[]
  mode: 'auto' | 'review'
  settle_seconds: number
  original_ttl: number
  job_ttl: number
}

export interface EncoderDirectory {
  path: string
  base: string
}

export interface EncoderFile {
  path: string
  name: string
}

export interface EncoderPreset {
  name: string
  encoder: string
  video_preset: string
  file_format: string
  /** The full HandBrake leaf, fetched before editing. */
  body?: Record<string, unknown>
}

export interface EncoderPresetPreview {
  name: string
  encoder: string
  supported: boolean
  reason: string | null
}

export interface EncoderPresetPreviewResult {
  presets: EncoderPresetPreview[]
}

export interface EncoderPresetImportSkip {
  name: string
  encoder: string
  reason: string
}

export interface EncoderPresetImportResult {
  imported: string[]
  skipped: EncoderPresetImportSkip[]
  unselected: string[]
}

export type EncoderJobStage =
  | 'settling'
  | 'pending'
  | 'queued'
  | 'encoding'
  | 'swapping'
  | 'done'
  | 'failed'
  | 'blocked'
  | 'cancelled'
  | 'skipped'

export interface EncoderJob {
  job_id: string
  source_path: string
  stage: EncoderJobStage
  progress: number
  preset_name: string | null
  rule_id: string | null
  error: string | null
  error_code: string | null
  remote_job_id?: string | null
  output_path: string | null
  facts: Record<string, unknown>
  original_size: number | null
  encoded_size: number | null
  saved_bytes: number | null
  created_at: string
  updated_at: string
}

export interface ReprocessResult {
  job_id: string
  path: string
  stage: EncoderJobStage
  created: boolean
}

export type EncoderReprocessStatus = 'started' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface EncoderReprocessEvent {
  type: 'reprocess'
  run_id: string
  status: EncoderReprocessStatus
  scanned: number
  created: number
  skipped: number
  failed: number
  path: string | null
  error: string | null
}

export interface EncoderReprocessRun {
  run_id: string
  status: EncoderReprocessStatus | 'already_running'
}

export interface EncoderReprocessStopResult {
  status: 'stopping'
}

export interface EncoderReprocessState {
  active: boolean
  event: EncoderReprocessEvent | null
}

export interface EncoderRuleCondition {
  field: string
  op: string
  value: unknown
}

export interface EncoderRule {
  id: string
  conditions: EncoderRuleCondition[]
  target: string
}

export interface EncoderTestResult {
  facts: Record<string, unknown>
  matched_rule: string | null
  target: string
  evaluated: string[]
  not_evaluated: string[]
}
