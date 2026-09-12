import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import PanelLayout from '@/components/PanelLayout'
import EncoderJobCard from '@/components/encoder/EncoderJobCard'
import EncoderSettings from '@/components/encoder/EncoderSettings'
import { useEncoderStream } from '@/hooks/useEncoderStream'
import {
  approveEncoderJob,
  deleteEncoderJob,
  fetchEncoderConfig,
  fetchEncoderHealth,
  fetchEncoderJobs,
  fetchEncoderPreset,
  fetchEncoderPresets,
  fetchEncoderRules,
  reprocessEncoderJob,
  startEncoderReprocessAll,
  stopEncoderReprocessAll,
} from '@/lib/api'
import type { EncoderConfig, EncoderHealth, EncoderJob, EncoderPreset, EncoderRule } from '@/types'

type EncoderRules = {
  rules: EncoderRule[]
  fallback: string
}

type EncoderPanelProps = {
  onBack: () => void
}

const TERMINAL_STAGES = new Set<EncoderJob['stage']>(['done', 'failed', 'cancelled', 'skipped'])

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function shortGpuName(gpu: string | null | undefined): string {
  if (!gpu) return ''
  return gpu.replace(/^NVIDIA GeForce /i, '')
}

function isAtLeastAsRecent(candidate: EncoderJob, current: EncoderJob): boolean {
  const candidateTime = Date.parse(candidate.updated_at)
  const currentTime = Date.parse(current.updated_at)
  if (Number.isFinite(candidateTime) && Number.isFinite(currentTime)) {
    return candidateTime >= currentTime
  }
  return candidate.updated_at >= current.updated_at
}

type AcknowledgedStage = {
  stage: EncoderJob['stage']
  baseUpdatedAt: string
}

export default function EncoderPanel({ onBack }: EncoderPanelProps) {
  const {
    jobs: streamedJobs,
    connected,
    snapshotReceived,
    latestReprocessEvent,
    reprocessActive,
  } = useEncoderStream()
  const [health, setHealth] = useState<EncoderHealth | null>(null)
  const [checkingHealth, setCheckingHealth] = useState(true)
  const [config, setConfig] = useState<EncoderConfig | null>(null)
  const [presets, setPresets] = useState<EncoderPreset[]>([])
  const [rules, setRules] = useState<EncoderRules | null>(null)
  const [initialJobs, setInitialJobs] = useState<EncoderJob[]>([])
  const [loading, setLoading] = useState(true)
  const [resourceError, setResourceError] = useState('')
  const [settingsError, setSettingsError] = useState('')
  const [jobErrors, setJobErrors] = useState<Map<string, string>>(() => new Map())
  const [historyOpen, setHistoryOpen] = useState(false)
  const [deletedJobIds, setDeletedJobIds] = useState<Set<string>>(() => new Set())
  const [acknowledgedStages, setAcknowledgedStages] = useState<Map<string, AcknowledgedStage>>(
    () => new Map(),
  )
  const mounted = useRef(true)
  const healthRequest = useRef(0)
  const resourceRequest = useRef(0)
  const approvingJobs = useRef(new Set<string>())
  const reprocessingJobs = useRef(new Set<string>())
  const [reprocessingJobIds, setReprocessingJobIds] = useState<Set<string>>(() => new Set())

  const checkHealth = useCallback(async () => {
    const request = ++healthRequest.current
    setCheckingHealth(true)
    try {
      const next = await fetchEncoderHealth()
      if (mounted.current && request === healthRequest.current) setHealth(next)
    } catch {
      if (mounted.current && request === healthRequest.current) setHealth(null)
    } finally {
      if (mounted.current && request === healthRequest.current) setCheckingHealth(false)
    }
  }, [])

  const refreshResources = useCallback(async (includeJobs: boolean) => {
    const request = ++resourceRequest.current
    setLoading(true)
    try {
      const [nextConfig, presetSummaries, nextRules, nextJobs] = await Promise.all([
        fetchEncoderConfig(),
        fetchEncoderPresets(),
        fetchEncoderRules(),
        includeJobs ? fetchEncoderJobs() : Promise.resolve<EncoderJob[] | null>(null),
      ])
      const completePresets = await Promise.all(
        presetSummaries.map(async (preset) => ({
          ...preset,
          body: (await fetchEncoderPreset(preset.name)).body,
        })),
      )
      if (!mounted.current || request !== resourceRequest.current) return
      setConfig(nextConfig)
      setPresets(completePresets)
      setRules(nextRules)
      if (nextJobs) setInitialJobs(nextJobs)
      setResourceError('')
    } catch (loadError) {
      if (mounted.current && request === resourceRequest.current) {
        setResourceError(errorMessage(loadError, 'Failed to load encoder settings.'))
      }
    } finally {
      if (mounted.current && request === resourceRequest.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    void checkHealth()
    void refreshResources(true)
    return () => {
      mounted.current = false
      healthRequest.current += 1
      resourceRequest.current += 1
    }
  }, [checkHealth, refreshResources])

  const jobs = useMemo(() => {
    const freshest = new Map((snapshotReceived ? [] : initialJobs).map((job) => [job.job_id, job]))
    for (const streamed of streamedJobs) {
      const current = freshest.get(streamed.job_id)
      if (!current || isAtLeastAsRecent(streamed, current)) freshest.set(streamed.job_id, streamed)
    }

    const seen = new Set<string>()
    return [...streamedJobs, ...(snapshotReceived ? [] : initialJobs)]
      .map((job) => freshest.get(job.job_id)!)
      .filter((job) => {
        if (seen.has(job.job_id) || deletedJobIds.has(job.job_id)) return false
        seen.add(job.job_id)
        return true
      })
      .map((job) => {
        const acknowledged = acknowledgedStages.get(job.job_id)
        if (!acknowledged) return job
        if (job.stage !== 'pending' && job.stage !== 'blocked') return job
        if (
          isAtLeastAsRecent(job, { ...job, updated_at: acknowledged.baseUpdatedAt }) &&
          job.updated_at !== acknowledged.baseUpdatedAt
        ) {
          return job
        }
        return { ...job, stage: acknowledged.stage }
      })
      .filter((job) => job.stage !== 'skipped')
  }, [acknowledgedStages, deletedJobIds, initialJobs, snapshotReceived, streamedJobs])
  const activeJobs = jobs.filter((job) => !TERMINAL_STAGES.has(job.stage))
  const history = jobs.filter((job) => TERMINAL_STAGES.has(job.stage))

  const updateJobError = (operation: string, message?: string) => {
    setJobErrors((current) => {
      const next = new Map(current)
      if (message) next.set(operation, message)
      else next.delete(operation)
      return next
    })
  }

  const approve = async (jobId: string) => {
    if (approvingJobs.current.has(jobId)) return
    approvingJobs.current.add(jobId)
    const operation = `approve:${jobId}`
    try {
      const baseUpdatedAt = jobs.find((job) => job.job_id === jobId)?.updated_at ?? ''
      const result = await approveEncoderJob(jobId)
      if (result.stage === 'queued') {
        setAcknowledgedStages((current) =>
          new Map(current).set(jobId, { stage: 'queued', baseUpdatedAt }),
        )
      }
      updateJobError(operation)
    } catch (actionError) {
      updateJobError(operation, errorMessage(actionError, 'Failed to approve encoder job.'))
    } finally {
      approvingJobs.current.delete(jobId)
    }
  }

  const remove = async (jobId: string) => {
    const operation = `delete:${jobId}`
    try {
      await deleteEncoderJob(jobId)
      setDeletedJobIds((current) => new Set(current).add(jobId))
      setAcknowledgedStages((current) => {
        const next = new Map(current)
        next.delete(jobId)
        return next
      })
      updateJobError(operation)
    } catch (actionError) {
      updateJobError(operation, errorMessage(actionError, 'Failed to delete encoder job.'))
    }
  }

  const reprocess = async (jobId: string) => {
    if (reprocessingJobs.current.has(jobId)) return
    reprocessingJobs.current.add(jobId)
    setReprocessingJobIds((current) => new Set(current).add(jobId))
    const operation = `reprocess:${jobId}`
    try {
      const baseUpdatedAt = jobs.find((job) => job.job_id === jobId)?.updated_at ?? ''
      const result = await reprocessEncoderJob(jobId)
      if (result.created) {
        setAcknowledgedStages((current) =>
          new Map(current).set(jobId, { stage: 'cancelled', baseUpdatedAt }),
        )
      }
      updateJobError(operation)
    } catch (actionError) {
      updateJobError(operation, errorMessage(actionError, 'Failed to re-evaluate encoder job.'))
    } finally {
      reprocessingJobs.current.delete(jobId)
      setReprocessingJobIds((current) => {
        const next = new Set(current)
        next.delete(jobId)
        return next
      })
    }
  }

  const healthy = health?.status === 'ok'
  const healthLabel = checkingHealth
    ? 'Checking...'
    : healthy
      ? shortGpuName(health.gpu_name) || health.vendor || 'Online'
      : 'Offline'
  const healthPill = (
    <button
      type="button"
      onClick={() => void checkHealth()}
      disabled={checkingHealth}
      title={healthy ? 'Click to re-check encoder connection' : 'Click to retry encoder connection'}
      className="ml-auto inline-flex cursor-pointer items-center gap-[0.4rem] rounded-[20px] border border-[var(--border)] bg-[var(--bg-input)] px-[0.7rem] py-[0.3rem] text-[0.7rem] font-medium text-[var(--text-tertiary)] transition-all duration-200 hover:border-[var(--glass-border-hover)] hover:bg-[var(--bg-glass-hover)] hover:text-[var(--text-secondary)] disabled:cursor-not-allowed disabled:opacity-70"
    >
      <span
        className={`h-[7px] w-[7px] shrink-0 rounded-full transition-colors duration-200 ${
          checkingHealth
            ? 'animate-pulse bg-yellow-500'
            : healthy
              ? 'bg-[var(--success)] shadow-[0_0_8px_var(--success-glow)]'
              : 'bg-[var(--error)]'
        }`}
      />
      {healthLabel}
    </button>
  )
  const visibleErrors = [
    ...(resourceError ? [{ key: 'resources', message: resourceError }] : []),
    ...(settingsError ? [{ key: 'settings', message: settingsError }] : []),
    ...Array.from(jobErrors, ([key, message]) => ({ key, message })),
  ]

  return (
    <div className="encoder-panel">
      <PanelLayout title="Auto Encoder" onBack={onBack} rightElement={healthPill} maxWidth="920px">
        {visibleErrors.map(({ key, message }) => (
          <div
            key={key}
            role="alert"
            className="mb-5 flex items-center justify-between rounded-lg border border-red-500/15 bg-red-500/[0.06] px-4 py-2.5"
          >
            <p className="text-[0.8rem] text-red-400">{message}</p>
            <button
              type="button"
              aria-label={`Dismiss ${key} error`}
              onClick={() => {
                if (key === 'resources') setResourceError('')
                else if (key === 'settings') setSettingsError('')
                else updateJobError(key)
              }}
              className="ml-3 shrink-0 text-[0.7rem] text-red-400/60"
            >
              dismiss
            </button>
          </div>
        ))}

        {loading && (!config || !rules) ? (
          <p className="py-8 text-center text-[0.82rem] text-[var(--text-tertiary)]">
            Loading encoder configuration…
          </p>
        ) : config && rules ? (
          <EncoderSettings
            config={config}
            health={health}
            presets={presets}
            rules={rules}
            onRefresh={() => {
              void refreshResources(false)
            }}
            onError={setSettingsError}
            onStartReprocessAll={startEncoderReprocessAll}
            onStopReprocessAll={stopEncoderReprocessAll}
            latestReprocessEvent={latestReprocessEvent}
            reprocessActive={reprocessActive}
          />
        ) : (
          <button
            type="button"
            onClick={() => void refreshResources(true)}
            className="mb-6 w-full rounded-xl border border-[var(--accent-5)]/30 bg-[var(--accent-5-glow)] px-4 py-3 text-[0.8rem] font-medium text-[var(--accent-5)]"
          >
            Retry loading encoder configuration
          </button>
        )}

        <section aria-label="Active encoder jobs" className="mt-6">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-[0.72rem] font-semibold uppercase tracking-[0.12em] text-[var(--text-secondary)]">
              Active {activeJobs.length > 0 && `— ${activeJobs.length}`}
            </h3>
            <span className="text-[0.68rem] text-[var(--text-tertiary)]">
              {connected ? 'Live updates' : 'Reconnecting…'}
            </span>
          </div>
          <div className="space-y-3">
            {activeJobs.length > 0 ? (
              activeJobs.map((job) => (
                <EncoderJobCard
                  connected={connected}
                  key={job.job_id}
                  job={job}
                  onApprove={(id) => void approve(id)}
                  onDelete={(id) => void remove(id)}
                  onReprocess={(id) => void reprocess(id)}
                  reprocessing={reprocessingJobIds.has(job.job_id)}
                />
              ))
            ) : (
              <p className="rounded-[14px] border border-white/6 bg-white/[0.02] py-5 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                No files are being processed right now.
              </p>
            )}
          </div>
        </section>

        <section aria-label="Encoder job history" className="mt-6">
          <button
            type="button"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((open) => !open)}
            className={`flex w-full cursor-pointer items-center gap-2 border border-[var(--glass-border)] bg-[var(--glass-bg)] px-4 py-2.5 text-left text-[0.8rem] font-medium text-white/70 backdrop-blur-sm transition hover:border-[var(--accent-5)]/30 hover:text-white/90 ${historyOpen ? 'rounded-t-xl' : 'rounded-xl'}`}
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
                  <EncoderJobCard
                    key={job.job_id}
                    job={job}
                    onApprove={(id) => void approve(id)}
                    onDelete={(id) => void remove(id)}
                    onReprocess={(id) => void reprocess(id)}
                    reprocessing={reprocessingJobIds.has(job.job_id)}
                  />
                ))
              ) : (
                <p className="py-3 text-center text-[0.8rem] text-[var(--text-tertiary)]">
                  Completed and stopped jobs will be listed here.
                </p>
              )}
            </div>
          )}
        </section>
      </PanelLayout>
    </div>
  )
}
