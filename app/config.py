"""Configuration. Nothing secret has a default."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return v


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))

    # HMAC key for phone lookup hashes. Rotating this requires a rehash.
    phone_hmac_key: bytes = field(
        default_factory=lambda: _env("PHONE_HMAC_KEY", "dev-only-not-for-production").encode()
    )
    # Envelope encryption key for name/email at rest. In production this is a
    # KMS data key, not an env var.
    attribute_key: bytes = field(
        default_factory=lambda: _env("ATTRIBUTE_KEY", "dev-only-not-for-production").encode()
    )

    ucm_base_url: str = field(default_factory=lambda: _env("UCM_BASE_URL", "http://localhost:9999"))
    ucm_timeout_s: float = 5.0

    # Exotel
    exotel_sid: str = field(default_factory=lambda: _env("EXOTEL_SID", "unset"))
    exotel_api_key: str = field(default_factory=lambda: _env("EXOTEL_API_KEY", "unset"))
    exotel_api_token: str = field(default_factory=lambda: _env("EXOTEL_API_TOKEN", "unset"))
    # Mumbai cluster. The Singapore default (api.exotel.com) is the wrong one
    # for an India residency posture -- see the design doc.
    exotel_base_url: str = field(
        default_factory=lambda: _env("EXOTEL_BASE_URL", "https://api.in.exotel.com")
    )
    exotel_caller_id: str = field(default_factory=lambda: _env("EXOTEL_CALLER_ID", "unset"))

    # Twilio. Unlike Exotel, Twilio signs every webhook with this token --
    # rotating it means old receipts can no longer be re-verified, so record
    # which generation was in force if you rotate.
    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID", "unset"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN", "unset"))
    twilio_api_base: str = field(
        default_factory=lambda: _env("TWILIO_API_BASE", "https://api.twilio.com")
    )
    twilio_caller_id: str = field(default_factory=lambda: _env("TWILIO_CALLER_ID", "unset"))
    # Voice names are provider-prefixed (Polly.X / Google.locale-X) and the
    # per-locale list is only authoritative in the Twilio Console -- read the
    # en-IN / hi-IN names off the console rather than hardcoding a guess.
    twilio_voice: str = field(default_factory=lambda: _env("TWILIO_VOICE", ""))
    twilio_language: str = field(default_factory=lambda: _env("TWILIO_LANGUAGE", "en-IN"))
    # Reject a webhook whose signature does not verify. Only ever false in a
    # local harness; in production an unverified Twilio webhook is a forgery.
    twilio_enforce_signature: bool = field(
        default_factory=lambda: _env("TWILIO_ENFORCE_SIGNATURE", "true").lower() == "true"
    )
    # The externally visible base URL, used to rebuild the signed URL. Behind
    # a TLS-terminating proxy the framework reports http:// and validation
    # then fails every time.
    public_base_url: str = field(
        default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8088")
    )

    # Recordings and daily digests. Local filesystem for now; everything goes
    # through app/storage.py so an object store can replace it later.
    evidence_dir: str = field(default_factory=lambda: _env("EVIDENCE_DIR", "var/evidence"))

    default_provider: str = field(default_factory=lambda: _env("DEFAULT_PROVIDER", "exotel"))
    default_country_code: str = "91"
    outbox_max_attempts: int = 12
    outbox_base_backoff_s: int = 5
    outbox_max_backoff_s: int = 900
    recording_max_attempts: int = 12
    # A call younger than this may still be in progress at the provider.
    reconcile_after_s: int = 300


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test hook."""
    global _settings
    _settings = None
