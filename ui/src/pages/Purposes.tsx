import { useState } from 'react'
import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { Failed, Loading, Pill } from '../components/bits'

export default function Purposes() {
  const state = useAsync(() => api.purposes(), [])
  const [open, setOpen] = useState<string | null>(null)

  if (state.error) return <Failed error={state.error} />
  if (state.loading || !state.data) return <Loading what="purposes" />

  return (
    <>
      <div className="page-head">
        <h1>Purposes &amp; notices</h1>
        <span className="muted">one consent record per purpose — never a blanket flag</span>
      </div>

      {state.data.items.length === 0 && <div className="empty">No purposes configured.</div>}

      {state.data.items.map((p) => (
        <div className="panel" key={p.purpose_key}>
          <div style={{ display: 'flex', gap: 12, alignItems: 'baseline', flexWrap: 'wrap' }}>
            <h2 style={{ margin: 0 }}>{p.name}</h2>
            <span className="mono muted">{p.purpose_key}</span>
            <div className="spacer" />
            {p.requires_verification && <Pill kind="plain">verification required</Pill>}
            <span className="muted">{p.active_consents} in force</span>
          </div>

          <dl className="kv" style={{ marginTop: 12 }}>
            <dt>Retention</dt><dd>{p.retention_days} days from the decision</dd>
            <dt>UCM key</dt><dd className="mono">{p.ucm_purpose_key}</dd>
          </dl>

          <div style={{ marginTop: 14 }}>
            <table>
              <thead>
                <tr><th>Language</th><th>Version</th><th>State</th><th>Audio</th><th>Digest</th></tr>
              </thead>
              <tbody>
                {p.notices.map((n) => (
                  <tr key={n.id} onClick={() => setOpen(open === n.id ? null : n.id)}>
                    <td>{n.language}</td>
                    <td className="num">v{n.version}</td>
                    <td>
                      {n.retired ? <Pill kind="plain">retired</Pill>
                        : n.published ? <Pill kind="good">live</Pill>
                        : <Pill kind="plain">draft</Pill>}
                    </td>
                    <td className="muted">{n.has_audio ? 'recorded' : 'text-to-speech'}</td>
                    <td className="mono">{n.sha256}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {p.notices.filter((n) => n.id === open).map((n) => (
              <div className="notice-box" key={n.id} style={{ marginTop: 12 }}>{n.body_text}</div>
            ))}
            <p className="muted" style={{ marginBottom: 0, marginTop: 10 }}>
              Published notices are frozen. A wording change creates a new version, and
              audio needs a new filename — providers cache by URL.
            </p>
          </div>
        </div>
      ))}
    </>
  )
}
