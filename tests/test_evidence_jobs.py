"""Background evidence jobs: recording fetch, purge, digest anchoring,
call reconciliation, and the loop that runs them."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from app.consent_service import attach_call, create_session, record_decision
from app.evidence import (
    anchor_daily_digest,
    fetch_pending_recordings,
    purge_expired_artifacts,
    queue_recording_fetch,
)
from app.hashchain import verify_chain
from app.models import CallArtifact, ConsentEvent, IvrSession, UcmOutbox
from app.reconcile import reconcile_due
from app.telephony.base import CallDetails, CallLookupUnavailable

PHONE = "+919876543210"


@pytest.fixture()
def evidence_dir(tmp_path, monkeypatch):
    from app.config import reset_settings

    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path))
    reset_settings()
    yield tmp_path
    reset_settings()


def _call(db, purpose, *, sid="cs-job", provider="exotel", digit="1", age_s=600):
    s = create_session(db, direction="ivr_outbound", phone_raw=PHONE,
                       purpose_code=purpose.code, provider=provider)
    attach_call(db, s, call_sid=sid)
    consent = record_decision(db, s, digit=digit).consent if digit else None
    s.started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s)
    db.commit()
    return s, consent


def _path(uri: str) -> Path:
    return Path(urlparse(uri).path)


# ------------------------------------------------------------------ recordings


def test_fetched_recording_is_stored_and_hashed(db, marketing_purpose, evidence_dir):
    s, _ = _call(db, marketing_purpose)
    art = queue_recording_fetch(db, s, "https://exotel.example/rec/cs-job.mp3")
    db.commit()

    seen = []
    counts = fetch_pending_recordings(db, fetch=lambda url, p: seen.append((url, p)) or b"audio")
    db.commit()

    assert counts == {"stored": 1, "failed": 0}
    assert seen == [("https://exotel.example/rec/cs-job.mp3", "exotel")]
    db.refresh(art)
    assert _path(art.storage_uri).read_bytes() == b"audio"
    assert _path(art.storage_uri).suffix == ".mp3"
    assert art.sha256 == hashlib.sha256(b"audio").digest()
    assert art.bytes == 5


def test_failed_fetch_is_retried_then_given_up(db, marketing_purpose, evidence_dir):
    from app.config import settings

    s, _ = _call(db, marketing_purpose)
    art = queue_recording_fetch(db, s, "https://exotel.example/rec/gone.mp3")
    db.commit()

    def boom(url, provider):
        raise httpx.HTTPError("404 gone")

    for _ in range(settings().recording_max_attempts + 2):
        fetch_pending_recordings(db, fetch=boom)
        db.commit()

    db.refresh(art)
    assert art.fetch_attempts == settings().recording_max_attempts
    assert art.storage_uri is None
    assert "404 gone" in art.last_error


def test_purge_deletes_the_stored_file(db, marketing_purpose, evidence_dir):
    s, _ = _call(db, marketing_purpose)
    art = queue_recording_fetch(db, s, "https://exotel.example/rec/x.mp3")
    db.commit()
    fetch_pending_recordings(db, fetch=lambda url, p: b"audio")
    db.refresh(art)
    stored = _path(art.storage_uri)
    art.purge_after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()

    assert purge_expired_artifacts(db) == 1
    db.commit()
    assert not stored.exists()
    assert art.sha256 is not None


# ------------------------------------------------------------------ digest


def test_digest_is_anchored_once_and_read_only(db, marketing_purpose, evidence_dir):
    _call(db, marketing_purpose)
    today = dt.datetime.now(dt.timezone.utc).date()

    uri = anchor_daily_digest(db, today)
    assert uri is not None
    assert today.isoformat() in _path(uri).read_text()
    assert not os.access(_path(uri), os.W_OK)

    # A second pass never rewrites it, even if the chains have moved on.
    _call(db, marketing_purpose, sid="cs-later")
    assert anchor_daily_digest(db, today) is None


# ------------------------------------------------------------------ reconciliation


def _lookup(details=None, exc=None):
    def f(call_sid):
        if exc:
            raise exc
        return details
    return {"exotel": f, "twilio": f}


def _outbox(db, consent):
    return db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == consent.id)).scalar_one()


def test_matching_call_reconciles_ok(db, marketing_purpose):
    s, consent = _call(db, marketing_purpose)
    counts = reconcile_due(db, _lookup(CallDetails("completed", "09876543210", "0801234567")))
    db.commit()

    assert counts["ok"] == 1
    db.refresh(s)
    assert s.reconciled_at is not None and s.reconcile_result == "ok"
    assert _outbox(db, consent).paused is False
    ev = db.execute(select(ConsentEvent).order_by(ConsentEvent.seq.desc()).limit(1)).scalar_one()
    assert ev.event_type == "call.reconciled" and ev.payload["result"] == "ok"
    assert verify_chain(db, s.data_principal_id) == (True, None)


@pytest.mark.parametrize("details,result", [
    (CallDetails("completed", "+919999999999", "0801234567"), "number_mismatch"),
    (None, "not_found"),
    (CallDetails("no-answer", PHONE, "0801234567"), "not_connected"),
])
def test_uncorroborated_call_holds_the_consent(db, marketing_purpose, details, result):
    s, consent = _call(db, marketing_purpose)
    counts = reconcile_due(db, _lookup(details))
    db.commit()

    assert counts["mismatch"] == 1
    db.refresh(s)
    db.refresh(consent)
    assert s.reconcile_result == result
    row = _outbox(db, consent)
    assert row.paused is True and result in row.last_error
    assert consent.ucm_sync_state == "failed"


def test_provider_outage_defers_without_marking(db, marketing_purpose):
    s, _ = _call(db, marketing_purpose)
    counts = reconcile_due(db, _lookup(exc=CallLookupUnavailable("HTTP 503")))
    db.commit()
    assert counts["deferred"] == 1
    db.refresh(s)
    assert s.reconciled_at is None


def test_call_still_in_progress_is_deferred(db, marketing_purpose):
    s, _ = _call(db, marketing_purpose)
    reconcile_due(db, _lookup(CallDetails("in-progress", PHONE, None)))
    db.commit()
    db.refresh(s)
    assert s.reconciled_at is None


def test_recent_call_is_left_alone(db, marketing_purpose):
    s, _ = _call(db, marketing_purpose, age_s=10)
    assert reconcile_due(db, _lookup(CallDetails("completed", PHONE, None))) == \
        {"ok": 0, "mismatch": 0, "deferred": 0}


def test_unanswered_call_without_consent_reconciles_ok(db, marketing_purpose):
    s, _ = _call(db, marketing_purpose, digit=None)
    reconcile_due(db, _lookup(CallDetails("no-answer", PHONE, None)))
    db.commit()
    db.refresh(s)
    assert s.reconcile_result == "ok"


def test_exotel_call_details_are_parsed(monkeypatch):
    from app.telephony import exotel

    def fake_get(url, auth, timeout):
        assert url.endswith("/Calls/cs-1.json")
        return httpx.Response(200, json={"Call": {
            "Sid": "cs-1", "Status": "completed", "From": "09876543210",
            "To": "08012345678", "EndTime": "2026-09-17 14:45:02"}})

    monkeypatch.setattr(exotel.httpx, "get", fake_get)
    d = exotel.fetch_call_details("cs-1")
    assert d.status == "completed" and d.from_number == "09876543210"
    assert d.ended_at.utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_twilio_call_details_404_means_no_such_call(monkeypatch):
    from app.telephony import twilio

    monkeypatch.setattr(twilio.httpx, "get", lambda url, auth, timeout: httpx.Response(404))
    assert twilio.fetch_call_details("CAnope") is None

    monkeypatch.setattr(twilio.httpx, "get", lambda url, auth, timeout: httpx.Response(503))
    with pytest.raises(CallLookupUnavailable):
        twilio.fetch_call_details("CAnope")


# ------------------------------------------------------------------ the loop


def test_one_failing_step_does_not_stop_the_others(db, marketing_purpose, evidence_dir,
                                                   monkeypatch):
    import app.jobs as jobs

    def boom(db):
        raise RuntimeError("provider down")

    monkeypatch.setattr(jobs, "reconcile_due", boom)
    _call(db, marketing_purpose)
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)

    out = jobs.run_once(today=tomorrow)
    assert out["reconcile"] == "error"
    assert out["recordings"] == {"stored": 0, "failed": 0}
    assert out["digest"] is not None
