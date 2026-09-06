import { useId, useState } from 'react'
import IconButton from '@/components/ui/IconButton'
import StatusPill from '@/components/ui/StatusPill'
import { InfoIcon, PlayIcon, TrashIcon } from '@/components/ui/icons'
import { formatBytes, formatFactValue } from '@/components/encoder/mediaFacts'
import type { EncoderJob, EncoderJobStage } from '@/types'

const PIPELINE: { stage: EncoderJobStage; label: string }[] = [
  { stage: 'settling', label: 'Settling' },
  { stage: 'encoding', label: 'Encoding' },
  { stage: 'swapping', label: 'Swapping' },
  { stage: 'done', label: 'Done' },
]

const TERMINAL_PILL: Partial<Record<EncoderJobStage, { label: string; className: string }>> = {
  done: { label: 'Done', className: 'bg-teal-400/15 text-teal-300' },
  failed: { label: 'Failed', className: 'bg-red-400/15 text-red-300' },
  cancelled: { label: 'Cancelled', className: 'bg-white/10 text-white/50' },
  skipped: { label: 'Skipped', className: 'bg-amber-400/15 text-amber-300' },
}

const REVIEW_LABEL: Partial<Record<EncoderJobStage, string>> = {
  pending: 'Pending approval',
  blocked: 'Blocked',
}

interface Props {
  job: EncoderJob
  onApprove: (jobId: string) => void
  onDelete: (jobId: string) => void
  onReprocess?: (jobId: string) => void
  reprocessing?: boolean
  connected?: boolean
}

function remainingTime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return 'Estimating…'
  if (seconds < 60) return 'Less than a minute remaining'
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `About ${minutes} min remaining`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return `About ${hours} h${rest ? ` ${rest} min` : ''} remaining`
}

function basename(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean)
  return parts[parts.length - 1] ?? path
}

function savings(job: EncoderJob): string | null {
  if (job.original_size === null || job.encoded_size === null || job.original_size <= 0) return null
  const change = Math.round(
    (Math.abs(job.original_size - job.encoded_size) / job.original_size) * 100,
  )
  const direction = job.encoded_size <= job.original_size ? 'smaller' : 'larger'
  return `${formatBytes(job.original_size)} → ${formatBytes(job.encoded_size)} · ${change}% ${direction}`
}

function stagePosition(stage: EncoderJobStage): number {
  if (stage === 'settling') return 0
  if (stage === 'pending' || stage === 'blocked') return 0
  if (stage === 'queued' || stage === 'encoding') return 1
  if (stage === 'swapping') return 2
  if (stage === 'done') return 3
  return -1
}

export default function EncoderJobCard({
  job,
  onApprove,
  onDelete,
  onReprocess,
  reprocessing = false,
  connected = true,
}: Props) {
  const [showFacts, setShowFacts] = useState(false)
  const factsId = useId()
  const terminal = TERMINAL_PILL[job.stage]
  const reviewLabel = REVIEW_LABEL[job.stage]
  const currentPosition = stagePosition(job.stage)
  const canApprove =
    (job.stage === 'pending' || job.stage === 'blocked') && job.error_code !== 'swap_interrupted'
  const canDelete = job.stage !== 'swapping'
  const canReprocess =
    (job.stage === 'failed' ||
      (job.stage === 'blocked' && job.error_code !== 'swap_interrupted' && !job.remote_job_id)) &&
    onReprocess
  const hasFacts = Object.keys(job.facts).length > 0
  const factLabels = Object.entries(job.facts)
    .map(([key, value]) => formatFactValue(key, value, job.facts))
    .filter((value): value is string => Boolean(value))
  const reduction = savings(job)

  return (
    <article className="glass-light rounded-[14px] p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-[0.88rem] font-medium text-[var(--text-primary)]">
            {basename(job.source_path)}
          </h3>
          <p className="mt-1 truncate font-mono text-[0.68rem] text-[var(--text-tertiary)]">
            {job.source_path}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {hasFacts && (
            <button
              type="button"
              aria-label={showFacts ? 'Hide media details' : 'Show media details'}
              title={showFacts ? 'Hide media details' : 'Show media details'}
              aria-expanded={showFacts}
              aria-controls={factsId}
              onClick={() => setShowFacts((visible) => !visible)}
              className="rounded-md p-1.5 text-white/25 transition hover:bg-teal-500/10 hover:text-teal-300"
            >
              <InfoIcon />
            </button>
          )}
          <>
            {canApprove && (
              <button
                type="button"
                aria-label="Approve encoding"
                title="Approve encoding"
                onClick={() => onApprove(job.job_id)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-teal-400/20 bg-teal-400/10 px-2.5 py-1.5 text-[0.72rem] font-medium text-teal-300"
              >
                <PlayIcon size={13} />
                Approve
              </button>
            )}
            {canReprocess && (
              <button
                type="button"
                aria-label="Re-evaluate file"
                disabled={reprocessing}
                onClick={() => onReprocess(job.job_id)}
                className="rounded-lg border border-amber-400/20 bg-amber-400/10 px-2.5 py-1.5 text-[0.72rem] font-medium text-amber-300 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {reprocessing ? 'Re-evaluating…' : 'Re-evaluate'}
              </button>
            )}
            {!canApprove &&
              (canDelete ? (
                <IconButton label="Delete job" onClick={() => onDelete(job.job_id)} tone="danger">
                  <TrashIcon />
                </IconButton>
              ) : (
                <span className="text-[0.68rem] text-[var(--text-tertiary)]">Publishing…</span>
              ))}
          </>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {PIPELINE.map(({ stage, label }, index) => {
          if (stage === 'done' && terminal?.label === 'Done') {
            return (
              <StatusPill key={stage} className={terminal.className}>
                Done
              </StatusPill>
            )
          }
          const active = job.stage === stage || (stage === 'encoding' && job.stage === 'queued')
          const reached = currentPosition >= index || currentPosition === -1
          const displayLabel = stage === 'encoding' && job.stage === 'queued' ? 'Waiting' : label
          return (
            <span
              key={stage}
              className={`text-[0.68rem] uppercase tracking-[0.1em] ${
                active
                  ? 'text-teal-300'
                  : reached
                    ? 'text-[var(--text-secondary)]'
                    : 'text-[var(--text-tertiary)]/40'
              }`}
            >
              {displayLabel}
            </span>
          )
        })}
        {terminal && terminal.label !== 'Done' && (
          <StatusPill className={terminal.className} title={job.error ?? undefined}>
            {terminal.label}
          </StatusPill>
        )}
        {reviewLabel && (
          <span className="text-[0.68rem] font-medium uppercase tracking-[0.1em] text-amber-300">
            {reviewLabel}
          </span>
        )}
      </div>

      {(job.stage === 'encoding' || job.stage === 'swapping') && (
        <div className="mt-3">
          <div
            role="progressbar"
            aria-label="Encoding progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.max(0, Math.min(job.progress, 100))}
            className="h-1.5 overflow-hidden rounded-full bg-white/6"
          >
            <div
              className="h-full rounded-full bg-teal-400 transition-all duration-300"
              style={{ width: `${Math.max(0, Math.min(job.progress, 100))}%` }}
            />
          </div>
          <p className="mt-1.5 text-[0.72rem] tabular-nums text-[var(--text-secondary)]">
            {job.progress.toFixed(1)}%
            {job.stage === 'encoding' && connected && (
              <span> · {remainingTime(job.eta_seconds)}</span>
            )}
          </p>
        </div>
      )}

      {(job.preset_name || job.rule_id) && (
        <p className="mt-2 text-[0.72rem] text-[var(--text-tertiary)]">
          {job.preset_name && <span>Preset: {job.preset_name}</span>}
          {job.preset_name && job.rule_id && <span> · </span>}
          {job.rule_id && <span>Rule: {job.rule_id}</span>}
        </p>
      )}

      {reduction && (
        <p className="mt-2 text-[0.76rem] font-medium tabular-nums text-teal-300">{reduction}</p>
      )}

      {showFacts && hasFacts && (
        <div
          id={factsId}
          className="mt-3 flex flex-wrap gap-2 border-t border-white/6 pt-3"
          aria-label="Media details"
        >
          {factLabels.map((fact) => (
            <span
              key={fact}
              className="whitespace-pre-line rounded-md border border-white/8 bg-white/4 px-2 py-1 text-[0.7rem] text-[var(--text-secondary)]"
            >
              {fact}
            </span>
          ))}
          <pre
            aria-label="Raw media facts"
            className="w-full overflow-x-auto rounded-md bg-black/20 p-2 font-mono text-[0.64rem] text-[var(--text-tertiary)]"
          >
            {JSON.stringify(job.facts, null, 2)}
          </pre>
        </div>
      )}

      {job.error && <p className="mt-2 text-[0.78rem] text-red-400">{job.error}</p>}
    </article>
  )
}
