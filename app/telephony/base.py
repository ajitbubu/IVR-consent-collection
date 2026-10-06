"""Provider-neutral telephony interface.

The consent state machine knows nothing about Exotel or Twilio. Everything
that differs between them -- how a keypress is encoded, whether the webhook
is signed, what a response body has to look like -- lives behind this.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class WebhookEvent:
    """One normalised inbound webhook, whatever sent it."""

    provider: str
    call_ref: str | None            # CallSid on both, as it happens
    session_ref: str | None         # our ivr_session.id
    from_number: str | None
    to_number: str | None
    direction: str | None           # ivr_inbound | ivr_outbound
    digits: str | None
    occurred_at: dt.datetime | None
    call_status: str | None = None
    answered_by: str | None = None
    recording_url: str | None = None
    ended_at: dt.datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Verification:
    """Outcome of checking a webhook's authenticity."""

    ok: bool
    signature: str | None
    reason: str

    @property
    def cryptographic(self) -> bool:
        """True when the provider actually signs. False means the only
        controls are network-level, and the record needs corroborating."""
        return self.reason not in ("provider does not sign webhooks",)


@dataclass
class CallDetails:
    """What the provider's authenticated call-details API says happened."""

    status: str | None
    from_number: str | None
    to_number: str | None
    answered_by: str | None = None
    ended_at: dt.datetime | None = None


class CallLookupUnavailable(Exception):
    """The provider API could not answer now (network, 5xx, 429, auth).
    Try again later; this says nothing about the call."""


class TelephonyProvider(Protocol):
    name: str

    def verify(self, *, url: str, headers: dict, form: dict, body: bytes) -> Verification: ...

    def parse(self, *, form: dict, query: dict) -> WebhookEvent: ...

    def notice_response(self, text: str) -> tuple[str, str]:
        """(body, content_type) for the applet that reads the notice out."""

    def decision_response(self, *, committed: bool, next_url: str | None = None) -> tuple[str, str]:
        """(body, content_type) telling the provider what to do next."""

    def readback_response(self, text: str) -> tuple[str, str]: ...


_REGISTRY: dict[str, TelephonyProvider] = {}


def register(provider: TelephonyProvider) -> None:
    _REGISTRY[provider.name] = provider


def get(name: str) -> TelephonyProvider:
    if name not in _REGISTRY:
        raise KeyError(f"unknown telephony provider {name!r}")
    return _REGISTRY[name]


def names() -> list[str]:
    return sorted(_REGISTRY)
