import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { DecisionPill, Failed, Loading, ProviderPill, SyncPill, when } from '../components/bits'

const PAGE = 50

export default function Consents() {
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [pending, setPending] = useState('')
  const [decision, setDecision] = useState('')
  const [provider, setProvider] = useState('')
  const [sync, setSync] = useState('')
  const [currentOnly, setCurrentOnly] = useState(true)
  const [offset, setOffset] = useState(0)

  const state = useAsync(
    () => api.consents({ q, decision, provider, sync, current_only: currentOnly, limit: PAGE, offset }),
    [q, decision, provider, sync, currentOnly, offset],
  )

  function search(e: React.FormEvent) {
    e.preventDefault()
    setOffset(0)
    setQ(pending)
  }

  return (
    <>
      <div className="page-head">
        <h1>Consents</h1>
        {state.data && <span className="muted">{state.data.total} matching</span>}
      </div>

      <form className="filters" onSubmit={search}>
        <input
          type="search" value={pending} placeholder="Phone number or consent id"
          onChange={(e) => setPending(e.target.value)}
          aria-label="Search by phone number or consent id"
        />
        <button type="submit" className="primary">Search</button>

        <select value={decision} onChange={(e) => { setOffset(0); setDecision(e.target.value) }} aria-label="Decision">
          <option value="">Any decision</option>
          <option value="granted">Granted</option>
          <option value="declined">Declined</option>
          <option value="withdrawn">Withdrawn</option>
        </select>

        <select value={provider} onChange={(e) => { setOffset(0); setProvider(e.target.value) }} aria-label="Provider">
          <option value="">Any provider</option>
          <option value="exotel">Exotel</option>
          <option value="twilio">Twilio</option>
        </select>

        <select value={sync} onChange={(e) => { setOffset(0); setSync(e.target.value) }} aria-label="UCM sync state">
          <option value="">Any sync state</option>
          <option value="pending">Pending</option>
          <option value="synced">Synced</option>
          <option value="failed">Failed</option>
        </select>

        <label className="muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={currentOnly} style={{ minWidth: 0 }}
                 onChange={(e) => { setOffset(0); setCurrentOnly(e.target.checked) }} />
          Current only
        </label>
      </form>

      {state.error && <Failed error={state.error} />}
      {state.loading && !state.data && <Loading what="consents" />}

      {state.data && (
        <div className="panel table-panel">
          {state.data.items.length === 0 ? (
            <div className="empty">Nothing matches those filters.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Decided</th><th>Phone</th><th>Purpose</th><th>Decision</th>
                  <th>Provider</th><th>Verification</th><th>UCM</th>
                </tr>
              </thead>
              <tbody>
                {state.data.items.map((c) => (
                  <tr key={c.consent_id} onClick={() => nav(`/consents/${c.consent_id}`)}>
                    <td className="muted">
                      <Link className="row-link" to={`/consents/${c.consent_id}`}>{when(c.decided_at)}</Link>
                    </td>
                    <td className="mono">{c.phone_masked}</td>
                    <td>{c.purpose_key}{!c.is_current && <span className="muted"> · superseded</span>}</td>
                    <td><DecisionPill decision={c.decision} /></td>
                    <td><ProviderPill provider={c.provider} /></td>
                    <td className="muted">{c.verification_level === 'verified' ? 'verified' : 'ANI only'}</td>
                    <td><SyncPill state={c.ucm_sync_state} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {state.data.total > PAGE && (
            <div className="filters" style={{ padding: '12px 16px', margin: 0, borderTop: '1px solid var(--line)' }}>
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Previous
              </button>
              <span className="muted">
                {offset + 1}–{Math.min(offset + PAGE, state.data.total)} of {state.data.total}
              </span>
              <button disabled={offset + PAGE >= state.data.total} onClick={() => setOffset(offset + PAGE)}>
                Next
              </button>
            </div>
          )}
        </div>
      )}
    </>
  )
}
