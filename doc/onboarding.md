# Onboarding: IVR Consent Capture

For a developer joining the project. By the end of day one you'll have the
service running over HTTPS, a consent recorded through a simulated call, and
the test suite passing. After that, this guide shows how the parts fit
together and how to make the changes people make most often.

It links rather than repeats. The [README](../README.md) is the reference
(endpoints, config, behaviour), and [architecture.md](architecture.md) explains
the design and why it is built this way.

## What you're working on

The service records DPDP consent given over a phone call. A caller hears a
versioned notice and presses 1 (agree), 2 (decline) or 9 (withdraw). The
decision is stored in Postgres as a permanent, hash-chained record. A separate
worker then delivers it to UCM, the ID-PRIVACY® consent API, so a UCM outage
never loses a consent. Calls come in through three telephony providers:

| Provider | Used for | How it reaches us | How we trust it |
|---|---|---|---|
| Exotel | India | GET webhooks from an Exotel flow | IP allowlist, then reconciliation against its call-details API |
| Twilio | Outside India | Signed POST webhooks, TwiML replies | HMAC-SHA1 signature on every request |
| Sprinklr | Being integrated | JSON from HTTP nodes in a Sprinklr IVR flow | Shared bearer token |

An operator console at `/console` shows every consent and call, and prints a
DPDP consent receipt.

## Day one: set up and run it

### 1. Install the tools

- Python 3.12 and [uv](https://docs.astral.sh/uv/) (the project venv is built with uv)
- Node 20+ (for the console)
- Docker (for Postgres)
- The `psql` client, which the test fixtures call:
  `brew install libpq`, then add `/opt/homebrew/opt/libpq/bin` to your `PATH`

### 2. Install dependencies

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements.txt
(cd ui && npm install && npm run build)     # the service serves ui/dist at /console
```

### 3. Start Postgres

```bash
docker run -d --name ivr-qa-pg -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1:5435:5432 postgres:16
docker exec ivr-qa-pg psql -U postgres -c "CREATE DATABASE ivr_consent_dev"
```

The tests use their own database, `ivr_consent_test`, which they drop and
recreate on every run. Keep your dev data in `ivr_consent_dev`.

### 4. Create `.env`

`.env` is gitignored and holds your local settings and secrets. Start with:

```bash
DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:5435/ivr_consent_dev
PUBLIC_BASE_URL=https://localhost:8088
UCM_BASE_URL=http://127.0.0.1:9999
SPRINKLR_WEBHOOK_TOKEN=devtoken
```

[`.env.example`](../.env.example) lists every other variable with its meaning.
Never put a real value in `.env.example`: it's committed.

### 5. Run the service

```bash
scripts/run-dev.sh            # API, webhooks and console on https://localhost:8088
scripts/run-dev.sh --all      # also the outbox worker and the evidence jobs
```

The script loads `.env`, creates a dev certificate in `var/certs` on first run,
and creates the schema if the database is empty. Check it's up:
`curl -sk https://localhost:8088/readyz` prints `{"status":"ok",…}`.

### 6. Record your first consent

1. Create the `marketing_outreach` purpose and its notice:
   [README tutorial, step 2](../README.md#step-2-add-a-purpose-and-its-notice).
2. Simulate a Sprinklr call:
   [Test the Sprinklr flow locally](../README.md#test-the-sprinklr-flow-locally-without-sprinklr).
   Or simulate an Exotel call with the [tutorial's step 4](../README.md#step-4-make-a-call-simulated-exotel),
   using `https://localhost:8088` and `curl -k`.
3. Open `https://localhost:8088/console` and find the consent. Its detail page
   shows the notice the caller heard, the webhook receipts and the verified hash
   chain.

### 7. Run the tests

```bash
PGHOST=127.0.0.1 PGPORT=5435 pytest -q
```

All tests should pass. If every test errors with `FileNotFoundError`, `psql`
isn't on your `PATH`.

## How it fits together

### Three processes, one database

| Process | Command | Does |
|---|---|---|
| API | `uvicorn app.main:app` | Provider webhooks, the `/v1` service API, the console |
| Outbox worker | `python -m app.worker` | Delivers consents to UCM every 2 s, in order per person, with retries |
| Evidence jobs | `python -m app.jobs` | Every 60 s: reconciles calls, downloads recordings, purges expired ones, writes the daily digest, refreshes the Sprinklr token |

They share nothing but Postgres, so any of them can restart at any time.

### The path of one keypress

1. A provider webhook hits `app/routes_<provider>.py`.
2. `webhook_common.ingest()` verifies it (`app/telephony/<provider>.py`),
   normalises it into a `WebhookEvent`, finds the session, and saves a
   `webhook_receipt`. Rejected requests get a receipt too.
3. The route calls `consent_service.record_decision()`. It locks the person's
   row, marks their previous consent for this purpose as superseded, and inserts
   the new consent, an audit event (`hashchain.append_event`) and an outbox row,
   all in one transaction.
4. The route **commits, then** tells the caller their choice was recorded.
5. Later, `app/worker.py` delivers the outbox row to UCM, and `app/jobs.py`
   checks the call against the provider's records.

[architecture.md](architecture.md) has a diagram for each provider's flow.

### Rules the code depends on

Break one of these and you produce a consent nobody can defend:

- **A consent is never edited.** A new decision supersedes the old row;
  `is_current` moves. Only one current consent exists per person and purpose,
  and a unique index enforces it.
- **Consent, audit event and outbox row commit together or not at all.**
- **Commit before confirming.** FastAPI commits the request's session *after*
  the response is sent, so any route that tells a caller "recorded" must call
  `db.commit()` first. Tests check this for all three providers.
- **Silence is never consent.** Only keys 1, 2 and 9 create a row; everything
  else records a session outcome.
- **The notice is pinned when the call starts**, so a notice republished
  mid-call can't change what the caller is recorded as having heard.
- **Secrets never reach a log, a response or git.** Sprinklr tokens and
  name/email values are encrypted at rest (`app/crypto.py`).

## Common tasks

### Add a purpose or a new notice wording

Published notices never change: a new wording is a new version. See
[How to add a purpose or a new notice version](../README.md#how-to-add-a-purpose-or-a-new-notice-version).

### Add a database migration

Add the next numbered file in `migrations/` (for example
`006_something.sql`) and mirror any new columns in `app/models.py`. The SQL file
is the source of truth.

The migrations are **not re-runnable**: `apply_migrations()` runs every file,
and `001` fails on an existing schema. On your dev database, apply only the new
file:

```bash
docker exec -i ivr-qa-pg psql -U postgres -d ivr_consent_dev < migrations/006_something.sql
```

The test suite always starts from an empty database and runs them all.

### Add a telephony provider

Sprinklr was the most recent; `git show d450f3f --stat` lists every file it
touched. The checklist:

1. `app/telephony/<name>.py`: a provider class with `verify()` and `parse()`,
   registered in `app/telephony/__init__.py`.
2. `app/routes_<name>.py`, included in `app/main.py`. Use `ingest()` for every
   request, and commit before any "recorded" response.
3. A migration that adds the name to the `provider` CHECK constraints on
   `ivr_session` and `consent` (see `migrations/004_sprinklr.sql`).
4. Allow the name in `SessionCreate.provider` in `app/api.py`.
5. Console: the `Provider` type in `ui/src/lib/api.ts`, the filter in
   `pages/Sessions.tsx`, and the authenticity text in `pages/ConsentDetail.tsx`
   and `pages/Dashboard.tsx`.
6. A call-details lookup in `app/reconcile.py`, so its calls get reconciled.
7. Tests modelled on `tests/test_sprinklr.py`: valid, missing and wrong auth;
   silence; replay; failed commit.

### Investigate a disputed consent

1. `GET /v1/consents/{id}/evidence` (or the console detail page) returns
   everything: the notice text and hash, the call, the recordings, and the
   person's full hash chain with `verified: true|false`.
2. `webhook_receipt` rows for the session show what each provider sent, and
   whether it authenticated.
3. A `call.reconciled` event in the chain records what the provider's own
   records said about the call.

### Find out why a consent didn't reach UCM

Check `GET /api/console/health`, then the `ucm_outbox` row for the consent:

- `paused = true` with `last_error` starting `reconciliation:` means the call
  didn't match the provider's records. Investigate it before releasing the row.
- Any other `paused = true` means UCM rejected the payload (a 4xx).
- `attempts` rising, with `next_attempt_at` in the future, means UCM is down;
  the worker keeps retrying.

### Connect the Sprinklr API

See [Sprinklr OAuth setup](../README.md#sprinklr-oauth-setup). It can't
complete until Sprinklr Support activates the app for a real environment.

## Things that catch people out

- **Exotel wraps the digit in quotes.** A keypress of 1 arrives as `"1"`
  (`%221%22`). `trim_digits()` handles it; don't parse Exotel digits yourself.
- **Twilio signatures cover the public URL.** Behind a proxy the app sees
  `http://`; `PUBLIC_BASE_URL` must be the external `https://` origin, or every
  signature check fails.
- **Phone numbers come in every shape.** Always go through
  `identity.normalise_e164()`, or one person splits into two with contradictory
  consents.
- **Sprinklr allows one API token per key.** Connecting a second deployment
  with the same app logs the first one out.
- **A repeated `call_id` returns the first decision.** That's replay
  protection, not a bug. Use a fresh ID for each manual test call.
- **The console in dev:** `cd ui && npm run dev` serves it on port 5173. Set
  `CORS_ORIGINS=http://localhost:5173` for the API and `VITE_API_BASE` (in
  `ui/.env`) to the API origin.

## Where to find answers

| Question | Look in |
|---|---|
| What does endpoint X take and return? | [README Reference](../README.md#reference) |
| Why is it built this way? | [architecture.md, "Why it's built this way"](architecture.md#why-its-built-this-way) |
| What's known to be missing? | [architecture.md, "Not yet implemented"](architecture.md#not-yet-implemented) and [TODOS.md](../TODOS.md) |
| ID-PRIVACY® platform and MongoDB integration | [id-privacy-integration.md](id-privacy-integration.md) |
| What's still open with Sprinklr? | `TODO(sprinklr)` in the code (`grep -rn "TODO(sprinklr)" app`), and the Sprinklr section of TODOS.md |
| Exotel network allowlist ranges | Exotel support, `hello@exotel.com` (they aren't published) |
| Sprinklr app activation, environment, API questions | Sprinklr Support, via the developer portal |
