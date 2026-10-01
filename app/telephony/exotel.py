"""Exotel provider.

Two facts drive everything here:

  * Exotel does not sign its webhooks -- no HMAC, no shared secret, nothing
    documented. Authenticity is network-level only.
  * The digits parameter arrives wrapped in literal double-quote characters.
"""
from __future__ import annotations

import datetime as dt

from app.telephony.base import Verification, WebhookEvent

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Exotel sends this constant in EndTime on every Passthru. It is the Unix
# epoch in IST and is not the end of the call.
FROZEN_END_TIME = "1970-01-01 05:30:00"

_QUOTES = '"“”\' '


def trim_digits(raw: str | None) -> str | None:
    """Exotel's own docs: the value comes with a double quote before and
    after the number, and must be trimmed to get the actual digits. A
    keypress of 1 arrives on the wire as %221%22."""
    if raw is None:
        return None
    cleaned = raw.strip().strip(_QUOTES).strip()
    return cleaned or None


def parse_exotel_time(raw: str | None) -> dt.datetime | None:
    if not raw or raw.strip() == FROZEN_END_TIME:
        return None
    try:
        naive = dt.datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=IST)


class ExotelProvider:
    name = "exotel"

    def verify(self, *, url: str, headers: dict, form: dict, body: bytes) -> Verification:
        # Nothing to check. The endpoint is protected by an IP allowlist and
        # every consent is corroborated against the Call Details API before
        # it is trusted or pushed to UCM.
        return Verification(ok=True, signature=None, reason="provider does not sign webhooks")

    def parse(self, *, form: dict, query: dict) -> WebhookEvent:
        p = {**query, **form}
        direction = (p.get("Direction") or "").lower()
        return WebhookEvent(
            provider=self.name,
            call_ref=p.get("CallSid"),
            session_ref=(p.get("CustomField") or "").strip() or None,
            from_number=p.get("CallFrom") or p.get("From"),
            to_number=p.get("CallTo") or p.get("To"),
            direction="ivr_inbound" if direction.startswith("incoming") else "ivr_outbound",
            digits=trim_digits(p.get("digits")),
            occurred_at=parse_exotel_time(p.get("CurrentTime")),
            call_status=p.get("Status") or p.get("CallStatus"),
            answered_by=_exotel_answered_by(p),
            recording_url=p.get("RecordingUrl"),
            ended_at=parse_exotel_time(p.get("EndTime")),
            raw=p,
        )

    # Exotel's Greeting applet takes plain text only, and requires the server
    # to answer HEAD with the same headers as GET.
    def notice_response(self, text: str) -> tuple[str, str]:
        return text, "text/plain"

    def readback_response(self, text: str) -> tuple[str, str]:
        return text, "text/plain"

    def decision_response(self, *, committed: bool, next_url: str | None = None):
        # Passthru routes on the status code alone: 200 is branch A, 302 is
        # branch B. The caller is never told a consent was recorded when the
        # write failed.
        return ("", "text/plain")


def _exotel_answered_by(p: dict) -> str | None:
    legs = p.get("Legs")
    if isinstance(legs, list) and len(legs) > 1 and isinstance(legs[1], dict):
        return legs[1].get("AnsweredBy")
    return None
