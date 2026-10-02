import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { DecisionPill, Failed, Loading, Pill, ProviderPill, SyncPill, when } from '../components/bits'

export default function ConsentDetail() {
  const { id = '' } = useParams()
  const [showEvidence, setShowEvidence] = useState(false)
  const detail = useAsync(() => api.consent(id), [id])
  const evidence = useAsync(
    () => (showEvidence ? api.evidence(id) : Promise.resolve(null)),
    [id, showEvidence],
  )

  if (detail.error) return <Failed error={detail.error} />
  if (detail.loading || !detail.data) return <Loading what="record" />

  const d = detail.data
  const c = d.consent
  const signed = d.webhook_receipts.filter((r) => r.signature_ok === true).length
  const rejected = d.webhook_receipts.filter((r) => r.signature_ok === false).length

  return (
    <>
      <div className="page-head">
        <Link className="back" to="/consents">&larr; Consents</Link>
        <h1>{d.purpose.name}</h1>
        <DecisionPill decision={c.decision} />
        {!c.is_current && <Pill kind="plain">superseded</Pill>}
        {c.permits_processing && <Pill kind="good">in force</Pill>}
        <div className="spacer" />
        <Link className="button-link" to={`/consents/${c.consent_id}/receipt`}>Consent receipt</Link>
      </div>

      <div className="row">
        <div className="panel">
          <h2>Record</h2>
          <dl className="kv">
            <dt>Consent id</dt><dd className="mono">{c.consent_id}</dd>
            <dt>Data Principal</dt><dd className="mono">{d.data_principal.phone_e164}</dd>
            {d.data_principal.external_ref && (<><dt>CRM reference</dt><dd className="mono">{d.data_principal.external_ref}</dd></>)}
            <dt>Purpose</dt><dd>{d.purpose.code}</dd>
            <dt>Decided</dt><dd>{when(c.decided_at)}</dd>
            <dt>Expires</dt><dd>{when(c.expires_at)}</dd>
            <dt>Channel</dt><dd>{c.channel} &middot; <ProviderPill provider={c.provider} /></dd>
            <dt>Verification</dt>
            <dd>
              {c.verification_level === 'verified' ? 'verified' : 'ANI only'}
              {c.verification_level === 'ani_only' && (
                <span className="muted"> — proves the handset, not the person</span>
              )}
            </dd>
            <dt>UCM</dt>
            <dd>
              <SyncPill state={c.ucm_sync_state} />
              {c.ucm_consent_ref && <span className="mono"> {c.ucm_consent_ref}</span>}
            </dd>
            {c.superseded_by && (
              <><dt>Superseded by</dt>
                <dd><Link className="mono" to={`/consents/${c.superseded_by}`}>{c.superseded_by}</Link></dd></>
            )}
          </dl>
        </div>

        <div className="panel">
          <h2>The call</h2>
          {d.call === null ? (
            <p className="muted">Not captured on a call.</p>
          ) : (
            <dl className="kv">
              <dt>Call reference</dt><dd className="mono">{d.call.call_sid ?? '—'}</dd>
              <dt>Direction</dt><dd>{d.call.direction}</dd>
              <dt>Started</dt><dd>{when(d.call.started_at)}</dd>
              <dt>Ended</dt><dd>{when(d.call.ended_at)}</dd>
              <dt>Answered by</dt><dd>{d.call.answered_by ?? '—'}</dd>
              <dt>Outcome</dt><dd>{d.call.outcome.replace(/_/g, ' ')}</dd>
              <dt>Corroborated</dt>
              <dd>
                {d.call.reconciled_at
                  ? <Pill kind="good">yes</Pill>
                  : <Pill kind="bad">not yet</Pill>}
                <span className="muted"> against the provider&rsquo;s call records</span>
              </dd>
            </dl>
          )}
        </div>
      </div>

      <div className="panel">
        <h2>What they were told</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Notice version {d.notice.version} &middot; {d.notice.language} &middot;{' '}
          <span className="mono">{d.notice.sha256.slice(0, 16)}…</span>
          {d.notice.audio_url ? ' · played from a recording' : ' · read by text-to-speech'}
        </p>
        <div className="notice-box">{d.notice.body_text}</div>
      </div>

      <div className="row">
        <div className="panel">
          <h2>Webhooks received</h2>
          {d.webhook_receipts.length === 0 ? (
            <p className="muted">None recorded.</p>
          ) : (
            <>
              <table>
                <thead><tr><th>Route</th><th>Received</th><th>Signature</th></tr></thead>
                <tbody>
                  {d.webhook_receipts.map((r, i) => (
                    <tr key={i} style={{ cursor: 'default' }}>
                      <td>{r.route}</td>
                      <td className="muted">{when(r.received_at)}</td>
                      <td>
                        {r.signature_ok === true && <Pill kind="good">verified</Pill>}
                        {r.signature_ok === false && <Pill kind="bad">rejected</Pill>}
                        {r.signature_ok === null && <Pill kind="plain">unsigned</Pill>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted" style={{ marginBottom: 0 }}>
                {c.provider === 'twilio'
                  ? `${signed} verified, ${rejected} rejected. The signature is kept, so this consent stays re-verifiable.`
                  : 'Exotel does not sign its webhooks, so authenticity rests on the IP allowlist and on corroborating the call.'}
              </p>
            </>
          )}
        </div>

        <div className="panel">
          <h2>History for this purpose</h2>
          <table>
            <thead><tr><th>When</th><th>Decision</th><th>Channel</th></tr></thead>
            <tbody>
              {d.history.map((h) => (
                <tr key={h.consent_id} style={{ cursor: 'default' }}
                    className={h.consent_id === c.consent_id ? 'is-current' : undefined}
                    aria-current={h.consent_id === c.consent_id ? 'true' : undefined}>
                  <td className="muted">
                    {h.consent_id === c.consent_id
                      ? when(h.decided_at)
                      : <Link className="row-link" to={`/consents/${h.consent_id}`}>{when(h.decided_at)}</Link>}
                  </td>
                  <td><DecisionPill decision={h.decision} /></td>
                  <td className="muted">{h.channel} &middot; {h.provider}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ marginBottom: 0 }}>
            A withdrawal never erases the grant it replaced — proving a consent was valid
            while it was relied on is exactly what gets asked for.
          </p>
        </div>
      </div>

      <div className="panel">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>Evidence</h2>
          <div className="spacer" />
          <button onClick={() => setShowEvidence((v) => !v)}>
            {showEvidence ? 'Hide' : 'Open evidence bundle'}
          </button>
        </div>

        {!showEvidence && (
          <p className="muted" style={{ margin: 0 }}>
            Opening the bundle is recorded against your account, with the reason you gave.
          </p>
        )}

        {showEvidence && evidence.loading && <Loading what="evidence" />}
        {showEvidence && evidence.error && <p className="error">{evidence.error}</p>}
        {showEvidence && evidence.data && (
          <>
            <p style={{ marginTop: 0 }}>
              {evidence.data.chain.verified
                ? <Pill kind="good">audit chain verified</Pill>
                : <Pill kind="bad">chain broken at #{evidence.data.chain.broken_at_seq}</Pill>}
              {' '}
              <span className="muted">
                head <span className="mono">{evidence.data.chain.head?.slice(0, 16)}…</span>
              </span>
            </p>

            <ol className="chain">
              {evidence.data.chain.events.map((e) => (
                <li key={e.seq}>
                  <span className="when">{when(e.occurred_at)}</span>
                  <span className="what">{e.event_type}</span>
                  <span className="hash">{e.entry_hash.slice(0, 12)}</span>
                </li>
              ))}
            </ol>

            <h2 style={{ marginTop: 18 }}>Recording</h2>
            {evidence.data.artifacts.length === 0 ? (
              <p className="muted">No recording captured for this call.</p>
            ) : (
              <table>
                <thead><tr><th>Kind</th><th>Stored</th><th>SHA-256</th><th>Size</th></tr></thead>
                <tbody>
                  {evidence.data.artifacts.map((a, i) => (
                    <tr key={i} style={{ cursor: 'default' }}>
                      <td>{a.kind}</td>
                      <td className="mono">
                        {a.purged_at ? <span className="muted">purged {when(a.purged_at)}</span>
                                     : a.storage_uri ?? <span className="muted">awaiting fetch</span>}
                      </td>
                      <td className="mono">{a.sha256 ? `${a.sha256.slice(0, 16)}…` : '—'}</td>
                      <td className="num">{a.bytes ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="muted" style={{ marginBottom: 0 }}>
              After the audio is purged the hash remains, so the record still shows a
              recording existed and what it hashed to.
            </p>
          </>
        )}
      </div>
    </>
  )
}
