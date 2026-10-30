import { useCallback, useEffect, useState } from 'react'

import { fetchJson } from './api.ts'

export interface Fetched<T> {
  data: T | undefined
  error: string | undefined
  /** True while a request is in flight; `data` still holds the previous answer. */
  loading: boolean
  reload: () => void
}

/**
 * GET `url` as JSON. A refetch keeps the previous data on screen (rendered `.stale`)
 * instead of flashing an empty state; `url === null` fetches nothing.
 */
export function useFetch<T>(url: string | null): Fetched<T> {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(url !== null)
  const [generation, setGeneration] = useState(0)

  useEffect(() => {
    if (url === null) return
    let live = true
    setLoading(true)
    fetchJson<T>(url)
      .then((value) => {
        if (!live) return
        setData(value)
        setError(undefined)
      })
      .catch((reason: unknown) => {
        if (live) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [url, generation])

  const reload = useCallback(() => setGeneration((n) => n + 1), [])
  return { data, error, loading, reload }
}
