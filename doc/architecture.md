# IVR Consent Capture: architecture and flows

As built at `a19b1e6` (2026-10-01). A caller hears a versioned DPDP notice,
presses a key, and the decision is stored in Postgres as the write-ahead
record. A separate worker then pushes it to UCM, so a UCM outage can never
lose a consent.

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

## Not yet implemented

Comments in the code describe these controls, but no code enforces them yet:

- **Exotel corroboration.** Nothing checks an Exotel consent against the Call Details API before it goes to UCM. The only protection today is the IP allowlist at the edge.
- **`/v1` and console authentication.** mTLS and the separate hostnames are expected at deployment; the app itself performs no auth check.
- **Daily digest anchoring.** `daily_digest()` exists, but nothing schedules it or writes the result to WORM storage.
- **Append-only audit tables.** The database doesn't enforce append-only on `consent_event` and `webhook_receipt`.
- **Outbox ordering under retries.** `claim_batch` filters out rows that aren't due before it picks each principal's oldest row. So a grant waiting to retry can be overtaken by a later withdrawal.
