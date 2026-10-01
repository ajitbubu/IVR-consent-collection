import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import type { ConsentDetail } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { Failed, Loading } from '../components/bits'

const ISSUER = import.meta.env.VITE_RECEIPT_ISSUER || 'FaceOff Privacy'

function stamp(iso: string): string {
  const d = new Date(iso)
  const date = d.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' })
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' })
  return `${date} · ${time}`
}

type Check = { state: 'ok' | 'pending' | 'bad'; text: string }

/** Each line states what the record actually proves. A check that has not
 *  happened yet is shown as pending, never as passed. */
function checks(d: ConsentDetail): Check[] {
  const c = d.consent
  const out: Check[] = []

  if (d.call === null) {
    out.push({ state: 'pending', text: 'Not captured on a call' })
  } else {
    out.push(d.call.outcome.startsWith('decision_')
      ? { state: 'ok', text: 'Keypress captured on the call' }
      : { state: 'bad', text: `Call outcome: ${d.call.outcome.replace(/_/g, ' ')}` })

    if (c.provider === 'twilio') {
      const rejected = d.webhook_receipts.filter((r) => r.signature_ok === false).length
      out.push(rejected === 0 && d.webhook_receipts.length > 0
        ? { state: 'ok', text: 'Provider signatures verified' }
        : { state: 'bad', text: `${rejected} webhook signature${rejected === 1 ? '' : 's'} rejected` })
    } else {
      out.push({ state: 'pending', text: 'Unsigned transport (Exotel does not sign webhooks)' })
    }

    out.push(d.call.reconciled_at
      ? { state: 'ok', text: 'Corroborated against provider call records' }
      : { state: 'pending', text: 'Not yet corroborated against provider call records' })
  }

  out.push(c.ucm_sync_state === 'synced'
    ? { state: 'ok', text: 'Delivered to consent manager' }
    : c.ucm_sync_state === 'failed'
      ? { state: 'bad', text: 'Delivery to consent manager failed' }
      : { state: 'pending', text: 'Awaiting delivery to consent manager' })

  return out
}

const MARK = { ok: '✓', pending: '○', bad: '✗' }

export default function Receipt() {
  const { id = '' } = useParams()
  const detail = useAsync(() => api.consent(id), [id])
  const evidence = useAsync(() => api.evidence(id), [id])

  if (detail.error) return <main className="receipt"><Failed error={detail.error} /></main>
  if (detail.loading || !detail.data) return <main className="receipt"><Loading what="receipt" /></main>

  const d = detail.data
  const c = d.consent
  const purpose = d.purpose.name.charAt(0).toUpperCase() + d.purpose.name.slice(1)
  const status = c.decision === c.status ? c.decision : `${c.decision}, now ${c.status}`
  const ev = evidence.data
  const chain: Check = evidence.error
    ? { state: 'bad', text: `Audit chain could not be checked: ${evidence.error}` }
    : ev === null
      ? { state: 'pending', text: 'Checking audit chain…' }
      : ev.chain.verified
        ? { state: 'ok', text: 'Event integrity sealed' }
        : { state: 'bad', text: `Audit chain broken at event #${ev.chain.broken_at_seq}` }

  return (
    <main className="receipt">
      <div className="receipt-tools">
        <Link className="back" to={`/consents/${c.consent_id}`}>&larr; Consent record</Link>
        <div className="spacer" />
        <button onClick={() => window.print()}>Print or save as PDF</button>
      </div>

      {!c.is_current && (
        <p className="receipt-note">
          This decision has been superseded.{' '}
          {c.superseded_by && <Link to={`/consents/${c.superseded_by}/receipt`}>View the current receipt</Link>}
        </p>
      )}

      <p className="receipt-issuer">{ISSUER} &middot; IVR consent capture</p>
      <h1>DPDP Consent Receipt</h1>

      <div className="receipt-id">
        <strong>Receipt ID: <span className="mono">{c.consent_id}</span></strong>
        <small>
          Created {stamp(c.decided_at)} &middot; {c.channel === 'ivr_inbound' ? 'Inbound call' : 'Outbound call'} via {c.provider === 'twilio' ? 'Twilio' : 'Exotel'}
        </small>
      </div>

      <h2>Data Principal</h2>
      <p>
        Mobile ending {d.data_principal.phone_e164.slice(-4)} &middot;{' '}
        {c.verification_level === 'verified' ? 'Identity verified' : 'Caller ID only · proves the handset, not the person'}
      </p>

      <h2>Notice evidence</h2>
      <p>
        Notice v{d.notice.version} &middot; {d.notice.language} &middot;{' '}
        {d.notice.audio_url ? 'Recorded audio' : 'Text-to-speech'} played before the keypress
        <br />
        <small>SHA-256 <span className="mono">{d.notice.sha256.slice(0, 32)}…</span></small>
      </p>

      <h2>Active purposes</h2>
      {c.permits_processing ? (
        <ul>
          <li>
            {purpose}
            {c.expires_at && <small> &middot; valid until {new Date(c.expires_at).toLocaleDateString()}</small>}
          </li>
        </ul>
      ) : <p><small>None on this receipt.</small></p>}

      <h2>Refused or withdrawn purposes</h2>
      {!c.permits_processing ? (
        <ul><li>{purpose} <small>&middot; {status}</small></li></ul>
      ) : <p><small>None on this receipt.</small></p>}

      <h2>Evidence</h2>
      <ul className="receipt-checks">
        {[...checks(d), chain].map((k) => (
          <li key={k.text} className={k.state}>
            <span aria-hidden="true">{MARK[k.state]}</span> {k.text}
          </li>
        ))}
      </ul>

      <p className="receipt-foot">
        <small>This receipt records the choice captured on the call. It is not a copy of the notice itself.</small>
      </p>
    </main>
  )
}
