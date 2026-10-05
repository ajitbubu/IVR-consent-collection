"""Sprinklr: an IVR flow calling our JSON endpoints from HTTP nodes."""
from __future__ import annotations

from sqlalchemy import select

from app.models import CallArtifact, Consent, IvrSession, WebhookReceipt

AUTH = {"Authorization": "Bearer test-sprinklr-token"}


def _post(client, path, body, headers=AUTH):
    return client.post(path, json=body, headers=headers)


def _outbound(client):
    r = client.post("/v1/sessions", json={
        "direction": "ivr_outbound", "phone_e164": "9876543210",
        "purpose_key": "marketing_outreach", "provider": "sprinklr",
    })
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def test_outbound_call_grants_consent(db, marketing_purpose, client):
    sid = _outbound(client)
    r = _post(client, "/sprinklr/start", {"session_id": sid, "call_id": "spr-1",
                                         "from": "+18005551212", "to": "09876543210",
                                         "direction": "outbound"})
    assert r.status_code == 200
    body = r.json()
    assert body["proceed"] is True and body["session_id"] == sid
    assert "press 1" in body["say"]
    assert body["audio_url"].endswith(".wav")

    r = _post(client, "/sprinklr/decision", {"session_id": sid, "call_id": "spr-1",
                                            "digits": 1,
                                            "occurred_at": "2026-10-05T10:15:00+05:30"})
    assert r.status_code == 200
    body = r.json()
    assert body["committed"] is True and body["outcome"] == "granted"
    assert "You have agreed to marketing calls and messages" in body["say"]

    c = db.execute(select(Consent)).scalar_one()
    assert c.provider == "sprinklr" and c.decision == "granted"
    assert c.decided_at.isoformat() == "2026-10-05T04:45:00+00:00"
    assert db.get(IvrSession, sid).call_sid == "spr-1"


def test_inbound_call_creates_its_session(db, marketing_purpose, client):
    r = _post(client, "/sprinklr/start", {"call_id": "spr-in", "from": "09876543210",
                                         "direction": "inbound",
                                         "purpose": "marketing_outreach"})
    assert r.json()["proceed"] is True
    sess = db.get(IvrSession, r.json()["session_id"])
    assert sess.provider == "sprinklr" and sess.direction == "ivr_inbound"

    # The decision can find the session by call id alone.
    r = _post(client, "/sprinklr/decision", {"call_id": "spr-in", "digits": "2"})
    assert r.json()["outcome"] == "declined"


def test_missing_or_wrong_token_is_rejected_and_kept(db, marketing_purpose, client):
    sid = _outbound(client)
    for headers in ({}, {"Authorization": "Bearer nope"}):
        r = _post(client, "/sprinklr/decision", {"session_id": sid, "digits": "1"},
                  headers=headers)
        assert r.status_code == 401
    assert db.execute(select(Consent)).scalars().all() == []

    receipts = db.execute(select(WebhookReceipt)).scalars().all()
    assert [r.signature_ok for r in receipts] == [False, False]
    # The token is never written down.
    assert all(r.signature is None for r in receipts)


def test_silence_and_unoffered_keys_are_never_consent(db, marketing_purpose, client):
    sid = _outbound(client)
    r = _post(client, "/sprinklr/decision", {"session_id": sid, "digits": ""})
    assert r.json() == {"committed": False, "outcome": "no_input",
                        "say": "We did not receive a response. Nothing has changed. Goodbye."}

    sid = _outbound(client)
    r = _post(client, "/sprinklr/decision", {"session_id": sid, "digits": "7"})
    assert r.json()["committed"] is False and r.json()["outcome"] == "invalid_key"
    assert db.execute(select(Consent)).scalars().all() == []


def test_replayed_decision_returns_the_original(db, marketing_purpose, client):
    sid = _outbound(client)
    first = _post(client, "/sprinklr/decision", {"session_id": sid, "digits": "1"}).json()
    again = _post(client, "/sprinklr/decision", {"session_id": sid, "digits": "2"}).json()
    assert again == first
    assert len(db.execute(select(Consent)).scalars().all()) == 1


def test_unknown_session_says_call_back(db, marketing_purpose, client):
    r = _post(client, "/sprinklr/decision", {"session_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                                            "digits": "1"})
    assert r.json()["committed"] is False
    assert "call you back" in r.json()["say"]


def test_start_without_session_or_purpose_stops(db, marketing_purpose, client):
    r = _post(client, "/sprinklr/start", {"call_id": "spr-x", "from": "09876543210"})
    assert r.json()["proceed"] is False


def test_withheld_caller_id_stops_politely(db, marketing_purpose, client):
    r = _post(client, "/sprinklr/start", {"call_id": "spr-anon", "from": "anonymous",
                                         "purpose": "marketing_outreach"})
    assert r.status_code == 200
    assert r.json()["proceed"] is False and "caller ID" in r.json()["say"]


def test_failed_commit_never_claims_committed(db, marketing_purpose, client, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.main import create_app

    sid = _outbound(client)

    def boom(self):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", boom)
    r = _post(TestClient(create_app(), raise_server_exceptions=False),
              "/sprinklr/decision", {"session_id": sid, "digits": "1"})
    monkeypatch.undo()

    assert r.json()["committed"] is False
    assert db.execute(select(Consent)).scalars().all() == []


def test_status_records_end_and_queues_the_recording(db, marketing_purpose, client):
    sid = _outbound(client)
    _post(client, "/sprinklr/start", {"session_id": sid, "call_id": "spr-st"})
    r = _post(client, "/sprinklr/status", {"call_id": "spr-st", "status": "completed",
                                          "ended_at": "2026-10-05T04:46:00Z",
                                          "recording_url": "https://media.example/spr-st.mp3"})
    assert r.status_code == 204

    sess = db.get(IvrSession, sid)
    db.refresh(sess)
    assert sess.call_status == "completed"
    assert sess.ended_at.isoformat() == "2026-10-05T04:46:00+00:00"
    art = db.execute(select(CallArtifact)).scalar_one()
    assert art.source_url == "https://media.example/spr-st.mp3"


def test_json_params_are_kept_on_the_receipt(db, marketing_purpose, client):
    sid = _outbound(client)
    _post(client, "/sprinklr/decision", {"session_id": sid, "digits": "1"})
    receipt = db.execute(select(WebhookReceipt)).scalar_one()
    assert receipt.params == {"session_id": sid, "digits": "1"}
    assert receipt.signature_ok is True
