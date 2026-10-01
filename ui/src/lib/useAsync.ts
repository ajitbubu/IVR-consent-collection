import { useCallback, useEffect, useState } from 'react'

export interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
}

/** Minimal fetch-on-mount hook. Deliberately not a data library: the
 *  console has a handful of read endpoints and no cache invalidation
 *  problem worth a dependency. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fn, deps)

  useEffect(() => {
    let live = true
    setLoading(true)
    run()
      .then((d) => { if (live) { setData(d); setError(null) } })
      .catch((e: Error) => { if (live) setError(e.message) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [run, nonce])

  return { data, error, loading, reload: () => setNonce((n) => n + 1) }
}
