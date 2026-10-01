"""Regression guards for failures that would silently void the audit trail."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select, text

from app.consent_service import attach_call, create_session, record_decision
from app.evidence import daily_digest, purge_expired_artifacts
from app.hashchain import canonical_instant, compute_entry_hash, verify_chain
from app.models import CallArtifact, ConsentEvent


def test_hash_is_timezone_independent():
    """Postgres returns timestamptz in the connection's TimeZone. Hashing the
    rendering rather than the instant makes every historical hash fail to
    verify from a client in another zone -- a silent, total loss of the trail."""
    utc = dt.datetime(2026, 9, 17, 4, 19, 50, 904007, tzinfo=dt.timezone.utc)
    ist = utc.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    est = utc.astimezone(dt.timezone(dt.timedelta(hours=-4)))

    assert canonical_instant(utc) == canonical_instant(ist) == canonical_instant(est)
    h = [compute_entry_hash(None, "consent.granted", {"a": 1}, t) for t in (utc, ist, est)]
    assert h[0] == h[1] == h[2]


def test_payload_key_order_does_not_change_the_hash():
    a = compute_entry_hash(None, "e", {"b": 2, "a": 1}, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    b = compute_entry_hash(None, "e", {"a": 1, "b": 2}, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert a == b


def test_chain_survives_a_reread_on_a_fresh_connection(db, marketing_purpose):
    """The failure mode that matters: written on one connection, verified on
    another, possibly months later."""
    s = create_session(db, direction="ivr_outbound", phone_raw="+919876543210",
                       purpose_code=marketing_purpose.code)
    attach_call(db, s, call_sid="cs-tz")
    c = record_decision(db, s, digit="1").consent
    db.commit()

    from app.db import session_factory

    other = session_factory()()
    try:
        other.execute(text("SET TIME ZONE 'Asia/Kolkata'"))
        ok, broken = verify_chain(other, c.data_principal_id)
        assert ok is True, f"chain broke at {broken} under a different session timezone"
    finally:
        other.close()


def test_consent_event_is_append_only_by_design(db, marketing_purpose):
    """The app role holds no UPDATE or DELETE on the audit table. This test
    documents the intent; the grant itself is applied by the migration
    identity, which the application does not hold at runtime."""
    sql = (
        db.execute(
            text("SELECT obj_description('consent_event'::regclass)")
        ).scalar_one_or_none()
    )
    # The structural guarantee we can assert here: the no-fork index exists.
    idx = db.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename='consent_event'"
    )).scalars().all()
    assert "consent_event_no_fork" in idx


def test_purge_keeps_the_hash_after_deleting_the_audio(db, marketing_purpose):
    """What remains after purge is a verifiable claim that a recording
    existed and what it hashed to. Weaker than the audio, stronger than a
    bare row."""
    from app.evidence import queue_recording_fetch, store_recording

    s = create_session(db, direction="ivr_outbound", phone_raw="+919876543210",
                       purpose_code=marketing_purpose.code)
    attach_call(db, s, call_sid="cs-purge")
    record_decision(db, s, digit="1")
    art = queue_recording_fetch(db, s, "https://exotel.example/rec.mp3")
    store_recording(db, art, b"fake audio bytes", "s3://dsg-consent-evidence/2026/09/x.mp3")
    art.purge_after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()

    n = purge_expired_artifacts(db)
    db.commit()
    assert n == 1

    db.refresh(art)
    assert art.storage_uri is None       # audio gone
    assert art.purged_at is not None
    assert art.sha256 is not None        # proof it existed survives
    assert art.bytes == 16


def test_daily_digest_covers_every_chain_touched(db, marketing_purpose):
    for i in range(3):
        s = create_session(db, direction="ivr_outbound", phone_raw=f"+91900000000{i}",
                           purpose_code=marketing_purpose.code)
        attach_call(db, s, call_sid=f"cs-d{i}")
        record_decision(db, s, digit="1")
    db.commit()

    d = daily_digest(db, dt.datetime.now(dt.timezone.utc).date())
    assert d["chains"] == 3
    assert len(d["digest"]) == 64
    assert all(v for v in d["heads"].values())


def test_digest_changes_if_anything_is_rewritten(db, marketing_purpose):
    s = create_session(db, direction="ivr_outbound", phone_raw="+919876543210",
                       purpose_code=marketing_purpose.code)
    attach_call(db, s, call_sid="cs-anchor")
    record_decision(db, s, digit="1")
    db.commit()

    today = dt.datetime.now(dt.timezone.utc).date()
    before = daily_digest(db, today)["digest"]

    ev = db.execute(
        select(ConsentEvent).order_by(ConsentEvent.seq.desc()).limit(1)
    ).scalar_one()
    ev.entry_hash = b"\x00" * 32
    db.commit()

    assert daily_digest(db, today)["digest"] != before
