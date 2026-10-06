"""Twilio provider.

The contrast with Exotel that matters: Twilio signs every webhook. The
signature is what lets you later prove a consent keypress genuinely came
from Twilio and was not forged against a public endpoint, so it is stored
alongside the record rather than merely checked and discarded.

Implemented without the SDK so the algorithm is inspectable, and because the
SDK is one more dependency in the consent path.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from urllib.parse import urlparse, urlunparse
from xml.sax.saxutils import escape

import httpx

from app.config import settings
from app.telephony.base import CallDetails, CallLookupUnavailable, Verification, WebhookEvent


def compute_signature(auth_token: str, url: str, params: dict[str, str] | None) -> str:
    """HMAC-SHA1 over the full request URL with each POST parameter's name
    and value appended in sorted order, no delimiters, then base64.

    Documented worked example:
        https://example.com/myapp.php?foo=1&bar=2
        + CallSid... + Caller... + Digits... + From... + To...
    The query string stays on the URL and is signed as-is; values are the
    decoded values.
    """
    s = url
    if params:
        for name in sorted(params):
            value = params[name]
            if isinstance(value, (list, tuple)):
                for v in sorted(str(x) for x in value):
                    s += name + v
            else:
                s += name + ("" if value is None else str(value))
    mac = hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("utf-8").strip()


def _url_variants(url: str) -> list[str]:
    """Twilio's own SDK checks the URL with and without an explicit port,
    because signature generation on their side is inconsistent about it.
    Reimplementing without that produces intermittent, maddening failures."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    default_port = 443 if parsed.scheme == "https" else 80
    with_port = parsed._replace(netloc=f"{host}:{parsed.port or default_port}")
    without_port = parsed._replace(netloc=host)
    return [urlunparse(with_port), urlunparse(without_port), url]


class TwilioProvider:
    name = "twilio"

    def __init__(self, auth_token: str | None = None):
        self._auth_token = auth_token

    @property
    def auth_token(self) -> str:
        return self._auth_token or settings().twilio_auth_token

    def verify(self, *, url: str, headers: dict, form: dict, body: bytes) -> Verification:
        signature = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature")
        if not signature:
            return Verification(ok=False, signature=None, reason="missing X-Twilio-Signature")

        token = self.auth_token
        if not token or token == "unset":
            return Verification(ok=False, signature=signature, reason="no auth token configured")

        params: dict[str, str] | None = dict(form)
        parsed = urlparse(url)
        if "bodySHA256" in parsed.query:
            # JSON bodies: the body is not appended. Its SHA-256 rides in the
            # query string, which is itself covered by the signature.
            expected = dict(
                kv.split("=", 1) for kv in parsed.query.split("&") if "=" in kv
            ).get("bodySHA256", "")
            if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected):
                return Verification(ok=False, signature=signature, reason="bodySHA256 mismatch")
            params = None

        for candidate in _url_variants(url):
            if hmac.compare_digest(compute_signature(token, candidate, params), signature):
                return Verification(ok=True, signature=signature, reason="signature valid")
        return Verification(ok=False, signature=signature, reason="signature mismatch")

    def parse(self, *, form: dict, query: dict) -> WebhookEvent:
        p = {**query, **form}
        direction = (p.get("Direction") or "").lower()
        return WebhookEvent(
            provider=self.name,
            call_ref=p.get("CallSid"),
            session_ref=(p.get("session") or p.get("SessionId") or "").strip() or None,
            from_number=p.get("From") or p.get("Caller"),
            to_number=p.get("To"),
            direction="ivr_inbound" if direction == "inbound" else "ivr_outbound",
            # Plain, unquoted, and the finishOnKey character is already
            # stripped by Twilio. Applying Exotel's quote-trimming here would
            # happen to work today and break on any value that legitimately
            # starts with a quote -- so the providers parse separately.
            digits=(p.get("Digits") or "").strip() or None,
            occurred_at=_parse_rfc2822(p.get("Timestamp")),
            call_status=p.get("CallStatus"),
            answered_by=p.get("AnsweredBy"),
            recording_url=p.get("RecordingUrl"),
            ended_at=_parse_rfc2822(p.get("Timestamp")),
            raw=p,
        )

    def notice_response(self, text: str) -> tuple[str, str]:
        """A <Gather> wrapping the notice.

        numDigits=1 with finishOnKey="" submits on the single keypress with
        no hash needed; actionOnEmptyResult=true guarantees a webhook on
        silence, so a non-response is an affirmatively recorded outcome
        rather than a silent fall-through.
        """
        raise NotImplementedError("built by the route, which knows the action URL")

    def readback_response(self, text: str) -> tuple[str, str]:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Response><Say>{escape(text)}</Say><Hangup/></Response>",
            "application/xml",
        )

    def decision_response(self, *, committed: bool, next_url: str | None = None):
        if committed and next_url:
            return (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f"<Response><Redirect>{escape(next_url)}</Redirect></Response>",
                "application/xml",
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response><Say>We could not record your response. "
            "Someone will call you back.</Say><Hangup/></Response>",
            "application/xml",
        )


def gather_twiml(*, notice_text: str, action_url: str, language: str = "en-IN",
                 voice: str | None = None, audio_url: str | None = None,
                 timeout: int = 10) -> str:
    """The consent prompt.

    A pre-recorded <Play> is preferred over <Say> where an audio file exists:
    it gives a byte-identical, reproducible disclosure, where a TTS render
    can change between provider versions and cannot be reproduced later.
    """
    inner = (
        f"<Play>{escape(audio_url)}</Play>"
        if audio_url
        else f'<Say language="{escape(language)}"'
        + (f' voice="{escape(voice)}"' if voice else "")
        + f">{escape(notice_text)}</Say>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>"
        f'<Gather input="dtmf" numDigits="1" finishOnKey="" timeout="{timeout}" '
        f'actionOnEmptyResult="true" action="{escape(action_url)}" method="POST">'
        f"{inner}"
        "</Gather>"
        "</Response>"
    )


def _parse_rfc2822(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    from email.utils import parsedate_to_datetime

    try:
        ts = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def fetch_call_details(call_sid: str) -> CallDetails | None:
    """Twilio's Call resource. None means Twilio has no such call."""
    s = settings()
    url = f"{s.twilio_api_base}/2010-04-01/Accounts/{s.twilio_account_sid}/Calls/{call_sid}.json"
    try:
        resp = httpx.get(url, auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=10.0)
    except httpx.HTTPError as exc:
        raise CallLookupUnavailable(str(exc)) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise CallLookupUnavailable(f"twilio HTTP {resp.status_code}: {resp.text[:200]}")
    call = resp.json()
    return CallDetails(
        status=call.get("status"),
        from_number=call.get("from"),
        to_number=call.get("to"),
        answered_by=call.get("answered_by"),
        ended_at=_parse_rfc2822(call.get("end_time")),
    )
