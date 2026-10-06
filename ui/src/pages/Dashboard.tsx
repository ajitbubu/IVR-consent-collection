import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { Failed, Loading, Tile } from '../components/bits'

function Bars({ rows }: { rows: { label: string; granted: number; declined: number; withdrawn: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.granted + r.declined + r.withdrawn))
  return (
    <div className="bars">
      {rows.map((r) => {
        const total = r.granted + r.declined + r.withdrawn
        return (
          <div className="bar-row" key={r.label}>
            <span title={r.label} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {r.label}
            </span>
            <div className="bar-track" title={`${r.granted} granted · ${r.declined} declined · ${r.withdrawn} withdrawn`}>
              <div className="bar-seg granted" style={{ width: `${(r.granted / max) * 100}%` }} />
              <div className="bar-seg declined" style={{ width: `${(r.declined / max) * 100}%` }} />
              <div className="bar-seg withdrawn" style={{ width: `${(r.withdrawn / max) * 100}%` }} />
            </div>
            <span className="num muted" style={{ textAlign: 'right' }}>{total}</span>
          </div>
        )
      })}
      <div className="legend">
        <span><i className="swatch" style={{ background: 'var(--granted)' }} />granted</span>
        <span><i className="swatch" style={{ background: 'var(--declined)' }} />declined</span>
        <span><i className="swatch" style={{ background: 'var(--withdrawn)' }} />withdrawn</span>
      </div>
    </div>
  )
}

export default function Dashboard() {
  const stats = useAsync(() => api.stats(30), [])
  const health = useAsync(() => api.health(), [])

  if (stats.error) return <Failed error={stats.error} />
  if (stats.loading || !stats.data) return <Loading what="overview" />

  const s = stats.data
  const h = health.data
  const rate = s.totals.grant_rate

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <span className="muted">last {s.window_days} days</span>
      </div>

      <div className="tiles">
        <Tile label="Consents" value={s.totals.consents} />
        <Tile label="Granted" value={s.totals.granted} />
        <Tile label="Grant rate" value={rate === null ? '—' : `${Math.round(rate * 100)}%`} />
        <Tile label="Withdrawn" value={s.totals.withdrawn} />
      </div>

      <h2>Delivery to UCM</h2>
      <div className="tiles">
        <Tile label="Queued" value={h?.outbox_pending ?? '—'} />
        <Tile label="Lag" value={h ? `${Math.round(h.outbox_lag_seconds)}s` : '—'}
              warn={(h?.outbox_lag_seconds ?? 0) > 3600} />
        <Tile label="Sync failed" value={h?.sync_failed ?? '—'} warn={(h?.sync_failed ?? 0) > 0} />
        <Tile label="Unreconciled calls" value={h?.unreconciled_sessions ?? '—'}
              warn={(h?.unreconciled_sessions ?? 0) > 0} />
        {/* A failed signature check is either a misconfiguration or someone
            forging consent against a public endpoint. Never hide it. */}
        <Tile label="Bad signatures" value={h?.failed_signature_checks ?? '—'}
              warn={(h?.failed_signature_checks ?? 0) > 0} />
      </div>

      <div className="row">
        <div className="panel">
          <h2>By purpose</h2>
          {s.by_purpose.length === 0 ? (
            <div className="empty">No decisions in this window.</div>
          ) : (
            <Bars rows={s.by_purpose.map((p) => ({ label: p.purpose_key, ...p }))} />
          )}
        </div>

        <div className="panel">
          <h2>How calls ended</h2>
          {Object.keys(s.session_outcomes).length === 0 ? (
            <div className="empty">No calls in this window.</div>
          ) : (
            <table>
              <tbody>
                {Object.entries(s.session_outcomes)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, v]) => (
                    <tr key={k} style={{ cursor: 'default' }}>
                      <td>{k.replace(/_/g, ' ')}</td>
                      <td className="num" style={{ textAlign: 'right' }}>{v}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
          <p className="muted" style={{ marginBottom: 0, marginTop: 12 }}>
            Timeouts, hangups and machine answers never become consent.
          </p>
        </div>
      </div>

      <div className="panel">
        <h2>By provider</h2>
        {Object.keys(s.by_provider).length === 0 ? (
          <div className="empty">No decisions in this window.</div>
        ) : (
          <table>
            <thead><tr><th>Provider</th><th>Consents</th><th>Webhook authenticity</th></tr></thead>
            <tbody>
              {Object.entries(s.by_provider).map(([name, count]) => (
                <tr key={name} style={{ cursor: 'default' }}>
                  <td>{name}</td>
                  <td className="num">{count}</td>
                  <td className="muted">
                    {name === 'twilio'
                      ? 'Signed per request; verifiable after the fact'
                      : name === 'sprinklr'
                        ? 'Shared bearer token; not verifiable after the fact'
                        : 'Unsigned; IP allowlist plus call-detail corroboration'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
