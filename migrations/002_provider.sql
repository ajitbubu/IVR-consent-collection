-- Multi-provider: Exotel for India, Twilio for everywhere else.
-- The consent model is provider-agnostic; only the webhook layer differs.

ALTER TABLE ivr_session ADD COLUMN provider TEXT NOT NULL DEFAULT 'exotel'
    CHECK (provider IN ('exotel','twilio'));
ALTER TABLE consent     ADD COLUMN provider TEXT NOT NULL DEFAULT 'exotel'
    CHECK (provider IN ('exotel','twilio'));

-- Twilio signs every webhook. Keeping the URL, the signature and the raw body
-- alongside the session makes each consent independently re-verifiable later:
-- it is the closest thing to a tamper-evident artifact the transport offers.
-- Exotel signs nothing, so these stay null there and corroboration against the
-- Call Details API carries the weight instead.
CREATE TABLE webhook_receipt (
    id             CHAR(26) PRIMARY KEY,
    ivr_session_id CHAR(26) REFERENCES ivr_session(id),
    provider       TEXT NOT NULL,
    route          TEXT NOT NULL,
    request_url    TEXT NOT NULL,
    signature      TEXT,
    signature_ok   BOOLEAN,
    body_sha256    BYTEA,
    params         JSONB NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX webhook_receipt_session_idx ON webhook_receipt (ivr_session_id, received_at);
CREATE INDEX webhook_receipt_unverified_idx
    ON webhook_receipt (received_at) WHERE signature_ok IS NOT TRUE;

CREATE INDEX consent_decided_at_idx ON consent (decided_at DESC);
CREATE INDEX ivr_session_started_at_idx ON ivr_session (started_at DESC);
