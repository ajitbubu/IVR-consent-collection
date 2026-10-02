import type { ReactNode } from 'react'
import type { Decision } from '../lib/api'

export function Pill({ kind, children }: { kind: string; children: ReactNode }) {
  return <span className={`pill ${kind}`}>{children}</span>
}

export function DecisionPill({ decision }: { decision: Decision }) {
  return <Pill kind={decision}>{decision}</Pill>
}

export function SyncPill({ state }: { state: string }) {
  const kind = state === 'synced' ? 'good' : state === 'failed' ? 'bad' : 'plain'
  return <Pill kind={kind}>{state}</Pill>
}

/** Twilio signs every webhook; Exotel signs none. Showing which provider
 *  carried a consent is not decoration -- it tells an operator how much the
 *  transport itself proves. */
export function ProviderPill({ provider }: { provider: string }) {
  return <Pill kind="provider">{provider}</Pill>
}

export function when(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

export function Tile({ label, value, warn }: { label: string; value: ReactNode; warn?: boolean }) {
  return (
    <div className="tile">
      <div className="k">{label}</div>
      <div className={`v${warn ? ' warn' : ''}`}>{value}</div>
    </div>
  )
}

export function Loading({ what }: { what: string }) {
  return <div className="empty">Loading {what}…</div>
}

export function Failed({ error }: { error: string }) {
  return (
    <div className="panel">
      <p className="error">Could not load: {error}</p>
      <p className="muted">Check that the consent service is reachable.</p>
    </div>
  )
}
