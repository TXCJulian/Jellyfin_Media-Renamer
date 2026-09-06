import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useDirectoryAutoRefresh } from '@/hooks/useDirectoryAutoRefresh'

afterEach(() => {
  vi.useRealTimers()
})

describe('useDirectoryAutoRefresh', () => {
  it('calls the latest callback every 30 seconds without firing on mount', () => {
    vi.useFakeTimers()
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = renderHook(
      ({ refresh }) => useDirectoryAutoRefresh(refresh),
      { initialProps: { refresh: first } },
    )

    expect(first).not.toHaveBeenCalled()
    rerender({ refresh: second })
    vi.advanceTimersByTime(30_000)

    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('clears the interval on unmount', () => {
    vi.useFakeTimers()
    const refresh = vi.fn()
    const { unmount } = renderHook(() => useDirectoryAutoRefresh(refresh))

    unmount()
    vi.advanceTimersByTime(60_000)

    expect(refresh).not.toHaveBeenCalled()
  })

  it('does not overlap ticks while an async refresh is pending', async () => {
    vi.useFakeTimers()
    let resolveRefresh!: () => void
    const refresh = vi.fn(() => new Promise<void>((resolve) => { resolveRefresh = resolve }))
    renderHook(() => useDirectoryAutoRefresh(refresh))

    vi.advanceTimersByTime(30_000)
    vi.advanceTimersByTime(30_000)
    expect(refresh).toHaveBeenCalledTimes(1)
    resolveRefresh()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('catches synchronous throws and rejected refresh promises', async () => {
    vi.useFakeTimers()
    const syncThrow = vi.fn<() => void | PromiseLike<unknown>>(() => {
      throw new Error('sync')
    })
    const { rerender } = renderHook(
      ({ refresh }) => useDirectoryAutoRefresh(refresh),
      { initialProps: { refresh: syncThrow } },
    )
    vi.advanceTimersByTime(30_000)
    rerender({ refresh: vi.fn(() => Promise.reject(new Error('async'))) })
    vi.advanceTimersByTime(30_000)
    await Promise.resolve()
  })
})
