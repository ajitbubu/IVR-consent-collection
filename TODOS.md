# TODOS

## Webhooks

### Route Exotel /status through the shared receipt path

**What:** Make `POST /exotel/status` go through `webhook_common.ingest()` and drop `routes_exotel._find_session` in favour of `webhook_common.find_session`.

**Why:** Every other webhook writes a `webhook_receipt` row. The Exotel status callback brings AnsweredBy and the RecordingUrl, so it is evidence too, but it leaves no receipt.

**Pros:** Every inbound webhook gets a receipt, and one duplicate session-lookup helper goes away.

**Cons:** Small. `ingest()` reads the form body, but Exotel can send JSON here, so `read_request` has to return the parsed JSON params for that content type.

**Context:** `app/routes_exotel.py:182-216` parses form/JSON by hand. `app/routes_exotel.py:36-45` duplicates `app/webhook_common.py:84-93`. Raised by /plan-eng-review on 2026-10-01 (finding #8). Start with a test that a status callback writes a receipt.

**Effort:** S
**Priority:** P3
**Depends on:** None

## Console

### Console has no favicon

**What:** Add a favicon to `ui/index.html` (for example `<link rel="icon" href="/console/favicon.svg">`) and serve it from `ui/public/`.

**Why:** Every console page logs a 404 for `/favicon.ico`, which adds noise to the browser console and to QA console-error counts.

**Context:** Found by /qa on 2026-10-02 (ISSUE-007, low). The new sidebar logo `ui/src/assets/dsg_white.svg` is white-on-transparent, so it would disappear on a light browser tab. Use a dark or shield-only variant for the favicon.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Not-found consent page blames the network

**What:** When a deep link points to an unknown consent ID, show "No consent with this ID" on the console detail page instead of "Could not load: unknown consent. Check that the consent service is reachable."

**Why:** The service answered with a 404, so the reachability hint sends the operator looking for an outage that doesn't exist.

**Context:** Found by /qa on 2026-10-01 (ISSUE-003, low, content). To reproduce: open `/console/consents/01ZZZZZZZZZZZZZZZZZZZZZZZZ`. The message comes from the shared `Failed` component in `ui/src/components/bits.tsx`. It needs a not-found variant, chosen when the API status is 404.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Make partial phone search use an index

**What:** Replace the `phone_e164 LIKE '%q%'` search in the console with an indexed search: a `pg_trgm` GIN index, or a suffix search on a reversed-phone btree index.

**Why:** The contains-search reads every `data_principal` row. That's fine at pilot volume, but it gets slow at millions of people.

**Pros:** Console search stays fast at any volume.

**Cons:** Adds a migration and an index to maintain. `pg_trgm` is an extension that has to be allowed on the target Postgres.

**Context:** `app/console_api.py:80`. Only btree/unique indexes exist on `phone_e164` and `phone_hash` (`migrations/001_init.sql`). Raised by /plan-eng-review on 2026-10-01 (finding #10). Do this when console search latency becomes noticeable.

**Effort:** S
**Priority:** P3
**Depends on:** None
