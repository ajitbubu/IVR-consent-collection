-- IVR consent capture: initial schema
-- Postgres 14+

CREATE TYPE consent_decision   AS ENUM ('granted','declined','withdrawn');
CREATE TYPE consent_status     AS ENUM ('active','declined','withdrawn','expired','superseded');
CREATE TYPE sync_state         AS ENUM ('pending','synced','failed','skipped');
CREATE TYPE consent_channel    AS ENUM ('ivr_inbound','ivr_outbound','web','app','agent');
CREATE TYPE verification_level AS ENUM ('ani_only','verified');

-- ---------------------------------------------------------------- who

CREATE TABLE data_principal (
    id           CHAR(26) PRIMARY KEY,
    phone_e164   TEXT        NOT NULL UNIQUE,
    phone_hash   BYTEA       NOT NULL,
    external_ref TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX data_principal_phone_hash_idx ON data_principal (phone_hash);

CREATE TABLE identity_attribute (
    id                CHAR(26) PRIMARY KEY,
    data_principal_id CHAR(26) NOT NULL REFERENCES data_principal(id),
    attr              TEXT     NOT NULL CHECK (attr IN ('name','email','phone_alt')),
    value_enc         BYTEA    NOT NULL,
    source            TEXT     NOT NULL CHECK (source IN ('network','crm','asr','self_declared','agent')),
    confidence        NUMERIC(3,2),
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_live_attr
    ON identity_attribute (data_principal_id, attr) WHERE superseded_at IS NULL;

-- ---------------------------------------------------------------- what they were asked

CREATE TABLE purpose (
    id                    CHAR(26) PRIMARY KEY,
    code                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    retention_days        INT  NOT NULL,
    requires_verification BOOLEAN NOT NULL DEFAULT FALSE,
    ucm_purpose_key       TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE notice_version (
    id           CHAR(26) PRIMARY KEY,
    purpose_id   CHAR(26) NOT NULL REFERENCES purpose(id),
    version      INT      NOT NULL,
    language     CHAR(3)  NOT NULL,
    body_text    TEXT     NOT NULL,
    body_sha256  BYTEA    NOT NULL,
    audio_url    TEXT,
    published_at TIMESTAMPTZ,
    retired_at   TIMESTAMPTZ,
    UNIQUE (purpose_id, version, language)
);

-- ---------------------------------------------------------------- the call

CREATE TABLE ivr_session (
    id                CHAR(26) PRIMARY KEY,
    call_sid          TEXT UNIQUE,
    direction         consent_channel NOT NULL,
    data_principal_id CHAR(26) REFERENCES data_principal(id),
    purpose_id        CHAR(26) REFERENCES purpose(id),
    notice_version_id CHAR(26) REFERENCES notice_version(id),
    language          CHAR(3) NOT NULL DEFAULT 'eng',
    from_number       TEXT,
    to_number         TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at          TIMESTAMPTZ,
    call_status       TEXT,
    answered_by       TEXT,
    outcome           TEXT NOT NULL DEFAULT 'in_progress',
    reconciled_at     TIMESTAMPTZ
);
CREATE INDEX ivr_session_unreconciled_idx
    ON ivr_session (started_at) WHERE reconciled_at IS NULL;

CREATE TABLE call_artifact (
    id             CHAR(26) PRIMARY KEY,
    ivr_session_id CHAR(26) NOT NULL REFERENCES ivr_session(id),
    kind           TEXT NOT NULL CHECK (kind IN ('recording','transcript','dtmf_log')),
    storage_uri    TEXT,
    sha256         BYTEA,
    bytes          BIGINT,
    captured_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    purge_after    TIMESTAMPTZ NOT NULL,
    purged_at      TIMESTAMPTZ
);

-- ---------------------------------------------------------------- the decision

CREATE TABLE consent (
    id                 CHAR(26) PRIMARY KEY,
    data_principal_id  CHAR(26) NOT NULL REFERENCES data_principal(id),
    purpose_id         CHAR(26) NOT NULL REFERENCES purpose(id),
    notice_version_id  CHAR(26) NOT NULL REFERENCES notice_version(id),
    ivr_session_id     CHAR(26) REFERENCES ivr_session(id),
    decision           consent_decision   NOT NULL,
    status             consent_status     NOT NULL,
    channel            consent_channel    NOT NULL,
    language           CHAR(3)            NOT NULL,
    verification_level verification_level NOT NULL,
    decided_at         TIMESTAMPTZ NOT NULL,
    expires_at         TIMESTAMPTZ,
    is_current         BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_by      CHAR(26) REFERENCES consent(id),
    ucm_sync_state     sync_state NOT NULL DEFAULT 'pending',
    ucm_consent_ref    TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one current row per (principal, purpose). Contradictory state
-- cannot be written, so it never has to be resolved. The flag rather than
-- superseded_by carries the index because the pointer can only be set after
-- the new row exists, and the index has to hold during the insert.
CREATE UNIQUE INDEX one_current_consent
    ON consent (data_principal_id, purpose_id) WHERE is_current;

-- One decision per call per purpose: makes webhook replay a no-op.
CREATE UNIQUE INDEX one_decision_per_call
    ON consent (ivr_session_id, purpose_id) WHERE ivr_session_id IS NOT NULL;

CREATE INDEX unsynced_consent ON consent (ucm_sync_state) WHERE ucm_sync_state <> 'synced';
CREATE INDEX expiring_consent ON consent (expires_at) WHERE status = 'active' AND is_current;

-- ---------------------------------------------------------------- the proof

CREATE TABLE consent_event (
    seq            BIGSERIAL PRIMARY KEY,
    chain_key      CHAR(26) NOT NULL,           -- data_principal_id
    consent_id     CHAR(26) REFERENCES consent(id),
    ivr_session_id CHAR(26) REFERENCES ivr_session(id),
    event_type     TEXT  NOT NULL,
    payload        JSONB NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash      BYTEA,
    entry_hash     BYTEA NOT NULL
);
CREATE INDEX consent_event_chain_idx ON consent_event (chain_key, seq);
CREATE INDEX consent_event_consent_idx ON consent_event (consent_id, seq);
-- Two writers cannot extend the same chain from the same point.
CREATE UNIQUE INDEX consent_event_no_fork ON consent_event (chain_key, entry_hash);

-- ---------------------------------------------------------------- the handoff

CREATE TABLE ucm_outbox (
    id                BIGSERIAL PRIMARY KEY,
    consent_id        CHAR(26) NOT NULL REFERENCES consent(id),
    data_principal_id CHAR(26) NOT NULL REFERENCES data_principal(id),
    decided_at        TIMESTAMPTZ NOT NULL,
    idempotency_key   TEXT     NOT NULL UNIQUE,
    payload           JSONB    NOT NULL,
    attempts          INT      NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error        TEXT,
    paused            BOOLEAN NOT NULL DEFAULT FALSE,
    delivered_at      TIMESTAMPTZ
);
CREATE INDEX pending_outbox
    ON ucm_outbox (data_principal_id, decided_at) WHERE delivered_at IS NULL;

-- ---------------------------------------------------------------- roles
-- The application role may append to the audit trail but never rewrite it.
-- (Run under a migration identity the app does not hold at runtime.)
