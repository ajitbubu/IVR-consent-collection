"""Twilio: signature validation and the consent flow.

The whole point of Twilio over Exotel here is that the webhook is signed, so
most of these tests are about that signature actually being load-bearing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.models import Consent, IvrSession, WebhookReceipt
from app.telephony.twilio import TwilioProvider, compute_signature, gather_twiml

TOKEN = "test-auth-token"
BASE = "https://consent.test"


def sign(url: str, params: dict) -> str:
    return compute_signature(TOKEN, url, params)


def post_signed(client, path: str, params: dict):
    url = BASE + path
    return client.post(path, data=params, headers={"X-Twilio-Signature": sign(url, params)})


# ------------------------------------------------------------ the algorithm


def test_signing_string_matches_the_documented_example():
    """Twilio's published worked example. The query string stays on the URL
    and is signed as-is; POST params are appended name+value with no
    delimiters, in sorted order, using decoded values."""
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675310",
        "Digits": "1234",
        "From": "+14158675310",
        "To": "+18005551212",
    }
    expected_string = (
        "https://mycompany.com/myapp.php?foo=1&bar=2"
        "CallSidCA1234567890ABCDE"
        "Caller+14158675310"
        "Digits1234"
        "From+14158675310"
        "To+18005551212"
    )
    built = url + "".join(k + params[k] for k in sorted(params))
    assert built == expected_string

    expected_sig = base64.b64encode(
        hmac.new(b"12345", expected_string.encode(), hashlib.sha1).digest()
    ).decode()
    assert compute_signature("12345", url, params) == expected_sig


def test_signature_covers_the_keypress():
    """Changing the digit must invalidate the signature -- otherwise the
    signature proves nothing about the consent itself."""
    url = f"{BASE}/twilio/decision"
    granted = {"CallSid": "CA1", "Digits": "1"}
    declined = {"CallSid": "CA1", "Digits": "2"}
    assert compute_signature(TOKEN, url, granted) != compute_signature(TOKEN, url, declined)


def test_verify_accepts_a_good_signature_and_rejects_a_forged_one():
    p = TwilioProvider(auth_token=TOKEN)
    url = f"{BASE}/twilio/decision?session=01ABC"
    params = {"CallSid": "CA1", "Digits": "1"}
    good = sign(url, params)

    assert p.verify(url=url, headers={"x-twilio-signature": good}, form=params, body=b"").ok
    tampered = p.verify(
        url=url, headers={"x-twilio-signature": good}, form={**params, "Digits": "2"}, body=b""
    )
    assert tampered.ok is False
    assert tampered.reason == "signature mismatch"


def test_missing_signature_is_not_ok():
    p = TwilioProvider(auth_token=TOKEN)
    v = p.verify(url=f"{BASE}/twilio/decision", headers={}, form={"Digits": "1"}, body=b"")
    assert v.ok is False
    assert "missing" in v.reason


def test_url_with_and_without_explicit_port_both_validate():
    """Twilio's own SDK checks both because their signature generation is
    inconsistent about the port. Implementing only one form gives
    intermittent failures that are very hard to chase down."""
    p = TwilioProvider(auth_token=TOKEN)
    params = {"CallSid": "CA1", "Digits": "1"}
    plain = "https://consent.test/twilio/decision"
    ported = "https://consent.test:443/twilio/decision"

    sig_from_ported = compute_signature(TOKEN, ported, params)
    assert p.verify(url=plain, headers={"x-twilio-signature": sig_from_ported},
                    form=params, body=b"").ok

    sig_from_plain = compute_signature(TOKEN, plain, params)
    assert p.verify(url=ported, headers={"x-twilio-signature": sig_from_plain},
                    form=params, body=b"").ok


def test_json_body_validates_via_bodysha256():
    p = TwilioProvider(auth_token=TOKEN)
    body = json.dumps({"CallSid": "CA1", "Digits": "1"}).encode()
    digest = hashlib.sha256(body).hexdigest()
    url = f"{BASE}/twilio/status?bodySHA256={digest}"
    sig = compute_signature(TOKEN, url, None)

    assert p.verify(url=url, headers={"x-twilio-signature": sig}, form={}, body=body).ok

    bad = p.verify(url=url, headers={"x-twilio-signature": sig}, form={},
                   body=b'{"CallSid":"CA1","Digits":"2"}')
    assert bad.ok is False
    assert bad.reason == "bodySHA256 mismatch"


def test_exotel_quote_trimming_is_not_applied_to_twilio():
    """Twilio sends Digits plain and unquoted, with finishOnKey already
    stripped. Sharing Exotel's parser would work by accident for digits and
    silently corrupt anything else."""
    p = TwilioProvider(auth_token=TOKEN)
    event = p.parse(form={"Digits": "1", "CallSid": "CA1"}, query={})
    assert event.digits == "1"

    quoted = p.parse(form={"Digits": '"1"', "CallSid": "CA1"}, query={})
    assert quoted.digits == '"1"'   # not silently unwrapped


# ------------------------------------------------------------ the TwiML


def test_gather_twiml_shape():
    xml = gather_twiml(notice_text="Press 1 to agree.",
                       action_url="https://consent.test/twilio/decision?session=01ABC")
    assert 'numDigits="1"' in xml          # single keypress
    assert 'finishOnKey=""' in xml         # no hash needed
    assert 'actionOnEmptyResult="true"' in xml   # silence still fires a webhook
    assert 'input="dtmf"' in xml
    assert "Press 1 to agree." in xml


def test_recorded_audio_is_preferred_over_tts_when_available():
    """A pre-recorded file is byte-identical and reproducible; a TTS render
    can change between provider versions and cannot be reproduced later."""
    xml = gather_twiml(notice_text="Press 1 to agree.",
                       action_url="https://consent.test/twilio/decision",
                       audio_url="https://cdn.example.com/notice.v1.mp3")
    assert "<Play>https://cdn.example.com/notice.v1.mp3</Play>" in xml
    assert "<Say" not in xml


def test_twiml_escapes_notice_text():
    xml = gather_twiml(notice_text='Fish & chips <script>', action_url="https://x.test/a?b=1&c=2")
    assert "&amp;" in xml and "<script>" not in xml


# ------------------------------------------------------------ the flow


def _session(client, purpose_code="marketing_outreach", direction="ivr_outbound",
             provider="twilio"):
    r = client.post("/v1/sessions", json={
        "direction": direction, "phone_e164": "9876543210",
        "purpose_key": purpose_code, "language": "eng", "provider": provider,
    })
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def test_signed_keypress_grants_consent(db, marketing_purpose, client):
    sid = _session(client)
    path = f"/twilio/voice?session={sid}"
    r = post_signed(client, path, {"CallSid": "CAtw1", "From": "+919876543210",
                                   "To": "+18005551212", "Direction": "outbound-api"})
    assert r.status_code == 200
    assert 'actionOnEmptyResult="true"' in r.text

    path = f"/twilio/decision?session={sid}"
    r = post_signed(client, path, {"CallSid": "CAtw1", "Digits": "1", "AnsweredBy": "human"})
    assert r.status_code == 200
    assert "<Redirect>" in r.text

    c = db.execute(select(Consent)).scalar_one()
    assert c.decision == "granted"
    assert c.provider == "twilio"
    assert c.permits_processing is True


def test_unsigned_request_records_no_consent(db, marketing_purpose, client):
    """An unverified Twilio webhook is a forgery. This is the control Exotel
    cannot offer at all."""
    sid = _session(client)
    r = client.post(f"/twilio/decision?session={sid}", data={"CallSid": "CAtw2", "Digits": "1"})
    assert r.status_code == 403
    assert db.execute(select(Consent)).scalars().all() == []


def test_forged_signature_records_no_consent(db, marketing_purpose, client):
    sid = _session(client)
    r = client.post(
        f"/twilio/decision?session={sid}",
        data={"CallSid": "CAtw3", "Digits": "1"},
        headers={"X-Twilio-Signature": "obviouslyWrongSignature="},
    )
    assert r.status_code == 403
    assert db.execute(select(Consent)).scalars().all() == []


def test_rejected_webhook_is_still_recorded(db, marketing_purpose, client):
    """A rejected webhook is exactly the thing you want a record of."""
    sid = _session(client)
    client.post(f"/twilio/decision?session={sid}", data={"CallSid": "CAtw4", "Digits": "1"},
                headers={"X-Twilio-Signature": "wrong="})

    r = db.execute(select(WebhookReceipt)).scalars().all()
    assert len(r) == 1
    assert r[0].signature_ok is False
    assert r[0].provider == "twilio"


def test_signature_is_kept_for_later_reverification(db, marketing_purpose, client):
    sid = _session(client)
    post_signed(client, f"/twilio/decision?session={sid}",
                {"CallSid": "CAtw5", "Digits": "1", "AnsweredBy": "human"})

    receipt = db.execute(
        select(WebhookReceipt).where(WebhookReceipt.route == "decision")
    ).scalar_one()
    assert receipt.signature_ok is True
    assert receipt.signature
    assert receipt.request_url.startswith("https://consent.test/twilio/decision")
    assert receipt.params["Digits"] == "1"

    # The stored triple re-verifies on its own, months later.
    assert compute_signature(TOKEN, receipt.request_url, receipt.params) == receipt.signature


def test_answering_machine_is_never_consent(db, marketing_purpose, client):
    sid = _session(client)
    r = post_signed(client, f"/twilio/decision?session={sid}",
                    {"CallSid": "CAtw6", "Digits": "1", "AnsweredBy": "machine_start"})
    assert r.status_code == 200
    assert db.execute(select(Consent)).scalars().all() == []

    sess = db.get(IvrSession, sid)
    db.refresh(sess)
    assert sess.outcome == "answering_machine"


def test_empty_result_is_recorded_as_a_non_decision(db, marketing_purpose, client):
    """actionOnEmptyResult means silence fires a webhook, so a non-response
    is an affirmative outcome rather than a silent fall-through."""
    sid = _session(client)
    r = post_signed(client, f"/twilio/decision?session={sid}",
                    {"CallSid": "CAtw7", "Digits": "", "AnsweredBy": "human"})
    assert r.status_code == 200
    assert db.execute(select(Consent)).scalars().all() == []

    sess = db.get(IvrSession, sid)
    db.refresh(sess)
    assert sess.outcome == "no_input"


def test_readback_names_the_decision(db, marketing_purpose, client):
    sid = _session(client)
    post_signed(client, f"/twilio/decision?session={sid}",
                {"CallSid": "CAtw8", "Digits": "2", "AnsweredBy": "human"})
    r = post_signed(client, f"/twilio/readback?session={sid}", {"CallSid": "CAtw8"})
    assert r.status_code == 200
    assert "declined" in r.text.lower()


def test_both_providers_write_the_same_consent_shape(db, marketing_purpose, client):
    """The state machine is provider-agnostic: only the webhook layer differs."""
    tw = _session(client)
    post_signed(client, f"/twilio/decision?session={tw}",
                {"CallSid": "CAtw9", "Digits": "1", "AnsweredBy": "human"})

    ex = _session(client, provider="exotel")
    client.get("/exotel/decision", params={"CustomField": ex, "CallSid": "cs-ex9",
                                           "digits": '"1"'})

    rows = db.execute(select(Consent).order_by(Consent.created_at)).scalars().all()
    assert {r.provider for r in rows} == {"twilio", "exotel"}
    assert {r.decision for r in rows} == {"granted"}
    # Same person, same purpose: the second supersedes the first regardless
    # of which provider carried it.
    assert sum(1 for r in rows if r.is_current) == 1


# Regression: ISSUE-004 — a withheld caller id made Twilio play "application error"
# Found by /qa on 2026-10-02
# Report: .gstack/qa-reports/run-20261002T142242Z/qa-report-ivr-consent-2026-10-02.md
def test_withheld_caller_id_gets_a_spoken_hangup_not_an_error(db, marketing_purpose, client):
    r = post_signed(client, "/twilio/voice?purpose=marketing_outreach",
                    {"CallSid": "CAanon", "From": "anonymous", "To": "+18668494269",
                     "Direction": "inbound"})
    assert r.status_code == 200, r.text
    assert "<Say>" in r.text and "<Hangup/>" in r.text
    assert db.execute(select(IvrSession)).scalars().all() == []


# Regression: ISSUE-006 — the inbound /voice receipt was never linked to its session
# Found by /qa on 2026-10-02
# Report: .gstack/qa-reports/run-20261002T142242Z/qa-report-ivr-consent-2026-10-02.md
def test_inbound_voice_receipt_is_linked_to_the_session_it_creates(db, marketing_purpose, client):
    r = post_signed(client, "/twilio/voice?purpose=marketing_outreach",
                    {"CallSid": "CAinbound", "From": "+19735550123", "To": "+18668494269",
                     "Direction": "inbound"})
    assert r.status_code == 200

    sess = db.execute(select(IvrSession).where(IvrSession.call_sid == "CAinbound")).scalar_one()
    receipt = db.execute(
        select(WebhookReceipt).where(WebhookReceipt.route == "voice")
    ).scalar_one()
    assert receipt.signature_ok is True
    assert receipt.ivr_session_id == sess.id


# Regression: ISSUE-005 — an older status callback arriving late overwrote "completed"
# Found by /qa on 2026-10-02
# Report: .gstack/qa-reports/run-20261002T142242Z/qa-report-ivr-consent-2026-10-02.md
def test_late_status_callback_does_not_rewind_the_call(db, marketing_purpose, client):
    sid = _session(client)
    post_signed(client, f"/twilio/voice?session={sid}",
                {"CallSid": "CAseq", "From": "+919876543210", "To": "+18668494269",
                 "Direction": "outbound-api"})

    def status(call_status, seq, ts):
        r = post_signed(client, "/twilio/status",
                        {"CallSid": "CAseq", "CallStatus": call_status, "SequenceNumber": seq,
                         "Timestamp": ts})
        assert r.status_code == 204

    status("in-progress", "1", "Fri, 02 Oct 2026 14:29:40 +0000")
    sess = db.get(IvrSession, sid)
    db.refresh(sess)
    assert sess.call_status == "in-progress"
    assert sess.ended_at is None  # a call that is still going has not ended

    status("completed", "3", "Fri, 02 Oct 2026 14:30:05 +0000")
    status("ringing", "0", "Fri, 02 Oct 2026 14:29:30 +0000")  # delivered last, happened first

    db.refresh(sess)
    assert sess.call_status == "completed"
    assert sess.ended_at.isoformat().startswith("2026-10-02T14:30:05")


def test_failed_commit_never_redirects_to_the_confirmation(db, marketing_purpose, client,
                                                            monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.main import create_app

    sid = _session(client)

    def boom(self):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", boom)
    r = post_signed(TestClient(create_app(), raise_server_exceptions=False),
                    f"/twilio/decision?session={sid}",
                    {"CallSid": "CAtw-commit", "Digits": "1", "AnsweredBy": "human"})
    monkeypatch.undo()

    assert r.status_code == 200
    assert "<Redirect>" not in r.text
    assert "Someone will call you back" in r.text
    assert db.execute(select(Consent)).scalars().all() == []
