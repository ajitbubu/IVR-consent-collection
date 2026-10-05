# IVR Consent Capture

Captures DPDP consent over a phone call (Exotel in India, Twilio elsewhere),
stores it as the durable write-ahead record with a hash-chained audit trail,
and pushes it into UCM (the ID-PRIVACY® consent API) asynchronously, so a UCM
outage can never lose a consent. An operator console at `/console` shows every
consent, call and notice, and prints a DPDP consent receipt.

**Documentation**

| Read this | When you want to |
|---|---|
| [Tutorial: your first consent](#tutorial-your-first-consent-in-10-minutes) | See it work end to end on your laptop |
| [How-to guides](#how-to-guides) | Connect Twilio or Exotel, place an outbound call, add a purpose, run the tests |
| [Reference](#reference) | Look up an endpoint, a webhook, a config variable or a behaviour |
| [doc/architecture.md](doc/architecture.md) | Understand how it works and why it is built this way (diagrams, design decisions) |
| [doc/id-privacy-integration.md](doc/id-privacy-integration.md) | Integrate with the ID-PRIVACY® platform and move to MongoDB |
| [TODOS.md](TODOS.md) | See known follow-up work and deferred QA issues |

## Shape

Three telephony providers behind one interface. The consent state machine does
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

Sprinklr --POST-> /sprinklr/start    (HTTP node, JSON)   session + notice to play
         --POST-> /sprinklr/decision (HTTP node, JSON)   {committed, outcome, say}
         --POST-> /sprinklr/status   (HTTP node, JSON)   end of call, recording
         bearer token on every request; wrong or missing = 401, receipt kept

  consent + audit event + outbox row  =  one transaction
  worker  --POST-> UCM /v1/consents   (Idempotency-Key: consent_id)
  console  GET     /api/console/*      read-only operator and DPO screens
           GET     /v1/consents/{id}/evidence   receipt and evidence panel
```

---

## Tutorial: your first consent in 10 minutes

You'll run the service locally, simulate an Exotel call in which the caller
presses 1, and watch the consent land in the database, reach a (stub) UCM,
and appear in the console with its receipt.

### What you'll need

- Python 3.12, Node 20+, Docker (for a throwaway Postgres)
- This repository checked out, and a terminal in its root

### Step 1: Start Postgres and install

```bash
docker run -d --name ivr-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:5440:5432 postgres:16
pip install -r requirements.txt
(cd ui && npm install && npm run build)

export DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:5440/postgres
export UCM_BASE_URL=http://127.0.0.1:9999 PUBLIC_BASE_URL=http://127.0.0.1:8088
python -c "from app.db import apply_migrations; apply_migrations()"
```

The last command creates the schema. It prints nothing on success.

### Step 2: Add a purpose and its notice

A **purpose** is what the person is consenting to. A **notice** is the exact
wording they hear, per language. Published notices never change: a new wording
is a new version.

```bash
python - <<'PY'
import datetime as dt
from ulid import ULID
from app.db import session_scope
from app.consent_service import notice_digest
from app.models import NoticeVersion, Purpose

body = ("Data Safeguard would like to contact you about our products. "
        "To agree press 1. To decline press 2.")
with session_scope() as db:
    p = Purpose(id=str(ULID()), code="marketing_outreach", name="marketing calls and messages",
                retention_days=365, requires_verification=False, ucm_purpose_key="marketing_outreach")
    db.add(p); db.flush()
    db.add(NoticeVersion(id=str(ULID()), purpose_id=p.id, version=1, language="eng",
                         body_text=body, body_sha256=notice_digest(body),
                         published_at=dt.datetime.now(dt.timezone.utc)))
print("purpose marketing_outreach + notice v1 created")
PY
```

### Step 3: Start the service, the worker and a stub UCM

Open three terminals (each with the `export` lines from step 1):

```bash
# terminal 1: a stand-in for the ID-PRIVACY® consent API that accepts everything
python -c "
import json, http.server as h
class U(h.BaseHTTPRequestHandler):
    def do_POST(s):
        b=json.loads(s.rfile.read(int(s.headers['content-length'])))
        print('UCM received', b['decision'], 'key', s.headers['Idempotency-Key'], flush=True)
        s.send_response(201); s.send_header('content-type','application/json'); s.end_headers()
        s.wfile.write(b'{\"consent_ref\": \"ucm-demo\"}')
    def log_message(s,*a): pass
h.HTTPServer(('127.0.0.1',9999),U).serve_forever()"

# terminal 2: API, webhooks and console
uvicorn app.main:app --port 8088

# terminal 3: the outbox worker that delivers to UCM
python -m app.worker

# optional, terminal 4: evidence jobs (reconciliation, recording fetch,
# purge, daily digest). Reconciliation needs real provider credentials.
python -m app.jobs
```

Check it is up: `curl -s http://127.0.0.1:8088/readyz` prints
`{"status":"ok","outbox_lag_seconds":0.0}`.

### Step 4: Make a call (simulated Exotel)

These are the same requests Exotel sends during a real call. Exotel wraps the
keypress in literal double quotes, hence `%221%22`.

```bash
H=http://127.0.0.1:8088
SID=$(curl -s -X POST $H/v1/sessions -H 'content-type: application/json' \
  -d '{"direction":"ivr_outbound","phone_e164":"09876543210","purpose_key":"marketing_outreach"}' \
  | python -c "import json,sys;print(json.load(sys.stdin)['session_id'])")
curl -s "$H/exotel/identify?CustomField=$SID&CallSid=CA-demo-1&CallFrom=09876543210"
curl -s "$H/exotel/notice?CustomField=$SID&CallSid=CA-demo-1"; echo
curl -s -o /dev/null -w "decision -> %{http_code}\n" "$H/exotel/decision?CustomField=$SID&CallSid=CA-demo-1&digits=%221%22"
curl -s "$H/exotel/readback?CustomField=$SID"; echo
```

You'll see the notice text, `decision -> 200`, and
`You have agreed to marketing calls and messages. Your reference is ……`.
Within about two seconds terminal 1 prints `UCM received granted key 01…`.

### Step 5: Look at the result

```bash
curl -s "$H/v1/consents?phone_e164=%2B919876543210" | python -m json.tool
```

The consent is `granted`, `permits_processing: true`, `ucm_sync_state: synced`.
Now open **http://127.0.0.1:8088/console**: the call is under *Calls*, the
consent under *Consents*. Open it to see the notice, the webhooks, the
history, and *Consent receipt* for the printable DPDP receipt.

### What you built

A working consent capture: the caller heard a pinned notice version, the
keypress became a consent, an audit event and an outbox row in one
transaction, and the worker delivered it to UCM with an idempotency key. Next:
[connect a real Twilio number](#how-to-connect-a-twilio-number), or read
[why it's built this way](doc/architecture.md#why-its-built-this-way).
Clean up with `docker rm -f ivr-pg`.

---

## How-to guides

### How to connect a Twilio number

You'll make calls to a real Twilio number reach this service.

**Prerequisites:** a Twilio account and number; the service running (tutorial
steps 1–3); `cloudflared` or another HTTPS tunnel.

1. Start a tunnel and note its URL:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8088
   ```
2. Restart the API with your **primary** Auth Token (not an API key) and the
   exact tunnel origin. Keep credentials in a git-ignored file and `source` it.
   ```bash
   export TWILIO_ACCOUNT_SID=AC… TWILIO_AUTH_TOKEN=… TWILIO_CALLER_ID=+1…
   export PUBLIC_BASE_URL=https://<your-tunnel>.trycloudflare.com TWILIO_ENFORCE_SIGNATURE=true
   uvicorn app.main:app --port 8088
   ```
3. In the Twilio Console → Phone Numbers → your number → *A call comes in*:
   Webhook, `HTTP POST`, `https://<your-tunnel>/twilio/voice?purpose=marketing_outreach`.
   Optionally set the status callback to `https://<your-tunnel>/twilio/status`.
4. Call the number and press 1.

**Verify:** the console shows the consent with provider `twilio` and three
*verified* webhooks (voice, decision, readback).

**Troubleshooting**

| Symptom | Cause / fix |
|---|---|
| Every request 403, "signature mismatch" in the log | `PUBLIC_BASE_URL` differs from the URL Twilio called (scheme, host, or tunnel restarted). Set it to the exact origin and restart |
| Caller hears "application error" | The service was unreachable or returned non-TwiML. Check the Twilio debugger; a withheld caller ID is handled and hears a spoken hangup |
| Trial account: call rejected before reaching you | Trial numbers only accept calls from *Verified Caller IDs* |
| Ended / Answered-by blank in the console | No status callback URL set on the number (step 3) |

### How to connect Exotel

1. Set `EXOTEL_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_CALLER_ID`,
   and `EXOTEL_BASE_URL` (`https://api.in.exotel.com` for the Mumbai cluster).
2. Build an Exotel flow: **Passthru** (async) `…/exotel/identify?purpose=<code>` →
   **Greeting** (dynamic) `…/exotel/notice` → **Gather** → **Passthru** (sync)
   `…/exotel/decision` (200 = recorded branch, 302 = failure branch) →
   **Greeting** (dynamic) `…/exotel/readback`. Set the call's StatusCallback to
   `…/exotel/status`.
3. Bind `/exotel/*` to its own hostname behind an IP allowlist: Exotel does not
   sign webhooks (see [before deploying](#two-things-to-know-before-deploying)).

**Verify:** a test call shows provider `exotel` and a consent; the decision
endpoint answers 200.

### How to connect Sprinklr

Sprinklr's IVR flow plays the audio and collects the key itself; HTTP nodes
in the flow call three JSON endpoints. The flow branches on response fields,
not status codes.

1. Set `SPRINKLR_WEBHOOK_TOKEN` to a long random secret. Every HTTP node sends
   `Authorization: Bearer <token>` and `Content-Type: application/json`.
2. Build the flow:
   1. **HTTP node** `POST …/sprinklr/start` with
      `{"call_id", "from", "to", "direction": "inbound"|"outbound", "session_id"?, "purpose"?, "language"?}`.
      Outbound calls pass the `session_id` from `POST /v1/sessions` (with
      `"provider": "sprinklr"`); inbound calls pass `purpose` instead.
      The response is `{"proceed", "session_id", "say", "audio_url"}`. If
      `proceed` is false, speak `say` and hang up.
   2. **Play** `audio_url` if it is set, otherwise speak `say` with TTS.
   3. **Collect one digit** (1 agree, 2 decline, 9 withdraw).
   4. **HTTP node** `POST …/sprinklr/decision` with
      `{"session_id", "call_id", "digits", "occurred_at"?}`. Send an empty
      `digits` on timeout so silence is recorded. The response is
      `{"committed", "outcome", "say"}`: speak `say`, then hang up.
   5. At call end, **HTTP node** `POST …/sprinklr/status` with
      `{"call_id", "session_id"?, "status", "ended_at"?, "recording_url"?}`.
3. Timestamps are ISO 8601; one without an offset is read as UTC.

**Verify:** a test call shows provider `sprinklr` in the console, the
decision response has `"committed": true`, and its receipts show `verified`.

#### Test the Sprinklr flow locally, without Sprinklr

These are the requests Sprinklr's HTTP nodes send during a call. Start the
service with `scripts/run-dev.sh` (HTTPS on port 8088; see
[Sprinklr OAuth setup](#sprinklr-oauth-setup)), add the `marketing_outreach`
purpose from [tutorial step 2](#step-2-add-a-purpose-and-its-notice), then in a
second terminal:

```bash
set -a; source .env; set +a      # SPRINKLR_WEBHOOK_TOKEN, never echoed
H=https://localhost:8088; J='content-type: application/json'
A="Authorization: Bearer $SPRINKLR_WEBHOOK_TOKEN"

curl -sk -X POST $H/sprinklr/start -H "$A" -H "$J" \
  -d '{"call_id":"spr-demo-1","from":"09876543210","direction":"inbound","purpose":"marketing_outreach"}'; echo
curl -sk -X POST $H/sprinklr/decision -H "$A" -H "$J" \
  -d '{"call_id":"spr-demo-1","digits":"1"}'; echo
curl -sk -o /dev/null -w "%{http_code}\n" -X POST $H/sprinklr/decision \
  -H 'Authorization: Bearer wrong' -H "$J" -d '{"call_id":"spr-demo-1","digits":"1"}'
```

You'll see `"proceed":true` with the notice text, then `"committed":true` with
`You have agreed to marketing calls and messages…`, then `401`. Use a new
`call_id` for each run: a repeated one returns the first call's decision. `-k`
accepts the self-signed certificate; with mkcert it isn't needed.

### Sprinklr OAuth setup

This is the other direction: our service calling Sprinklr's REST API, which
reconciliation will use to look up calls. It uses Sprinklr's OAuth 2.0 code
grant, as documented on [dev.sprinklr.com](https://dev.sprinklr.com) (Getting
Started, Refreshing Access Token). You need a developer-portal app *and* a
user on a Sprinklr instance with the **Generate API Token** permission.

1. Put the app's key and secret in `.env` (gitignored), and set the environment
   and callback:

   ```bash
   SPRINKLR_API_KEY=...
   SPRINKLR_API_SECRET=...
   SPRINKLR_ENV=spr-uat          # your instance's "sentry-environment", see below
   SPRINKLR_REDIRECT_URI=https://localhost:8088/sprinklr/oauth/callback
   ```

   `SPRINKLR_REDIRECT_URI` must match the app's **Callback URL** in the
   portal exactly, or Sprinklr answers with a 500. To find the environment, sign
   in to the Sprinklr UI, view the page source and search for
   `sentry-environment`. Use `prod` for the main production environment; it has
   no path segment in API URLs. `spr-uat` is a placeholder and is not a
   documented environment name.
2. Serve the API over HTTPS on `localhost:8088`, because the registered callback
   is `https://`. With [mkcert](https://github.com/FiloSottile/mkcert):

   ```bash
   brew install mkcert && mkcert -install          # trusts a local CA, once
   mkdir -p var/certs && mkcert -cert-file var/certs/localhost.pem \
     -key-file var/certs/localhost-key.pem localhost 127.0.0.1
   uvicorn app.main:app --port 8088 --env-file .env \
     --ssl-certfile var/certs/localhost.pem --ssl-keyfile var/certs/localhost-key.pem
   ```

   `var/` is gitignored. Without mkcert, a self-signed certificate from
   `openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=localhost -addext
   subjectAltName=DNS:localhost -keyout var/certs/localhost-key.pem -out
   var/certs/localhost.pem` works too, after you accept the browser warning.
   The app reads only real environment variables, so pass `--env-file .env` to
   uvicorn, or run `set -a; source .env; set +a` first for `python -m app.jobs`.
   `scripts/run-dev.sh` does all of this in one step: it loads `.env`, creates
   the certificate on first run, creates the schema on an empty database, and
   serves `https://localhost:8088`. Add `--all` to also start the outbox worker
   and `app.jobs`, or set `PORT=` to use another port.
3. Open `https://localhost:8088/sprinklr/oauth/login` in a browser. Sign in to
   Sprinklr, choose the environments the token may access, and submit.
4. Sprinklr redirects to `/sprinklr/oauth/callback`. The service checks the
   `state`, exchanges the code (valid for 10 minutes) for tokens, and stores
   them encrypted in `sprinklr_oauth_token`. The page shows
   `{"connected": true, "env": …, "expires_at": …}`, and never the tokens.
5. Set `SPRINKLR_API_ENABLED=true` once the instance is live. `python -m app.jobs`
   then refreshes the token once 80% of its lifetime has passed. Sprinklr allows
   **one token per API key**, so connecting from a second deployment with the
   same key logs the first one out. Give each deployment its own app.

**Verify:** the callback returns `"connected": true`, and
`SprinklrClient(db).get("/api/v2/me")` returns your Sprinklr user.

The developer portal says the callback must be publicly accessible. If
Sprinklr refuses `https://localhost:8088/…`, expose the service through a tunnel
(for example `cloudflared tunnel --url https://localhost:8088`), then register
the tunnel's `https://` URL as the Callback URL and set it as
`SPRINKLR_REDIRECT_URI`.

### How to place an outbound call

There is no dialler endpoint yet. Create a session, then call the provider
helper with its `session_id`:

```bash
python - <<'PY'
import httpx
from app.telephony.outbound import place_call_twilio
base = "https://<your-tunnel>.trycloudflare.com"
s = httpx.post("http://127.0.0.1:8088/v1/sessions", json={
    "direction": "ivr_outbound", "phone_e164": "+1XXXXXXXXXX",
    "purpose_key": "marketing_outreach", "provider": "twilio"}).json()
print(place_call_twilio(to_number="+1XXXXXXXXXX", session_id=s["session_id"],
                        voice_url=f"{base}/twilio/voice",
                        status_callback=f"{base}/twilio/status",
                        recording_callback=f"{base}/twilio/recording"))
PY
```

The call uses answering-machine detection (a voicemail is never consent) and
dual-channel recording. For Exotel use
`place_call_exotel(to_number=, flow_app_id=, session_id=, status_callback=)`.

### How to add a purpose or a new notice version

Purposes and notices are rows (there is no admin API yet). Use the step-2
script from the tutorial with your own `code`, `name`, `retention_days`,
`requires_verification` and `ucm_purpose_key` (the ID-PRIVACY® purpose ID).
To change wording, insert a **new** `NoticeVersion` with `version` + 1 and set
`retired_at` on the old one. Never edit a published notice: sessions pin the
version they played. Check with `GET /v1/purposes`.

### How to run the tests

```bash
docker run -d --name ivr-qa-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:5435:5432 postgres:16
PGHOST=127.0.0.1 PGPORT=5435 pytest -q        # 135 tests
```

The fixtures drop and recreate `ivr_consent_test` as the `postgres` superuser
and shell out to the `psql` client, so `psql` must be on `PATH`
(`brew install libpq`).

---

## Reference

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | none (required) | SQLAlchemy URL for Postgres |
| `PHONE_HMAC_KEY` | `dev-only-not-for-production` | HMAC key for phone lookup hashes. Rotating it requires rehashing every principal. **Must be set in production** |
| `ATTRIBUTE_KEY` | `dev-only-not-for-production` | Key-encryption key for name/email at rest (AES-256-GCM envelope encryption via `LocalKms`; swap in a real KMS behind `crypto.KmsPort` for production). **Must be set** |
| `EVIDENCE_DIR` | `var/evidence` | Where `python -m app.jobs` stores recordings and write-once daily digests |
| `UCM_BASE_URL` | `http://localhost:9999` | ID-PRIVACY® consent API base; the worker POSTs to `/v1/consents` (5 s timeout) |
| `PUBLIC_BASE_URL` | `http://localhost:8088` | External origin; used to rebuild the URL Twilio signed and for TwiML action URLs |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_CALLER_ID` | `unset` | Twilio live credentials (primary auth token) |
| `TWILIO_API_BASE` | `https://api.twilio.com` | Twilio REST base |
| `TWILIO_VOICE`, `TWILIO_LANGUAGE` | `""`, `en-IN` | TTS voice and language for `<Say>` |
| `TWILIO_ENFORCE_SIGNATURE` | `true` | Reject unsigned/forged Twilio webhooks (403). Only `false` in a local harness |
| `EXOTEL_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_CALLER_ID` | `unset` | Exotel credentials |
| `EXOTEL_BASE_URL` | `https://api.in.exotel.com` | Mumbai cluster; `api.exotel.com` is Singapore |
| `SPRINKLR_WEBHOOK_TOKEN` | `unset` | Bearer token Sprinklr's HTTP nodes must send. While unset, every Sprinklr request gets a 401 |
| `SPRINKLR_API_KEY`, `SPRINKLR_API_SECRET` | `unset` | Developer-portal app credentials for calling Sprinklr's API. `/sprinklr/oauth/login` answers 503 while unset |
| `SPRINKLR_ENV` | `spr-uat` | Instance environment (`prod`, `prod0`, `prod2`, …) from `sentry-environment`; `prod` has no path segment. The default is a placeholder |
| `SPRINKLR_REDIRECT_URI` | `https://localhost:8088/sprinklr/oauth/callback` | Must equal the app's Callback URL exactly |
| `SPRINKLR_API_BASE` | `https://api3.sprinklr.com` | Sprinklr API host |
| `SPRINKLR_API_ENABLED` | `false` | Turns on token refresh in `app.jobs` and the Sprinklr call lookup in reconciliation |
| `DEFAULT_PROVIDER` | `exotel` | Provider for sessions that don't name one |
| `CORS_ORIGINS` | empty | Comma-separated origins (dev only, for the Vite server) |
| `VITE_API_BASE` (UI build) | empty | API origin if the console is served elsewhere |
| `VITE_RECEIPT_ISSUER` (UI build) | `FaceOff Privacy` | Issuer name printed on the consent receipt |

Fixed in code: country code `91` for numbers without one; outbox backoff
5 s doubling to 900 s, row marked `failed` after 12 attempts (it keeps retrying
every 900 s). Evidence jobs: a call is reconciled 300 s after it started; a
recording download is tried 12 times, then left with `last_error` set.

### Service API (`/v1`)

| Method & path | Body / params | Returns |
|---|---|---|
| `POST /v1/sessions` | `{direction: ivr_inbound\|ivr_outbound, phone_e164, purpose_key, language="eng", provider="exotel"\|"twilio"}` | 201 `{session_id, custom_field, notice_version_id, provider}`; 400 unknown purpose / no live notice / malformed phone |
| `GET /v1/consents` | `phone_e164` (any shape) or `data_principal_id` | `{data_principal_id, consents:[{consent_id, purpose_key, status, decision, permits_processing, decided_at, channel, verification_level, expires_at, ucm_sync_state}]}` (current only); 400 neither given or malformed phone; 404 unknown |
| `GET /v1/consents/{id}` | | One consent incl. `is_current`, `superseded_by`, `ucm_consent_ref`; 404 |
| `GET /v1/consents/{id}/evidence` | | Evidence bundle: consent, principal, notice text + hash, call, artifacts, full hash chain with `verified`; 404 |
| `POST /v1/consents/withdraw` | query `phone_e164`; body `{purpose_key, channel="agent", reason?}` | 201 `{consent_id, status:"withdrawn"}`. **Known gaps:** `channel` must be one of `ivr_inbound`, `ivr_outbound`, `web`, `app`, `agent` (422 otherwise) and is stored on the consent; `reason` is accepted but not stored; the session's `provider` is still recorded as `exotel`; an unknown phone creates a principal; 400 when the purpose has no live notice |
| `GET /v1/purposes` | | Purposes with their live notices |
| `GET /healthz`, `GET /readyz` | | Liveness; readiness + `outbox_lag_seconds` (`degraded` above 3600 s) |

### Console API (`/api/console`, read-only)

| Path | Params |
|---|---|
| `GET /consents` | `q` (phone in any shape or consent id; anything else is a partial `LIKE` match on the number), `decision`, `status`, `purpose`, `provider`, `sync`, `current_only=true`, `limit≤200`, `offset`. Phones are masked in lists |
| `GET /consents/{id}` | Detail with full phone, call, webhooks, history |
| `GET /stats` | `days≤365` (default 30) |
| `GET /health` | Queue, lag, failed syncs, unreconciled calls, bad signatures |
| `GET /sessions` | `provider`, `outcome`, `limit≤200` |
| `GET /purposes`, `GET /events` | `events`: `limit≤500` recent audit events |

### Webhooks

| Route | From | Responds |
|---|---|---|
| `GET /exotel/identify` | Exotel Passthru (async) | 200; creates the session for inbound calls from `?purpose=`, `CallFrom` and optional `?lang=` (notice language, default `eng`). 400 if `CallFrom` is missing or malformed; an unknown `purpose` is an unhandled error (500) |
| `GET\|HEAD /exotel/notice` | Exotel Greeting | `text/plain` notice of the pinned version |
| `GET /exotel/decision` | Exotel Passthru (sync) | **200** consent committed; **302** no session found, no input, unoffered key, verification required or write failure |
| `GET\|HEAD /exotel/readback` | Exotel Greeting | Decision and 6-character reference, or "Nothing has changed" |
| `POST /exotel/status` | StatusCallback | Always 200; stores status and AnsweredBy; queues the recording; `python -m app.jobs` copies it into `EVIDENCE_DIR` and hashes it |
| `POST /twilio/voice` | Twilio (signed) | TwiML `<Gather numDigits=1>` with the notice; creates the inbound session from `?purpose=` and optional `?lang=`, links this request's receipt to it, and binds an unbound session to `twilio`. Spoken hangup if no purpose or withheld caller ID |
| `POST /twilio/decision` | `<Gather>` action | `<Redirect>` to readback, or a spoken hangup for silence / bad key / answering machine |
| `POST /twilio/readback` | Twilio | Spoken decision and reference |
| `POST /twilio/status` | StatusCallback | 204; older `SequenceNumber` ignored; end time set only by a final status |
| `POST /twilio/recording` | RecordingStatusCallback | 204; queues the recording; `python -m app.jobs` downloads it (HTTP Basic) into `EVIDENCE_DIR` and hashes it |
| `POST /sprinklr/start` | HTTP node, JSON | 200 `{proceed, session_id, say, audio_url}`; creates the session for inbound calls; 401 without the bearer token |
| `POST /sprinklr/decision` | HTTP node, JSON | 200 `{committed, outcome, say}`; `committed` is true only after the consent is committed; 401 without the token |
| `POST /sprinklr/status` | HTTP node, JSON | 204; stores status and end time; queues `recording_url` (downloaded without auth) |
| `GET /sprinklr/oauth/login` | Browser | 302 to Sprinklr's authorize URL with a `state` cookie (10 min); 503 without key/secret |
| `GET /sprinklr/oauth/callback` | Browser, from Sprinklr | 200 `{connected, env, expires_at}`; 400 on a state mismatch or a Sprinklr error; 502 if the code exchange fails |

Every Twilio webhook request, and Exotel's `/exotel/decision`, is stored as a
`webhook_receipt` (Twilio signature included), whether or not it was accepted.
Exotel's `identify`, `notice`, `readback` and `status` do not write receipts yet
(the `/status` case is tracked in [TODOS.md](TODOS.md)).

### Keys and outcomes

| Key | Decision | Status |
|---|---|---|
| 1 | `granted` | `active`; `permits_processing` turns false after `decided_at` + `retention_days` (the status itself stays `active`; nothing sets `expired` yet) |
| 2 | `declined` | `declined` |
| 9 | `withdrawn` | `withdrawn` |

Calls that produce no consent record a session `outcome`: `no_input`,
`invalid_key`, `verification_required`, `answering_machine` (Twilio). A
second keypress on the same call keeps the first decision.

---

## Tests

`PGHOST=127.0.0.1 PGPORT=5435 pytest -q` runs 135 tests (see
[how to run the tests](#how-to-run-the-tests)). The suite drives simulated
calls on both providers against a stubbed UCM. Notable cases, because each one
pins a failure that would otherwise be silent:

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
| `test_withheld_caller_id_gets_a_spoken_hangup_not_an_error` | A withheld number making Twilio play "application error" |
| `test_late_status_callback_does_not_rewind_the_call` | Out-of-order status callbacks rewinding a finished call |
| `test_malformed_phone_is_rejected_not_a_server_error` | A bad number crashing the API or a webhook with a 500 |

## What this does not do yet

No CRM adapter beyond the `CrmPort` interface, no ASR, no dialler endpoint, and
no authentication on `/v1` or the console (deploy behind the PMP gateway).
Evidence storage is the local filesystem (`EVIDENCE_DIR`), not S3 with Object
Lock, and attribute encryption uses `LocalKms` rather than a cloud KMS. Calls
are reconciled after they end, so a consent can reach UCM before its call is
corroborated; a mismatch then only holds consents not yet delivered. Sprinklr
calls are not reconciled at all: the API client and OAuth are in place, but
the call-details lookup is a stub until Sprinklr names the endpoint. Sprinklr
recording URLs are downloaded without authentication. Full list:
[doc/architecture.md](doc/architecture.md#not-yet-implemented).

## Choosing a provider

| | Exotel | Twilio |
| --- | --- | --- |
| Webhook signing | None documented | HMAC-SHA1 per request |
| Digits on the wire | Wrapped in literal double quotes | Plain |
| Recording URLs | Access model undocumented | HTTP Basic enforced |
| India residency | Mumbai cluster | **No India region at all** |
| Indian numbers | Local and mobile | Toll-free `+91800` only |

Sprinklr is not in this table because its webhook signing, recording access
and data residency have not been checked against its docs yet; this service
authenticates it with a shared bearer token.

Exotel for India, Twilio elsewhere. Twilio's signature is a genuinely
stronger evidentiary chain, but its own docs decline to guarantee that data
stays in a selected region, and there is no Indian region to select.

## Two things to know before deploying

**Exotel does not sign its webhooks.** No HMAC, no shared secret, nothing.
The `/exotel/*` routes must be bound to a separate hostname behind an IP
allowlist, and every consent needs corroborating against the authenticated
Call Details API — `python -m app.jobs` does this after each call ends. Exotel's source IP ranges are not
published — request them from `hello@exotel.com`.

**Check which cluster the account is on.** `api.exotel.com` is Singapore and
is the default for new accounts; `api.in.exotel.com` is Mumbai. Exotel's
documented recording URLs point at AWS Singapore in every example, so the
recording fetcher copies audio into our own storage and treats Exotel's copy as
transient (local filesystem today; an `ap-south-1` bucket in production).
