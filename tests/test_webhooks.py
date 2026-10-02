"""The Exotel webhooks as Exotel actually calls them: GET with query params."""
from __future__ import annotations

from sqlalchemy import select

from app.models import Consent, IvrSession


def _create_session(client, purpose_code="marketing_outreach"):
    r = client.post("/v1/sessions", json={
        "direction": "ivr_outbound",
        "phone_e164": "9876543210",
        "purpose_key": purpose_code,
        "language": "eng",
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_session_id_fits_exotel_custom_field(db, marketing_purpose, client):
    body = _create_session(client)
    # Exotel caps CustomField at 128 characters.
    assert len(body["custom_field"]) <= 128
    assert body["notice_version_id"]


def test_notice_returns_plain_text_and_answers_head(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-1", "CallFrom": "09876543210"}

    got = client.get("/exotel/notice", params=params)
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("text/plain")
    assert "press 1" in got.text

    # Exotel requires HEAD with the same headers; it fails silently otherwise.
    head = client.head("/exotel/notice", params=params)
    assert head.status_code == 200
    assert head.headers["content-type"].startswith("text/plain")


def test_quoted_digit_from_exotel_grants_consent(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-2", "CallFrom": "09876543210"}
    client.get("/exotel/identify", params=params)
    client.get("/exotel/notice", params=params)

    # This is the wire shape: the value arrives wrapped in double quotes.
    r = client.get("/exotel/decision", params={**params, "digits": '"1"',
                                               "CurrentTime": "2026-09-17 14:44:22"})
    assert r.status_code == 200

    c = db.execute(select(Consent)).scalar_one()
    assert c.decision == "granted"
    assert c.permits_processing is True


def test_no_input_returns_302_and_no_consent(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-3"}
    r = client.get("/exotel/decision", params=params)
    assert r.status_code == 302
    assert db.execute(select(Consent)).scalars().all() == []


def test_unoffered_key_returns_302_and_no_consent(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-4"}
    r = client.get("/exotel/decision", params={**params, "digits": '"7"'})
    assert r.status_code == 302
    assert db.execute(select(Consent)).scalars().all() == []

    sess = db.get(IvrSession, s["session_id"])
    db.refresh(sess)
    assert sess.outcome == "invalid_key"


# Regression: ISSUE-002 — readback after silence promised a callback that never comes
# Found by /qa on 2026-10-01
# Report: .gstack/qa-reports/qa-report-ivr-consent-2026-10-01.md
def test_readback_after_no_decision_says_nothing_changed(db, marketing_purpose, client):
    for call_sid, digits in (("cs-silent", None), ("cs-badkey", '"7"')):
        s = _create_session(client)
        params = {"CustomField": s["session_id"], "CallSid": call_sid}
        client.get("/exotel/decision", params={**params, **({"digits": digits} if digits else {})})

        r = client.get("/exotel/readback", params=params)
        assert r.status_code == 200
        assert "nothing has changed" in r.text.lower(), call_sid
        assert "call you back" not in r.text.lower(), call_sid


def test_unknown_session_returns_302_never_200(db, marketing_purpose, client):
    """302 means the write failed. A caller is never told their consent was
    recorded when it was not."""
    r = client.get("/exotel/decision", params={"CallSid": "cs-nope", "digits": '"1"'})
    assert r.status_code == 302


def test_readback_names_the_decision(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-5"}
    client.get("/exotel/notice", params=params)
    client.get("/exotel/decision", params={**params, "digits": '"1"'})

    r = client.get("/exotel/readback", params=params)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "agreed" in r.text.lower()


def test_status_callback_always_returns_200(db, marketing_purpose, client):
    """Exotel is told 200 even for an unknown CallSid -- a non-200 buys
    nothing, since the retry policy is undocumented."""
    r = client.post("/exotel/status", json={"CallSid": "cs-unknown", "Status": "completed"})
    assert r.status_code == 200


def test_status_callback_records_recording_and_answered_by(db, marketing_purpose, client):
    from app.models import CallArtifact

    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-6"}
    client.get("/exotel/notice", params=params)
    client.get("/exotel/decision", params={**params, "digits": '"1"'})

    r = client.post("/exotel/status", json={
        "CallSid": "cs-6",
        "CustomField": s["session_id"],
        "Status": "completed",
        "EndTime": "2026-09-17 14:45:02",
        "RecordingUrl": "https://s3-ap-southeast-1.amazonaws.com/exotelrecordings/x/cs-6.mp3",
        "Legs": [{"AnsweredBy": "NA"}, {"AnsweredBy": "Human"}],
    })
    assert r.status_code == 200

    sess = db.get(IvrSession, s["session_id"])
    db.refresh(sess)
    assert sess.answered_by == "Human"
    assert sess.ended_at is not None

    art = db.execute(select(CallArtifact)).scalar_one()
    assert art.kind == "recording"
    # Exotel's URL is never the long-term reference: a worker copies it into
    # our own bucket and fills storage_uri there.
    assert art.storage_uri is None


def test_frozen_endtime_is_not_stored(db, marketing_purpose, client):
    s = _create_session(client)
    client.post("/exotel/status", json={
        "CallSid": "cs-7", "CustomField": s["session_id"],
        "Status": "completed", "EndTime": "1970-01-01 05:30:00",
    })
    sess = db.get(IvrSession, s["session_id"])
    db.refresh(sess)
    assert sess.ended_at is None


def test_evidence_bundle_is_complete(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-8"}
    client.get("/exotel/notice", params=params)
    client.get("/exotel/decision", params={**params, "digits": '"1"'})

    c = db.execute(select(Consent)).scalar_one()
    r = client.get(f"/v1/consents/{c.id}/evidence")
    assert r.status_code == 200
    b = r.json()

    assert b["consent"]["decision"] == "granted"
    assert b["notice"]["body_text"]
    assert b["notice"]["sha256"]
    assert b["chain"]["verified"] is True
    types = [e["event_type"] for e in b["chain"]["events"]]
    assert types.index("notice.served") < types.index("consent.granted")


def test_read_consents_by_phone_in_any_shape(db, marketing_purpose, client):
    s = _create_session(client)
    params = {"CustomField": s["session_id"], "CallSid": "cs-9"}
    client.get("/exotel/notice", params=params)
    client.get("/exotel/decision", params={**params, "digits": '"1"'})

    for shape in ["+919876543210", "09876543210", "9876543210"]:
        r = client.get("/v1/consents", params={"phone_e164": shape})
        assert r.status_code == 200, shape
        assert r.json()["consents"][0]["decision"] == "granted"


# Regression: ISSUE-001 — a malformed phone number returned 500 instead of 400
# Found by /qa on 2026-10-01
# Report: .gstack/qa-reports/qa-report-ivr-consent-2026-10-01.md
def test_malformed_phone_is_rejected_not_a_server_error(db, marketing_purpose, client):
    r = client.post("/v1/sessions", json={
        "direction": "ivr_outbound", "phone_e164": "not-a-phone",
        "purpose_key": "marketing_outreach",
    })
    assert r.status_code == 400, r.text

    r = client.get("/v1/consents", params={"phone_e164": "abc"})
    assert r.status_code == 400, r.text

    # A withheld caller id arrives as no CallFrom at all.
    r = client.get("/exotel/identify", params={
        "CallSid": "cs-withheld", "purpose": "marketing_outreach", "Direction": "incoming",
    })
    assert r.status_code == 400, r.text
    assert db.execute(
        select(IvrSession).where(IvrSession.call_sid == "cs-withheld")
    ).scalar_one_or_none() is None
