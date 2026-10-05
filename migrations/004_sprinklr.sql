-- Sprinklr as a third telephony provider. Its IVR flow calls our JSON
-- endpoints from HTTP nodes; the consent model does not change.

ALTER TABLE ivr_session DROP CONSTRAINT ivr_session_provider_check;
ALTER TABLE ivr_session ADD CONSTRAINT ivr_session_provider_check
    CHECK (provider IN ('exotel','twilio','sprinklr'));
ALTER TABLE consent DROP CONSTRAINT consent_provider_check;
ALTER TABLE consent ADD CONSTRAINT consent_provider_check
    CHECK (provider IN ('exotel','twilio','sprinklr'));
