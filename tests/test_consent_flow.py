"""End-to-end: a simulated Exotel call producing a defensible consent."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.consent_service import (
    UnknownDigit,
    VerificationRequired,
    attach_call,
    create_session,
    current_consents,
    record_decision,
    record_no_decision,
    record_notice_served,
)
from app.hashchain import verify_chain
from app.models import Consent, ConsentEvent, UcmOutbox

PHONE = "+919876543210"


def _session(db, purpose, phone=PHONE, direction="ivr_outbound"):
    s = create_session(db, direction=direction, phone_raw=phone, purpose_code=purpose.code)
    attach_call(db, s, call_sid="cs-" + s.id[-8:], call_to="+918047115777")
    record_notice_served(db, s)
    db.commit()
    return s


def test_keypress_one_grants_consent(db, marketing_purpose):
    s = _session(db, marketing_purpose)
    result = record_decision(db, s, digit="1")
    db.commit()

    c = result.consent
    assert result.replayed is False
    assert c.decision == "granted"
    assert c.status == "active"
    assert c.permits_processing is True
    assert c.notice_version_id == s.notice_version_id   # the wording they heard
    assert c.expires_at is not None
    assert (c.expires_at - c.decided_at).days == 365


def test_keypress_two_declines(db, marketing_purpose):
    s = _session(db, marketing_purpose)
    c = record_decision(db, s, digit="2").consent
    db.commit()
    assert c.decision == "declined"
    assert c.permits_processing is False


def test_unoffered_key_is_not_a_decision(db, marketing_purpose):
    s = _session(db, marketing_purpose)
    with pytest.raises(UnknownDigit):
        record_decision(db, s, digit="7")
    db.rollback()
    assert db.execute(select(Consent)).scalars().all() == []


def test_silence_is_never_consent(db, marketing_purpose):
    s = _session(db, marketing_purpose)
    record_no_decision(db, s, "no_input")
    db.commit()
    assert db.execute(select(Consent)).scalars().all() == []
    assert s.outcome == "no_input"


def test_replayed_webhook_is_a_noop(db, marketing_purpose):
    """Exotel's retry behaviour is undocumented, so the same call arriving
    twice must not create a second consent."""
    s = _session(db, marketing_purpose)
    first = record_decision(db, s, digit="1")
    db.commit()
    second = record_decision(db, s, digit="1")
    db.commit()

    assert second.replayed is True
    assert second.consent.id == first.consent.id
    assert len(db.execute(select(Consent)).scalars().all()) == 1


def test_withdrawal_supersedes_the_grant_without_erasing_it(db, marketing_purpose):
    grant = record_decision(db, _session(db, marketing_purpose), digit="1").consent
    db.commit()
    grant_id = grant.id

    withdrawal = record_decision(
        db, _session(db, marketing_purpose, direction="ivr_inbound"), digit="9"
    ).consent
    db.commit()

    db.refresh(grant)
    assert grant.is_current is False
    assert grant.superseded_by == withdrawal.id
    assert withdrawal.decision == "withdrawn"
    assert withdrawal.permits_processing is False

    # The original grant is still there to prove it was valid while relied on.
    assert db.get(Consent, grant_id) is not None
    assert db.get(Consent, grant_id).decision == "granted"

    current = current_consents(db, withdrawal.data_principal_id)
    assert [c.id for c in current] == [withdrawal.id]


def test_only_one_current_consent_per_purpose_ever(db, marketing_purpose):
    ids = []
    for digit in ["1", "2", "1", "9", "1"]:
        ids.append(record_decision(db, _session(db, marketing_purpose), digit=digit).consent.id)
        db.commit()

    rows = db.execute(
        select(Consent).where(Consent.is_current.is_(True))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == ids[-1]
    assert len(db.execute(select(Consent)).scalars().all()) == 5


def test_purpose_requiring_verification_refuses_ani_only(db, verified_purpose):
    s = _session(db, verified_purpose)
    with pytest.raises(VerificationRequired):
        record_decision(db, s, digit="1")
    db.rollback()

    s2 = _session(db, verified_purpose)
    c = record_decision(db, s2, digit="1", verification_level="verified").consent
    db.commit()
    assert c.verification_level == "verified"


def test_consent_and_audit_and_outbox_land_together(db, marketing_purpose):
    s = _session(db, marketing_purpose)
    c = record_decision(db, s, digit="1").consent
    db.commit()

    outbox = db.execute(
        select(UcmOutbox).where(UcmOutbox.consent_id == c.id)
    ).scalar_one()
    assert outbox.idempotency_key == c.id
    assert outbox.delivered_at is None
    assert outbox.payload["decision"] == "granted"
    assert outbox.payload["purpose_key"] == "marketing_outreach"
    assert outbox.payload["notice"]["sha256"]

    events = db.execute(
        select(ConsentEvent).order_by(ConsentEvent.seq)
    ).scalars().all()
    assert [e.event_type for e in events] == [
        "session.created", "notice.served", "consent.granted",
    ]


def test_notice_is_served_before_the_decision(db, marketing_purpose):
    """Notice precedes consent -- provable from the chain, not from trust."""
    s = _session(db, marketing_purpose)
    c = record_decision(db, s, digit="1").consent
    db.commit()

    events = db.execute(
        select(ConsentEvent).order_by(ConsentEvent.seq)
    ).scalars().all()
    notice_seq = next(e.seq for e in events if e.event_type == "notice.served")
    grant_seq = next(e.seq for e in events if e.event_type == "consent.granted")
    assert notice_seq < grant_seq


def test_chain_verifies(db, marketing_purpose):
    c = record_decision(db, _session(db, marketing_purpose), digit="1").consent
    db.commit()
    ok, broken = verify_chain(db, c.data_principal_id)
    assert ok is True and broken is None


def test_tampering_breaks_the_chain(db, marketing_purpose):
    c = record_decision(db, _session(db, marketing_purpose), digit="1").consent
    db.commit()

    ev = db.execute(
        select(ConsentEvent).where(ConsentEvent.event_type == "consent.granted")
    ).scalar_one()
    ev.payload = dict(ev.payload, digit="2")   # rewrite history
    db.commit()

    ok, broken = verify_chain(db, c.data_principal_id)
    assert ok is False
    assert broken == str(ev.seq)
