# IVR Consent Capture

Captures DPDP consent over an Exotel IVR call, stores it as the durable
write-ahead record with a hash-chained audit trail, and pushes it into UCM
asynchronously so a UCM outage can never lose a consent.

Design doc: **IVR Consent Capture — Design & UCM Integration**.

## Shape

Two telephony providers behind one interface. The consent state machine does
not know which one carried the call.

```
Exotel  --GET-->  /exotel/identify   (Passthru, async)   principal + CRM lookup
        --GET-->  /exotel/notice     (Greeting, dynamic) versioned notice text
        --GET-->  /exotel/decision   (Passthru, sync)    200 committed / 302 failed
        --GET-->  /exotel/readback   (Greeting, dynamic) decision read back
        --POST->  /exotel/status     (StatusCallback)    recording, AnsweredBy

Twilio  --POST->  /twilio/voice      (TwiML)             <Gather> wrapping the notice
        --POST->  /twilio/decision   (Gather action)     <Redirect> or apology
        --POST->  /twilio/readback   (TwiML)             decision read back
        --POST->  /twilio/status     (StatusCallback)    AnsweredBy, durations
        --POST->  /twilio/recording  (RecordingStatus)   recording ready
        every request signed; unverified = 403, and the receipt is kept

  consent + audit event + outbox row  =  one transaction
  worker  --POST-> UCM /v1/consents   (Idempotency-Key: consent_id)
  console  GET     /api/console/*      read-only operator and DPO screens
```

## Run it

```bash
pip install -r requirements.txt
createdb ivr_consent
export DATABASE_URL=postgresql+psycopg2://user@localhost/ivr_consent
python -c "from app.db import apply_migrations; apply_migrations()"

cd ui && npm install && npm run build && cd ..   # builds the console

uvicorn app.main:app --port 8088     # API + webhooks + console at /console
python -m app.worker                 # outbox drain loop
```

The console is at `http://localhost:8088/console`. For UI development run
`npm run dev` in `ui/` instead — Vite proxies the API to port 8088.

`.env.example` lists what else must be set. Nothing secret has a default.

## Tests

```bash
pytest -q          # 80 tests, needs a local Postgres
```

The suite drives simulated calls on both providers against a stubbed UCM.
Notable cases, because each one pins a failure that would otherwise be
silent:

| Test | Guards against |
| --- | --- |
| `test_digits_are_unquoted` | Exotel sends `digits` wrapped in literal double quotes; a naive compare makes every consent read as no-decision |
| `test_frozen_end_time_is_rejected` | `EndTime` is always `1970-01-01 05:30:00` on Passthru |
| `test_hash_is_timezone_independent` | Postgres renders timestamptz in the connection's TimeZone; hashing the rendering voids the whole audit trail when read from another zone |
| `test_only_one_current_consent_per_purpose_ever` | Contradictory consent state |
| `test_grant_and_withdrawal_are_delivered_in_order` | UCM holding a live consent the person revoked |
| `test_replayed_webhook_is_a_noop` | Exotel's retry policy is undocumented |
| `test_silence_is_never_consent` | Timeouts and hangups becoming consent |
| `test_purge_keeps_the_hash_after_deleting_the_audio` | Losing proof that the recording existed |
| `test_signing_string_matches_the_documented_example` | Drift in the Twilio signature algorithm |
| `test_signature_covers_the_keypress` | A signature that does not actually protect the decision |
| `test_url_with_and_without_explicit_port_both_validate` | Intermittent Twilio failures behind a proxy |
| `test_unsigned_request_records_no_consent` | Consent forged against a public endpoint |
| `test_signature_is_kept_for_later_reverification` | Storing a merged param view that will not re-verify |
| `test_answering_machine_is_never_consent` | Voicemail counted as agreement |

## What this does not do yet

No real Exotel credentials, no CRM adapter beyond the `CrmPort` interface,
no ASR, no dialler, no S3 recording fetcher (the hook is
`evidence.queue_recording_fetch`). `crypto.encrypt_attribute` is a
placeholder with the right interface — replace it with KMS envelope
encryption before real data touches this.

## Choosing a provider

| | Exotel | Twilio |
| --- | --- | --- |
| Webhook signing | None documented | HMAC-SHA1 per request |
| Digits on the wire | Wrapped in literal double quotes | Plain |
| Recording URLs | Access model undocumented | HTTP Basic enforced |
| India residency | Mumbai cluster | **No India region at all** |
| Indian numbers | Local and mobile | Toll-free `+91800` only |

Exotel for India, Twilio elsewhere. Twilio's signature is a genuinely
stronger evidentiary chain, but its own docs decline to guarantee that data
stays in a selected region, and there is no Indian region to select.

## Two things to know before deploying

**Exotel does not sign its webhooks.** No HMAC, no shared secret, nothing.
The `/exotel/*` routes must be bound to a separate hostname behind an IP
allowlist, and every consent needs corroborating against the authenticated
Call Details API before it is trusted. Exotel's source IP ranges are not
published — request them from `hello@exotel.com`.

**Check which cluster the account is on.** `api.exotel.com` is Singapore and
is the default for new accounts; `api.in.exotel.com` is Mumbai. Exotel's
documented recording URLs point at AWS Singapore in every example, so the
recording fetcher copies audio into our own `ap-south-1` bucket and treats
Exotel's copy as transient.
