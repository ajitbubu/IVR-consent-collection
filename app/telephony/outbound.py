"""Outbound call placement, per provider."""
from __future__ import annotations

import base64

import httpx

from app.config import settings


class OutboundError(Exception):
    pass


def place_call_exotel(*, to_number: str, flow_app_id: str, session_id: str,
                      status_callback: str) -> dict:
    s = settings()
    auth = base64.b64encode(f"{s.exotel_api_key}:{s.exotel_api_token}".encode()).decode()
    url = f"{s.exotel_base_url}/v1/Accounts/{s.exotel_sid}/Calls/connect.json"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Basic {auth}"},
        data={
            "From": to_number,                      # the party Exotel calls first
            "CallerId": s.exotel_caller_id,
            "Url": f"http://my.exotel.com/{s.exotel_sid}/exoml/start_voice/{flow_app_id}",
            "CallType": "trans",
            "StatusCallback": status_callback,
            "StatusCallbackEvents[0]": "terminal",
            "CustomField": session_id,              # max 128 chars; a ULID is 26
        },
        timeout=10.0,
    )
    if resp.status_code >= 400:
        raise OutboundError(f"exotel HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def place_call_twilio(*, to_number: str, session_id: str, voice_url: str,
                      status_callback: str, recording_callback: str) -> dict:
    """MachineDetection is on: consent captured from a voicemail system is
    not consent, so the decision handler drops anything not answered by a
    human. Trim is off and recording is dual-channel, which keeps the
    disclosure and the caller's keypress on separate tracks -- much stronger
    evidence that the notice was played before the decision."""
    s = settings()
    auth = base64.b64encode(f"{s.twilio_account_sid}:{s.twilio_auth_token}".encode()).decode()
    url = f"{s.twilio_api_base}/2010-04-01/Accounts/{s.twilio_account_sid}/Calls.json"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Basic {auth}"},
        data=[
            ("To", to_number),
            ("From", s.twilio_caller_id),
            ("Url", f"{voice_url}?session={session_id}"),
            ("StatusCallback", status_callback),
            ("StatusCallbackEvent", "initiated"),
            ("StatusCallbackEvent", "ringing"),
            ("StatusCallbackEvent", "answered"),
            ("StatusCallbackEvent", "completed"),
            ("MachineDetection", "Enable"),
            ("AsyncAmd", "false"),
            ("Record", "true"),
            ("RecordingChannels", "dual"),
            ("Trim", "do-not-trim"),
            ("RecordingStatusCallback", recording_callback),
            ("RecordingStatusCallbackEvent", "completed"),
        ],
        timeout=10.0,
    )
    if resp.status_code >= 400:
        raise OutboundError(f"twilio HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()
