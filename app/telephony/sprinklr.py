"""Sprinklr provider.

Sprinklr's IVR flow plays the notice and collects the keypress itself, and
calls our JSON endpoints from HTTP nodes. The contract is ours, not
Sprinklr's: see the /sprinklr routes.

Authentication is a shared bearer token in the Authorization header, the
most an HTTP node can be relied on to send. It proves the request came from
something holding the token, but unlike Twilio's per-request signature it
cannot be re-verified later, so the token itself is never stored.

TODO(sprinklr): where is Sprinklr Voice data hosted, and can it stay in
India? Needed for the DPDP residency column in the README's provider table.
"""
from __future__ import annotations

import datetime as dt
import hmac

from app.config import settings
from app.telephony.base import Verification, WebhookEvent


def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse_iso(raw) -> dt.datetime | None:
    """ISO 8601. A timestamp without an offset is taken as UTC."""
    raw = _str(raw)
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


class SprinklrProvider:
    name = "sprinklr"

    def __init__(self, token: str | None = None):
        self._token = token

    @property
    def token(self) -> str:
        return self._token or settings().sprinklr_webhook_token

    def verify(self, *, url: str, headers: dict, form: dict, body: bytes) -> Verification:
        # TODO(sprinklr): confirm IVR HTTP nodes can send a custom
        # Authorization header. If they can HMAC-sign the body instead, verify
        # that here and store the signature on the receipt, as Twilio does.
        token = self.token
        if not token or token == "unset":
            return Verification(ok=False, signature=None, reason="no webhook token configured")
        got = headers.get("authorization", "")
        if not got.startswith("Bearer "):
            return Verification(ok=False, signature=None, reason="missing bearer token")
        if not hmac.compare_digest(got[len("Bearer "):].encode(), token.encode()):
            return Verification(ok=False, signature=None, reason="token mismatch")
        return Verification(ok=True, signature=None, reason="shared token valid")

    def parse(self, *, form: dict, query: dict) -> WebhookEvent:
        p = {**query, **form}
        direction = (_str(p.get("direction")) or "").lower()
        return WebhookEvent(
            provider=self.name,
            call_ref=_str(p.get("call_id")),
            session_ref=_str(p.get("session_id")),
            from_number=_str(p.get("from")),
            to_number=_str(p.get("to")),
            direction="ivr_outbound" if direction.startswith("out") else "ivr_inbound",
            # JSON may carry the key as a number; it is still one keypress.
            digits=_str(p.get("digits")),
            occurred_at=parse_iso(p.get("occurred_at")),
            call_status=_str(p.get("status")),
            recording_url=_str(p.get("recording_url")),
            ended_at=parse_iso(p.get("ended_at")),
            raw=p,
        )

    # Responses are JSON built by the routes; the flow branches on fields.
    def notice_response(self, text: str) -> tuple[str, str]:
        raise NotImplementedError("built by the route")

    def decision_response(self, *, committed: bool, next_url: str | None = None):
        raise NotImplementedError("built by the route")

    def readback_response(self, text: str) -> tuple[str, str]:
        raise NotImplementedError("built by the route")
