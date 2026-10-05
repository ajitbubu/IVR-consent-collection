-- Sprinklr REST API access token. Sprinklr allows one token per API key, and
-- each refresh token works once, so there is exactly one row per environment
-- and every refresh happens under a row lock.
CREATE TABLE sprinklr_oauth_token (
    env               TEXT PRIMARY KEY,
    access_token_enc  BYTEA       NOT NULL,
    refresh_token_enc BYTEA       NOT NULL,
    issued_at         TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
