import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import DownloadJobCard from '@/components/downloader/DownloadJobCard'
import type { DownloadJob } from '@/types'

function baseJob(overrides: Partial<DownloadJob> = {}): DownloadJob {
  return {
    job_id: 'job-1',
    url: 'https://example.com/v',
    stage: 'downloading',
    error: null,
    created_at: '2026-08-04 10:00:00',
    updated_at: '2026-08-04 10:00:00',
    items: [
      {
        index: 0,
        title: 'A',
        path: null,
        size: null,
        progress: 40,
        stage: 'downloading',
        error: null,
      },
    ],
    has_transcode: false,
    ...overrides,
  }
}

const noop = vi.fn()

describe('DownloadJobCard stage strip', () => {
  it('shows indeterminate progress before the first download event', () => {
    render(
      <DownloadJobCard
        job={baseJob({ items: [] })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.queryByText('0.0%')).toBeNull()
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull()
    expect(screen.getByText(/Progress unavailable/)).toBeTruthy()
  })

  it('does not average unknown progress as zero', () => {
    const download = baseJob()
    download.items[0]!.progress_known = false
    render(
      <DownloadJobCard
        job={download}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.queryByText('40.0%')).toBeNull()
    expect(screen.getByText(/Progress unavailable/)).toBeTruthy()
  })

  it('omits the Transcoding stage when the job has no re-encode', () => {
    render(
      <DownloadJobCard
        job={baseJob({ has_transcode: false })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.queryByText('Transcoding')).toBeNull()
  })

  it('shows the Transcoding stage when the job itself will re-encode', () => {
    render(
      <DownloadJobCard
        job={baseJob({ has_transcode: true })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.getByText('Transcoding')).toBeTruthy()
  })

  it('renders the trailing FAILED state as a red status pill', () => {
    render(
      <DownloadJobCard
        job={baseJob({
          stage: 'error',
          error: 'Something broke',
        })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    const failedLabel = screen.getByText('Failed')
    // A pill, not the old bare text strip: rounded chip + red tag colours.
    expect(failedLabel.className).toContain('rounded')
    expect(failedLabel.className).toContain('bg-red-400/15')
    expect(failedLabel.className).toContain('text-red-300')
  })

  it('renders the trailing CANCELLED pill distinctly from the error red', () => {
    render(
      <DownloadJobCard
        job={baseJob({ stage: 'cancelled' })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    const cancelledLabel = screen.getByText('Cancelled')
    expect(cancelledLabel.className).toContain('rounded')
    expect(cancelledLabel.className).not.toContain('text-red')
  })

  it('renders the completed state as a pill in the downloader accent', () => {
    render(
      <DownloadJobCard
        job={baseJob({
          stage: 'done',
          items: [
            {
              index: 0,
              title: 'A',
              path: '/a',
              size: 1024,
              progress: 100,
              stage: 'done',
              error: null,
            },
          ],
        })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    const doneLabel = screen.getByText('Done')
    expect(doneLabel.className).toContain('rounded')
    expect(doneLabel.className).toContain('text-cyan-300')
  })

  it('keeps the pipeline stage strip legible (not dimmed tertiary) for a failed job', () => {
    render(
      <DownloadJobCard
        job={baseJob({ stage: 'error', error: 'Something broke' })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    const downloadingLabel = screen.getByText('Downloading')
    expect(downloadingLabel.className).not.toContain('/40')
  })

  it('does not duplicate the Done label for a completed job', () => {
    render(
      <DownloadJobCard
        job={baseJob({
          stage: 'done',
          items: [
            {
              index: 0,
              title: 'A',
              path: '/a',
              size: 1024,
              progress: 100,
              stage: 'done',
              error: null,
            },
          ],
        })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.getAllByText('Done')).toHaveLength(1)
  })
})

describe('DownloadJobCard actions', () => {
  it('exposes every action as a labelled icon button', () => {
    const onCancel = vi.fn()
    const onDelete = vi.fn()
    const onStart = vi.fn()
    render(
      <DownloadJobCard
        job={baseJob({ stage: 'queued' })}
        onCancel={onCancel}
        onDelete={onDelete}
        onStart={onStart}
        onRetry={noop}
      />,
    )
    const start = screen.getByRole('button', { name: 'Start download' })
    const del = screen.getByRole('button', { name: 'Delete job' })
    // Icon-only controls must still carry a tooltip, not just a11y metadata.
    expect(start.getAttribute('title')).toBe('Start download')
    expect(del.getAttribute('title')).toBe('Delete job')
    expect(start.textContent).toBe('')

    start.click()
    expect(onStart).toHaveBeenCalledWith('job-1')
    del.click()
    expect(onDelete).toHaveBeenCalledWith('job-1')
  })

  it('offers cancel while running and retry once failed', () => {
    const onCancel = vi.fn()
    const onRetry = vi.fn()
    const { unmount } = render(
      <DownloadJobCard
        job={baseJob({ stage: 'downloading' })}
        onCancel={onCancel}
        onDelete={noop}
        onStart={noop}
        onRetry={onRetry}
      />,
    )
    screen.getByRole('button', { name: 'Cancel download' }).click()
    expect(onCancel).toHaveBeenCalledWith('job-1')
    expect(screen.queryByRole('button', { name: 'Retry download' })).toBeNull()
    unmount()

    render(
      <DownloadJobCard
        job={baseJob({ stage: 'error', error: 'boom' })}
        onCancel={onCancel}
        onDelete={noop}
        onStart={noop}
        onRetry={onRetry}
      />,
    )
    screen.getByRole('button', { name: 'Retry download' }).click()
    expect(onRetry).toHaveBeenCalledWith('https://example.com/v')
  })
})

describe('DownloadJobCard item errors', () => {
  const item = (overrides: Partial<DownloadJob['items'][number]>) => ({
    index: 0,
    title: 'A',
    path: '/a',
    size: 1024,
    progress: 100,
    stage: 'done' as const,
    error: null,
    ...overrides,
  })

  it('surfaces a per-item message on a single-item job', () => {
    render(
      <DownloadJobCard
        job={baseJob({
          stage: 'done',
          items: [item({ error: 'File already existed and was not re-downloaded.' })],
        })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.getByText(/already existed/)).toBeTruthy()
  })

  it('surfaces per-item messages in a multi-item list', () => {
    render(
      <DownloadJobCard
        job={baseJob({
          stage: 'done',
          items: [
            item({ index: 0, title: 'A', error: 'first item went wrong' }),
            item({ index: 1, title: 'B' }),
            item({ index: 2, title: 'C', error: 'third item went wrong' }),
          ],
        })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(screen.getByText('first item went wrong')).toBeTruthy()
    expect(screen.getByText('third item went wrong')).toBeTruthy()
  })

  it('renders nothing extra when no item carries a message', () => {
    const { container } = render(
      <DownloadJobCard
        job={baseJob({ stage: 'done', items: [item({})] })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    expect(container.querySelectorAll('[data-testid="item-error"]')).toHaveLength(0)
  })
})

describe('DownloadJobCard save link', () => {
  const item = (overrides: Partial<DownloadJob['items'][number]>) => ({
    index: 0,
    title: 'A',
    path: '/downloads/movies/My Video.mp4',
    size: 1024,
    progress: 100,
    stage: 'done' as const,
    error: null,
    ...overrides,
  })

  it('shows a download-arrow link with the filename for a single-item job', () => {
    render(
      <DownloadJobCard
        job={baseJob({ stage: 'done', items: [item({})] })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    const link = screen.getByRole('link', { name: /My Video\.mp4/ })
    expect(link.textContent).toContain('↓')
    expect(link.getAttribute('download')).not.toBeNull()
  })

  it('uses the stored item index for a single-item download link', () => {
    render(
      <DownloadJobCard
        job={baseJob({ stage: 'done', items: [item({ index: 1 })] })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )

    expect(screen.getByRole('link', { name: /My Video\.mp4/ }).getAttribute('href')).toBe(
      '/download/jobs/job-1/items/1/file',
    )
  })

  it('shows a generic download-arrow link per row in a multi-item job, without repeating the title', () => {
    render(
      <DownloadJobCard
        job={baseJob({
          stage: 'done',
          items: [item({ index: 0, title: 'A' }), item({ index: 1, title: 'B' })],
        })}
        onCancel={noop}
        onDelete={noop}
        onStart={noop}
        onRetry={noop}
      />,
    )
    const links = screen.getAllByRole('link', { name: /Save/ })
    expect(links).toHaveLength(2)
    for (const link of links) {
      expect(link.textContent).toContain('↓')
      expect(link.getAttribute('download')).not.toBeNull()
    }
  })
})
