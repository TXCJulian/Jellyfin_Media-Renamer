import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import EncoderJobCard from '@/components/encoder/EncoderJobCard'
import {
  formatAudioTracks,
  formatBytes,
  formatDuration,
  formatFactValue,
  formatFrameRate,
} from '@/components/encoder/mediaFacts'
import type { EncoderJob } from '@/types'

const GiB = 1024 ** 3

function job(overrides: Partial<EncoderJob> = {}): EncoderJob {
  return {
    job_id: 'encode-1',
    source_path: '/media/Movies/Demo.mkv',
    stage: 'encoding',
    progress: 42,
    preset_name: 'NVENC',
    rule_id: 'uhd',
    error: null,
    error_code: null,
    remote_job_id: null,
    output_path: null,
    facts: {},
    original_size: null,
    encoded_size: null,
    saved_bytes: null,
    created_at: '2026-08-17T08:00:00Z',
    updated_at: '2026-08-17T08:01:00Z',
    ...overrides,
  }
}

describe('EncoderJobCard', () => {
  it('updates remaining time and clears it when encoding ends', () => {
    const { rerender } = render(
      <EncoderJobCard job={job({ eta_seconds: 720 })} onApprove={vi.fn()} onDelete={vi.fn()} />,
    )
    expect(screen.getByText(/About 12 min remaining/)).toBeTruthy()
    rerender(
      <EncoderJobCard job={job({ eta_seconds: null })} onApprove={vi.fn()} onDelete={vi.fn()} />,
    )
    expect(screen.getByText(/Estimating/)).toBeTruthy()
    expect(screen.queryByText(/remaining/)).toBeNull()
    rerender(
      <EncoderJobCard
        job={job({ stage: 'swapping', eta_seconds: 720 })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    expect(screen.queryByText(/remaining|Estimating/)).toBeNull()
  })

  it('hides the estimate when live updates disconnect', () => {
    render(
      <EncoderJobCard
        job={job({ eta_seconds: 720 })}
        connected={false}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
      />,
    )
    expect(screen.queryByText(/remaining/)).toBeNull()
  })

  it('renders completed savings and expandable media facts', async () => {
    render(
      <EncoderJobCard
        job={job({
          stage: 'done',
          progress: 100,
          original_size: 20 * GiB,
          encoded_size: 6 * GiB,
          saved_bytes: 14 * GiB,
          facts: { height: 2160, hdr: true },
        })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    expect(screen.getByText(/20\.0 GiB → 6\.0 GiB · 70% smaller/)).toBeTruthy()
    expect(screen.queryByText(/2160p/)).toBeNull()
    const details = screen.getByRole('button', { name: 'Show media details' })
    expect(details.querySelector('svg')).toBeTruthy()
    expect(details.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(details)
    expect(screen.getByText(/2160p/)).toBeTruthy()
    expect(screen.getByText(/HDR/)).toBeTruthy()
    expect(
      screen.getByRole('button', { name: 'Hide media details' }).getAttribute('aria-expanded'),
    ).toBe('true')
  })

  it('shows the four-stage pipeline and a real encoding progress bar', () => {
    render(<EncoderJobCard job={job()} onApprove={vi.fn()} onDelete={vi.fn()} />)

    for (const label of ['Settling', 'Encoding', 'Swapping', 'Done']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(screen.getByText('Encoding').className).not.toContain('rounded')
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('42')
  })

  it('shows Waiting in the pipeline while a job is queued and awaiting encoding', () => {
    render(
      <EncoderJobCard
        job={job({ stage: 'queued', progress: 0 })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    for (const label of ['Settling', 'Waiting', 'Swapping', 'Done']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(screen.queryByText('Encoding')).toBeNull()
    expect(screen.getByText('Waiting').className).toContain('text-teal-300')
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('renders a terminal outcome as a status pill without duplicating Done', () => {
    render(
      <EncoderJobCard
        job={job({ stage: 'done', progress: 100 })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    expect(screen.getAllByText('Done')).toHaveLength(1)
    expect(screen.getByText('Done').className).toContain('rounded')
  })

  it.each(['pending', 'blocked'] as const)(
    'offers approval, but not deletion, while %s',
    (stage) => {
      const onApprove = vi.fn()
      const onDelete = vi.fn()
      render(<EncoderJobCard job={job({ stage })} onApprove={onApprove} onDelete={onDelete} />)

      screen.getByRole('button', { name: 'Approve encoding' }).click()
      expect(onApprove).toHaveBeenCalledWith('encode-1')
      expect(screen.queryByRole('button', { name: 'Delete job' })).toBeNull()
    },
  )

  it('offers deletion instead of rejected approval after an interrupted swap', () => {
    const onApprove = vi.fn()
    const onDelete = vi.fn()
    render(
      <EncoderJobCard
        job={job({ stage: 'blocked', error_code: 'swap_interrupted' })}
        onApprove={onApprove}
        onDelete={onDelete}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Approve encoding' })).toBeNull()
    screen.getByRole('button', { name: 'Delete job' }).click()
    expect(onDelete).toHaveBeenCalledWith('encode-1')
    expect(onApprove).not.toHaveBeenCalled()
  })

  it('offers deletion for non-review jobs and surfaces backend errors', () => {
    const onDelete = vi.fn()
    render(
      <EncoderJobCard
        job={job({ stage: 'failed', error: 'HandBrake exited 1' })}
        onApprove={vi.fn()}
        onDelete={onDelete}
      />,
    )

    expect(screen.getByText('Failed').className).toContain('text-red')
    expect(screen.getByText('HandBrake exited 1')).toBeTruthy()
    screen.getByRole('button', { name: 'Delete job' }).click()
    expect(onDelete).toHaveBeenCalledWith('encode-1')
    expect(screen.queryByRole('button', { name: 'Approve encoding' })).toBeNull()
  })

  it('offers re-evaluation for a failed job', () => {
    const onReprocess = vi.fn()
    render(
      <EncoderJobCard
        job={job({ stage: 'failed' })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
        onReprocess={onReprocess}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Re-evaluate file' }))

    expect(onReprocess).toHaveBeenCalledWith('encode-1')
  })

  it('offers re-evaluation for a recoverable blocked job', () => {
    const onReprocess = vi.fn()
    render(
      <EncoderJobCard
        job={job({ stage: 'blocked', error_code: 'offline' })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
        onReprocess={onReprocess}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Re-evaluate file' }))

    expect(onReprocess).toHaveBeenCalledWith('encode-1')
    expect(screen.getByRole('button', { name: 'Approve encoding' })).toBeTruthy()
  })

  it('does not offer unsafe recovery after an interrupted swap', () => {
    render(
      <EncoderJobCard
        job={job({ stage: 'blocked', error_code: 'swap_interrupted' })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
        onReprocess={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Re-evaluate file' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Approve encoding' })).toBeNull()
  })

  it('does not offer re-evaluation while a job is swapping', () => {
    render(
      <EncoderJobCard
        job={job({ stage: 'swapping' })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
        onReprocess={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Re-evaluate file' })).toBeNull()
  })

  it('does not offer re-evaluation for blocked jobs with active remote work', () => {
    render(
      <EncoderJobCard
        job={job({ stage: 'blocked', remote_job_id: 'remote-123' })}
        onApprove={vi.fn()}
        onDelete={vi.fn()}
        onReprocess={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Re-evaluate file' })).toBeNull()
  })

  it('formats the common media facts for readable diagnostics', () => {
    expect(formatDuration(6565.183)).toBe('1:49:25')
    expect(formatFrameRate(23.976023978)).toBe('23.976 fps')
    expect(formatAudioTracks([{ codec: 'aac', channels: 6, language: 'eng' }])).toContain('aac')
    expect(
      formatAudioTracks([
        { codec: 'aac', channels: 2, language: 'eng' },
        { codec: 'ac3', channels: 6, language: 'deu' },
        { codec: 'flac', channels: 4, language: 'und' },
      ]),
    ).toBe('aac · stereo · eng\nac3 · 5.1 · deu\nflac · 4.0 · und')
    expect(formatFactValue('metadata', { title: 'Demo' }, {})).toBe('metadata: {"title":"Demo"}')
    expect(formatFactValue('width', 1920, { height: 1080 })).toBeNull()
    expect(formatFactValue('width', 1920, { height: 0 })).toBe('1920px wide')
    expect(formatBytes(-500)).toBe('-500')
  })

  it('formats malformed object-valued numeric facts as compact JSON', () => {
    expect(formatDuration({ seconds: 6565 })).toBe('{"seconds":6565}')
    expect(formatFrameRate({ numerator: 24000, denominator: 1001 })).toBe(
      '{"numerator":24000,"denominator":1001}',
    )
  })
})
