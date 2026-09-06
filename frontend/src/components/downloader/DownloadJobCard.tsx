import IconButton from '@/components/ui/IconButton'
import StatusPill from '@/components/ui/StatusPill'
import { PlayIcon, RetryIcon, StopIcon, TrashIcon } from '@/components/ui/icons'
import { getDownloadItemFileUrl } from '@/lib/api'
import type { DownloadJob, DownloadStage } from '@/types'

const STAGE_LABELS: Record<DownloadStage, string> = {
  queued: 'Queued',
  downloading: 'Downloading',
  transcoding: 'Transcoding',
  done: 'Done',
  cancelled: 'Cancelled',
  error: 'Failed',
}

const PIPELINE: DownloadStage[] = ['downloading', 'transcoding', 'done']

/**
 * Colours for the terminal-state pills, borrowed from the cutter's job tags.
 * Only outcomes get a pill; the in-progress stages stay a plain strip because
 * the strip conveys pipeline position, which a single pill cannot.
 */
const TERMINAL_PILL: Partial<Record<DownloadStage, string>> = {
  done: 'bg-cyan-400/15 text-cyan-300',
  cancelled: 'bg-white/10 text-white/50',
  error: 'bg-red-400/15 text-red-300',
}

/** Last path segment, used as the display filename for a finished item's download link. */
function basename(path: string | null): string | null {
  if (!path) return null
  const parts = path.split(/[/\\]/)
  return parts[parts.length - 1] || null
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return ''
  const units = ['B', 'KiB', 'MiB', 'GiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`
}

/**
 * A per-item message from the backend.
 *
 * `item.error` carries two different things: an actual failure on an item
 * whose stage is `error`, and an advisory note on an item that succeeded —
 * the pre-existing-file explanation, which is the entire mitigation for a
 * known spec deviation and is useless if it is never shown. Only the first
 * is styled as a failure.
 */
function ItemMessage({ stage, error }: { stage: DownloadStage; error: string }) {
  const failed = stage === 'error'
  return (
    <p
      data-testid="item-error"
      className={`mt-1 text-[0.72rem] ${failed ? 'text-red-400' : 'text-[var(--accent-6)]'}`}
    >
      {error}
    </p>
  )
}

interface Props {
  job: DownloadJob
  onCancel: (jobId: string) => void
  onDelete: (jobId: string) => void
  onStart: (jobId: string) => void
  onRetry: (url: string) => void
}

export default function DownloadJobCard({ job, onCancel, onDelete, onStart, onRetry }: Props) {
  const isActive = ['queued', 'downloading', 'transcoding'].includes(job.stage)
  const isTerminal = job.stage === 'error' || job.stage === 'cancelled'
  const stages = job.has_transcode ? PIPELINE : PIPELINE.filter((s) => s !== 'transcoding')
  // `error`/`cancelled` aren't part of the pipeline, so stages.indexOf(job.stage)
  // is always -1 for them. Treat every pipeline stage as reached so the strip
  // stays legible instead of collapsing to 40%-opacity tertiary text.
  const reached = (stage: DownloadStage) =>
    job.stage === 'done' || isTerminal || stages.indexOf(job.stage) >= stages.indexOf(stage)

  const overall =
    job.items.length > 0 && job.items.every((item) => item.progress_known !== false)
      ? job.items.reduce((sum, item) => sum + item.progress, 0) / job.items.length
      : null

  return (
    <div className="glass-light rounded-[14px] p-4">
      <div className="mb-2 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-[0.88rem] font-medium text-[var(--text-primary)]">
            {job.items[0]?.title || job.url}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            {stages.map((stage) =>
              // 'Done' is the one pipeline entry that is also an outcome, so
              // once it is reached it becomes the pill instead of being
              // repeated as one.
              stage === 'done' && job.stage === 'done' ? (
                <StatusPill key={stage} className={TERMINAL_PILL.done}>
                  {STAGE_LABELS.done}
                </StatusPill>
              ) : (
                <span
                  key={stage}
                  className={`text-[0.68rem] uppercase tracking-[0.1em] ${
                    job.stage === stage
                      ? 'text-[var(--accent-6)]'
                      : reached(stage)
                        ? 'text-[var(--text-secondary)]'
                        : 'text-[var(--text-tertiary)]/40'
                  }`}
                >
                  {STAGE_LABELS[stage]}
                </span>
              ),
            )}
            {!isActive && job.stage !== 'done' && (
              <StatusPill className={TERMINAL_PILL[job.stage]} title={job.error ?? undefined}>
                {STAGE_LABELS[job.stage]}
              </StatusPill>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {job.stage === 'queued' && (
            <IconButton
              label="Start download"
              onClick={() => onStart(job.job_id)}
              tone="accent"
              accentClass="hover:bg-cyan-500/10 hover:text-[var(--accent-6)]"
            >
              <PlayIcon size={14} />
            </IconButton>
          )}
          {isActive && (
            <IconButton label="Cancel download" onClick={() => onCancel(job.job_id)} tone="danger">
              <StopIcon />
            </IconButton>
          )}
          {(job.stage === 'error' || job.stage === 'cancelled') && (
            <IconButton
              label="Retry download"
              onClick={() => onRetry(job.url)}
              tone="accent"
              accentClass="hover:bg-cyan-500/10 hover:text-[var(--accent-6)]"
            >
              <RetryIcon />
            </IconButton>
          )}
          <IconButton label="Delete job" onClick={() => onDelete(job.job_id)} tone="danger">
            <TrashIcon />
          </IconButton>
        </div>
      </div>

      {isActive && (
        <>
          <div
            role="progressbar"
            aria-label="Download progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={overall === null ? undefined : Math.max(0, Math.min(overall, 100))}
            className="h-1.5 overflow-hidden rounded-full bg-white/6"
          >
            <div
              className={`h-full rounded-full bg-[var(--accent-6)] transition-all duration-300 ${overall === null ? 'motion-safe:animate-pulse' : ''}`}
              style={{
                width: overall === null ? '100%' : `${Math.max(0, Math.min(overall, 100))}%`,
              }}
            />
          </div>
          <p className="mt-1.5 text-[0.72rem] tabular-nums text-[var(--text-secondary)]">
            {overall === null ? 'Progress unavailable' : `${overall.toFixed(1)}%`}
          </p>
        </>
      )}

      {job.items.length > 1 && (
        <ul className="mt-3 space-y-1.5 border-t border-white/6 pt-3">
          {job.items.map((item) => (
            <li key={item.index} className="text-[0.75rem]">
              <div className="flex items-center gap-3">
                <span className="min-w-0 flex-1 truncate text-[var(--text-secondary)]">
                  {item.title}
                </span>
                <span className="tabular-nums text-[var(--text-tertiary)]">
                  {item.stage === 'done'
                    ? formatSize(item.size)
                    : item.progress_known === false
                      ? 'Progress unavailable'
                      : `${item.progress.toFixed(0)}%`}
                </span>
                {item.stage === 'done' && (
                  <a
                    href={getDownloadItemFileUrl(job.job_id, item.index)}
                    download
                    className="shrink-0 font-mono text-[0.68rem] text-[var(--accent-6)]/70 underline decoration-[var(--accent-6)]/20 hover:decoration-[var(--accent-6)]/60"
                  >
                    &darr; Save
                  </a>
                )}
              </div>
              {item.error && <ItemMessage stage={item.stage} error={item.error} />}
            </li>
          ))}
        </ul>
      )}

      {job.items.length === 1 && (
        <>
          {job.stage === 'done' && (
            <div className="mt-2 flex items-center gap-3 text-[0.72rem] text-[var(--text-tertiary)]">
              <span>{formatSize(job.items[0]!.size)}</span>
              <a
                href={getDownloadItemFileUrl(job.job_id, job.items[0]!.index)}
                download
                className="min-w-0 truncate font-mono text-[0.68rem] text-[var(--accent-6)]/70 underline decoration-[var(--accent-6)]/20 hover:decoration-[var(--accent-6)]/60"
              >
                &darr; {basename(job.items[0]!.path) || 'Save'}
              </a>
            </div>
          )}
          {job.items[0]!.error && (
            <ItemMessage stage={job.items[0]!.stage} error={job.items[0]!.error} />
          )}
        </>
      )}

      {job.error && <p className="mt-2 text-[0.78rem] text-red-400">{job.error}</p>}
    </div>
  )
}
