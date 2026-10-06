// Typed client for the consent service. One place that knows about
// transport, so every page just awaits a shape.

const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  }
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ''}`
  const resp = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail ?? detail
    } catch { /* body was not json */ }
    throw new ApiError(detail, resp.status)
  }
  return resp.json() as Promise<T>
}

// ---------------------------------------------------------------- types

export type Decision = 'granted' | 'declined' | 'withdrawn'
export type Provider = 'exotel' | 'twilio' | 'sprinklr'

export interface ConsentRow {
  consent_id: string
  phone_masked: string
  data_principal_id: string
  purpose_key: string
  purpose_name: string
  decision: Decision
  status: string
  permits_processing: boolean
  is_current: boolean
  provider: Provider
  channel: string
  language: string
  verification_level: 'ani_only' | 'verified'
  decided_at: string
  expires_at: string | null
  ucm_sync_state: 'pending' | 'synced' | 'failed' | 'skipped'
}

export interface ConsentPage {
  total: number
  limit: number
  offset: number
  items: ConsentRow[]
}

export interface ConsentDetail {
  consent: ConsentRow & { superseded_by: string | null; ucm_consent_ref: string | null }
  data_principal: { id: string; phone_e164: string; external_ref: string | null }
  purpose: { code: string; name: string; retention_days: number; requires_verification: boolean }
  notice: { id: string; version: number; language: string; body_text: string; sha256: string; audio_url: string | null }
  call: null | {
    session_id: string; call_sid: string | null; provider: Provider; direction: string
    outcome: string; answered_by: string | null; started_at: string
    ended_at: string | null; reconciled_at: string | null
  }
  webhook_receipts: { route: string; provider: Provider; signature_ok: boolean | null; has_signature: boolean; received_at: string }[]
  outbox: null | { attempts: number; paused: boolean; last_error: string | null; delivered_at: string | null; next_attempt_at: string }
  history: { consent_id: string; decision: Decision; status: string; decided_at: string; channel: string; provider: Provider; is_current: boolean }[]
}

export interface EvidenceBundle {
  consent: Record<string, unknown>
  notice: { body_text: string; sha256: string; version: number; language: string; audio_url: string | null }
  call: Record<string, unknown> | null
  artifacts: { kind: string; storage_uri: string | null; sha256: string | null; bytes: number | null; purged_at: string | null }[]
  chain: {
    verified: boolean
    broken_at_seq: string | null
    head: string | null
    events: { seq: number; event_type: string; occurred_at: string; payload: Record<string, unknown>; prev_hash: string | null; entry_hash: string }[]
  }
}

export interface Stats {
  window_days: number
  totals: { consents: number; granted: number; declined: number; withdrawn: number; grant_rate: number | null }
  by_provider: Record<string, number>
  by_purpose: { purpose_key: string; granted: number; declined: number; withdrawn: number }[]
  daily: { day: string; granted: number; declined: number; withdrawn: number }[]
  session_outcomes: Record<string, number>
  providers_available: string[]
}

export interface Health {
  outbox_pending: number
  outbox_paused: number
  sync_failed: number
  unreconciled_sessions: number
  failed_signature_checks: number
  outbox_lag_seconds: number
  status: 'ok' | 'attention'
}

export interface PurposeRow {
  purpose_key: string
  name: string
  retention_days: number
  requires_verification: boolean
  ucm_purpose_key: string
  active_consents: number
  notices: { id: string; version: number; language: string; published: boolean; retired: boolean; sha256: string; has_audio: boolean; body_text: string }[]
}

export interface SessionRow {
  session_id: string; call_sid: string | null; provider: Provider
  direction: string; outcome: string; purpose_key: string | null
  answered_by: string | null; started_at: string; reconciled: boolean
}

// ---------------------------------------------------------------- calls

export const api = {
  consents: (p: Record<string, unknown>) => get<ConsentPage>('/api/console/consents', p),
  consent: (id: string) => get<ConsentDetail>(`/api/console/consents/${id}`),
  evidence: (id: string) => get<EvidenceBundle>(`/v1/consents/${id}/evidence`),
  stats: (days = 30) => get<Stats>('/api/console/stats', { days }),
  health: () => get<Health>('/api/console/health'),
  purposes: () => get<{ items: PurposeRow[] }>('/api/console/purposes'),
  sessions: (p: Record<string, unknown> = {}) => get<{ items: SessionRow[] }>('/api/console/sessions', p),
}
