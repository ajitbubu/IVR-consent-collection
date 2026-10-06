-- Background evidence jobs: the recording fetcher and call reconciliation.

-- The provider's URL is only where the fetcher copies from; storage_uri is
-- where the evidence lives. Attempts are capped so a dead link cannot spin.
ALTER TABLE call_artifact ADD COLUMN source_url     TEXT;
ALTER TABLE call_artifact ADD COLUMN fetch_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE call_artifact ADD COLUMN last_error     TEXT;
CREATE INDEX call_artifact_unfetched_idx
    ON call_artifact (captured_at) WHERE storage_uri IS NULL AND purged_at IS NULL;

-- What the provider's call-details API said about the call: 'ok', or the
-- reason it did not corroborate the webhooks.
ALTER TABLE ivr_session ADD COLUMN reconcile_result TEXT;
