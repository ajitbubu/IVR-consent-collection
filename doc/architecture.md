# IVR Consent Capture: architecture and flows

As built at `a19b1e6` (2026-10-01). A caller hears a versioned DPDP notice,
presses a key, and the decision is stored in Postgres as the write-ahead
record. A separate worker then pushes it to UCM, so a UCM outage can never
lose a consent.

How to run it and integrate it with the ID-PRIVACY® platform (including the
MongoDB port): [id-privacy-integration.md](id-privacy-integration.md).

## Components

```mermaid
flowchart LR
    exotel["Exotel (India)<br/>unsigned, IP allowlist"]
    twilio["Twilio (elsewhere)<br/>HMAC-SHA1 signed"]
    svc["Internal services"]
    ops["Ops / DPO browser"]

    subgraph app["FastAPI app (app/main.py)"]
        rex["routes_exotel.py<br/>/exotel/identify, notice,<br/>decision, readback, status"]
        rtw["routes_twilio.py<br/>/twilio/voice, decision,<br/>readback, status, recording"]
        ingest["webhook_common.ingest()<br/>verify, parse, find_session,<br/>write webhook_receipt"]
        api["api.py<br/>/v1/*"]
        console["console_api.py + ui/<br/>/api/console/*, /console"]
        cs["consent_service.py<br/>state machine"]
        hc["hashchain.py<br/>append_event()"]
    end

    subgraph pg["Postgres"]
        tx[["One transaction per decision:<br/>consent + consent_event + ucm_outbox"]]
        tables[("data_principal, ivr_session,<br/>consent, consent_event,<br/>ucm_outbox, webhook_receipt,<br/>call_artifact")]
    end

    worker["app/worker.py<br/>outbox.drain() every 2s"]
    ucm["UCM<br/>POST /v1/consents<br/>Idempotency-Key: consent_id"]

    exotel -- GET --> rex
    twilio -- POST --> rtw
    rex --> ingest
    rtw --> ingest
    ingest --> cs
    svc --> api --> cs
    ops --> console --> tables
    cs --> hc
    cs --> tx
    hc --> tx
    tx --> tables
    tables --> worker --> ucm
```

| Module | Role |
|---|---|
| `app/routes_exotel.py`, `app/routes_twilio.py` | Provider webhooks; translate call events into consent-service calls |
| `app/telephony/` | Provider-neutral interface: signature verification, parsing, response bodies |
| `app/webhook_common.py` | Rebuild signed URL, verify, write `webhook_receipt`, resolve session |
| `app/consent_service.py` | Consent state machine: sessions, decisions, supersession, outbox row |
| `app/hashchain.py` | Per-principal append-only hash chain |
| `app/outbox.py`, `app/worker.py`, `app/ucm.py` | Ordered, retried delivery to UCM |
| `app/evidence.py` | Recording artifacts, evidence bundle, daily chain digest |
| `app/api.py` | Service API (`/v1`) |
| `app/console_api.py`, `ui/` | Read-only operator and DPO console |

## Call flow: Exotel (India)

```mermaid
sequenceDiagram
    autonumber
    actor C as Caller
    participant X as Exotel flow
    participant S as IVR service
    participant DB as Postgres

    C->>X: dials / answers
    X->>S: GET /identify (Passthru, async)
    S->>S: create_session() if none: normalise phone, lock principal
    S->>DB: ivr_session with LIVE notice version pinned
    S->>DB: consent_event session.created
    S->>S: attach_call(CallSid)
    X->>S: HEAD + GET /notice
    S-->>X: notice text (text/plain)
    S->>DB: consent_event notice.served
    X-->>C: TTS notice
    C->>X: presses 1
    X->>S: GET /decision?digits="1" (Passthru, sync)
    S->>DB: webhook_receipt
    S->>S: trim quotes, "1" becomes 1
    Note over S,DB: record_decision(), one transaction
    S->>DB: lock principal FOR UPDATE
    alt replayed webhook
        S->>S: return existing consent (no-op)
    else new decision
        S->>DB: previous current consent: is_current = false
        S->>DB: insert consent (is_current = true)
        S->>DB: consent_event consent.granted
        S->>DB: ucm_outbox row (payload built now)
    end
    alt committed
        S-->>X: 200 (branch A)
    else no input / bad key / error
        S-->>X: 302 (branch B), no consent
    end
    X->>S: GET /readback
    S-->>X: "You have agreed to X. Your reference is ABC123."
    X-->>C: readback
    C->>X: hangs up
    X->>S: POST /status (delivery not guaranteed)
    S->>DB: call_status, AnsweredBy, RecordingUrl to call_artifact
```

## Call flow: Twilio (outside India)

```mermaid
sequenceDiagram
    autonumber
    participant T as Twilio
    participant S as IVR service
    participant DB as Postgres

    T->>S: POST /twilio/voice?session=ID + X-Twilio-Signature
    S->>DB: webhook_receipt (always, even if rejected)
    alt bad or missing signature
        S-->>T: 403
    else signature valid
        S-->>T: TwiML Gather numDigits=1, actionOnEmptyResult, notice
    end
    T->>S: POST /twilio/decision?session=ID Digits=1
    alt AnsweredBy is machine
        S->>DB: session.no_decision (answering_machine)
        S-->>T: Say Goodbye, Hangup
    else no digits
        S->>DB: session.no_decision (no_input)
        S-->>T: Say nothing has changed, Hangup
    else key pressed
        S->>DB: record_decision() (same as Exotel)
        S-->>T: TwiML Redirect to /twilio/readback
    end
    T->>S: POST /twilio/readback
    S-->>T: Say "You have agreed to X. Ref ...", Hangup
    T->>S: POST /twilio/status, /twilio/recording (signed)
```

## Consent state machine (per principal + purpose)

```mermaid
stateDiagram-v2
    [*] --> NoDecision: call answered

    NoDecision --> Active: key 1 (granted)
    NoDecision --> Declined: key 2 (declined)
    NoDecision --> Withdrawn: key 9 or POST /v1/consents/withdraw
    NoDecision --> [*]: silence, timeout, bad key, machine (no consent row)

    Active --> Superseded: new decision for same purpose
    Declined --> Superseded: new decision for same purpose
    Withdrawn --> Superseded: new decision for same purpose
    Active --> Expired: decided_at + retention_days passes

    note right of Active
        permits_processing =
        is_current AND status = active
        AND not expired
    end note
    note right of Superseded
        is_current = false, superseded_by = new id.
        Never deleted. DB indexes enforce one current
        consent per purpose and one decision per call.
    end note
```

## Audit hash chain

One chain per data principal (`chain_key = data_principal_id`).
`entry_hash = SHA256(prev_hash || event_type || canonical_json(payload) || UTC ISO occurred_at)`.

```mermaid
flowchart LR
    e1["seq 1<br/>session.created<br/>prev = null"] -- h1 --> e2["seq 2<br/>notice.served<br/>prev = h1"]
    e2 -- h2 --> e3["seq 3<br/>consent.granted<br/>prev = h2"]
    e3 -- h3 --> e4["seq 4 ...<br/>prev = h3"]
    e4 -. "verify_chain() walks and recomputes" .-> e1
    e4 --> digest[("daily_digest(day)<br/>folds every touched chain head<br/>into one SHA256 for WORM storage")]
```

Writers hold the principal row lock, so two writers cannot fork the chain. `UNIQUE(chain_key, entry_hash)` backs that up.

## Outbox to UCM

```mermaid
flowchart TD
    loop["worker loop, every 2s"] --> claim["claim_batch()<br/>undelivered, not paused, next_attempt_at <= now<br/>ORDER BY principal, decided_at, id<br/>FOR UPDATE SKIP LOCKED"]
    claim --> head["keep first row per principal (max 50)"]
    head --> push["deliver_one(): POST UCM /v1/consents<br/>Idempotency-Key = consent_id"]
    push --> ok{"UCM response"}
    ok -- "200 / 201 / 409" --> synced["delivered_at set<br/>ucm_sync_state = synced"]
    ok -- "5xx / 429 / timeout" --> retry["attempts + 1<br/>backoff 5s doubling to 900s"]
    retry --> max{"attempts >= 12?"}
    max -- no --> loop
    max -- yes --> failed["ucm_sync_state = failed<br/>keep retrying every 900s"]
    failed --> loop
    ok -- "other 4xx" --> paused["paused = true<br/>ucm_sync_state = failed<br/>operator must unpause"]
```

## Why it's built this way

A consent is evidence: months later someone will ask *what exactly did this
person agree to, when, and how do you know?* Each design choice below exists
because the simpler alternative loses that answer.

### The consent is written here first, and delivered to UCM later

**Problem.** If the call handler pushed straight to UCM, a UCM outage or slow
response during a call would either lose the consent or keep the caller
waiting (Exotel's decision Passthru and Twilio's 15-second limit both run
while the caller holds).

**Approach.** The consent row, its audit event and an outbox row commit in one
database transaction (`consent_service.record_decision`). A separate worker
(`app/outbox.py`) delivers to UCM with the consent ID as `Idempotency-Key`, and
retries with backoff for as long as needed.

**Trade-off.** UCM is seconds behind (minutes during an outage), so anyone
reading UCM directly sees a short lag. We monitor `outbox_lag_seconds` for this.

### Decisions are superseded, never edited

**Problem.** Updating a consent row in place erases the grant you relied on
before a withdrawal: exactly what a regulator or a court asks to see.

**Approach.** Every decision is a new row. The previous current row gets
`is_current=false` and `superseded_by`. A partial unique index guarantees one
current consent per person and purpose at every instant.

**Trade-off.** More rows and a two-step swap inside the transaction (clear
the flag, insert, then point), in exchange for a history that can't be lost.

### The notice version is pinned when the call starts

**Problem.** If a notice is republished mid-call, "the notice at decision
time" isn't what the caller heard.

**Approach.** `create_session` pins the live notice version. The consent
stores it, the receipt prints its SHA-256, and published notices are immutable
(a change is a new version).

**Trade-off.** Fixing a typo needs a new version rather than an edit.

### One hash chain per person

**Problem.** An audit log that can be silently rewritten proves nothing. A
single global chain would make every write contend for one lock, and every
verification re-hash the whole system's history.

**Approach.** Each person's events form their own chain
(`entry_hash = SHA256(prev ‖ type ‖ canonical JSON ‖ UTC time)`), written while
that person's row is locked. Verification cost is bounded by one person's
history. Times are hashed in UTC because Postgres renders timestamps in the
connection's time zone; hashing the rendering voided chains read from another
zone (`test_hash_is_timezone_independent`).

**Trade-off.** Chains prove internal consistency only. Tamper-evidence
against someone with database write access needs the daily digest anchored to
write-once storage, which is not running yet ([below](#not-yet-implemented)).

### Silence is never consent, and replays are no-ops

**Problem.** Telephony gives you timeouts, hang-ups, voicemail, wrong keys and
duplicate webhooks. Any of them turned into a grant is a fabricated consent.

**Approach.** Only keys 1, 2 and 9 create a row; everything else records a
session `outcome`. Twilio's `<Gather>` uses `actionOnEmptyResult` so silence is
recorded rather than falling through. A unique index on (session, purpose)
makes a replayed webhook return the original consent.

**Trade-off.** A caller who presses another key on the same call keeps their
first decision; changing it takes a new call.

### Two providers, one state machine

**Problem.** Exotel and Twilio differ in everything at the edge: signing
(none vs HMAC), digit encoding (quoted vs plain), response format (status
codes vs TwiML).

**Approach.** `app/telephony/` isolates verification, parsing and responses
per provider. The consent service never sees provider details, and every
inbound request is stored as a `webhook_receipt`. For Twilio that includes the
signature, so each consent can be re-verified later.

**Trade-off.** Exotel's unsigned webhooks give a weaker evidence chain. That
is why its routes need an IP allowlist and Call Details corroboration (see the
README's *Two things to know before deploying*).

## Not yet implemented

Comments in the code describe these controls, but no code enforces them yet:

- **Exotel corroboration.** Nothing checks an Exotel consent against the Call Details API before it goes to UCM. The only protection today is the IP allowlist at the edge.
- **`/v1` and console authentication.** mTLS and the separate hostnames are expected at deployment; the app itself performs no auth check.
- **Daily digest anchoring.** `daily_digest()` exists, but nothing schedules it or writes the result to WORM storage.
- **Append-only audit tables.** The database doesn't enforce append-only on `consent_event` and `webhook_receipt`.
- **Outbox ordering under retries.** `claim_batch` filters out rows that aren't due before it picks each principal's oldest row. So a grant waiting to retry can be overtaken by a later withdrawal.
