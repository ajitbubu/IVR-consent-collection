import { useState } from 'react'
import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { Failed, Loading, Pill, ProviderPill, when } from '../components/bits'

export default function Sessions() {
  const [provider, setProvider] = useState('')
  const [outcome, setOutcome] = useState('')
  const state = useAsync(() => api.sessions({ provider, outcome, limit: 100 }), [provider, outcome])

  return (
    <>
      <div className="page-head">
        <h1>Calls</h1>
        <span className="muted">every attempt, including the ones that produced nothing</span>
      </div>

      <div className="filters">
        <select value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="Provider">
          <option value="">Any provider</option>
          <option value="exotel">Exotel</option>
          <option value="twilio">Twilio</option>
        </select>
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Outcome">
          <option value="">Any outcome</option>
          <option value="decision_granted">Granted</option>
          <option value="decision_declined">Declined</option>
          <option value="decision_withdrawn">Withdrawn</option>
          <option value="no_input">No input</option>
          <option value="invalid_key">Invalid key</option>
          <option value="answering_machine">Answering machine</option>
          <option value="in_progress">In progress</option>
        </select>
      </div>

      {state.error && <Failed error={state.error} />}
      {state.loading && !state.data && <Loading what="calls" />}

      {state.data && (
        <div className="panel" style={{ padding: '16px 6px 6px' }}>
          {state.data.items.length === 0 ? (
            <div className="empty">No calls match.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Started</th><th>Call reference</th><th>Provider</th>
                  <th>Direction</th><th>Purpose</th><th>Answered by</th>
                  <th>Outcome</th><th>Corroborated</th>
                </tr>
              </thead>
              <tbody>
                {state.data.items.map((s) => (
                  <tr key={s.session_id} style={{ cursor: 'default' }}>
                    <td className="muted">{when(s.started_at)}</td>
                    <td className="mono">{s.call_sid ?? '—'}</td>
                    <td><ProviderPill provider={s.provider} /></td>
                    <td className="muted">{s.direction.replace('ivr_', '')}</td>
                    <td>{s.purpose_key ?? '—'}</td>
                    <td className="muted">{s.answered_by ?? '—'}</td>
                    <td>{s.outcome.replace(/_/g, ' ')}</td>
                    <td>{s.reconciled ? <Pill kind="good">yes</Pill> : <Pill kind="plain">pending</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </>
  )
}
