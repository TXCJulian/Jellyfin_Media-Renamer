import { useEffect, useRef } from 'react'

export function useDirectoryAutoRefresh(
  refresh: () => void | PromiseLike<unknown>,
  intervalMs = 30_000,
): void {
  const latestRefresh = useRef(refresh)
  const refreshInFlight = useRef(false)

  useEffect(() => {
    latestRefresh.current = refresh
  }, [refresh])

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (refreshInFlight.current) return
      refreshInFlight.current = true
      let result: void | PromiseLike<unknown>
      try {
        result = latestRefresh.current()
      } catch {
        refreshInFlight.current = false
        return
      }
      Promise.resolve(result)
        .catch(() => {})
        .finally(() => {
          refreshInFlight.current = false
        })
    }, intervalMs)
    return () => window.clearInterval(interval)
  }, [intervalMs])
}
