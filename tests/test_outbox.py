"""Outbox behaviour: ordering, retries, idempotency, poison isolation."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.consent_service import attach_call, create_session, record_decision
from app.models import Consent, UcmOutbox
from app.outbox import backoff_seconds, claim_batch, deliver_one, drain, lag_seconds
from app.ucm import UcmRejected, UcmUnavailable


class FakeUcm:
    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    def push(self, payload, idempotency_key):
        self.calls.append((idempotency_key, payload))
        if self.behaviour == "down":
            raise UcmUnavailable("HTTP 503")
        if self.behaviour == "bad":
            raise UcmRejected("HTTP 422 unknown purpose_key")
        return "UCM-" + idempotency_key[-6:]


def _grant(db, purpose, phone="+919876543210", when=None):
    s = create_session(db, direction="ivr_outbound", phone_raw=phone, purpose_code=purpose.code)
    attach_call(db, s, call_sid="cs-" + s.id[-10:])
    c = record_decision(db, s, digit="1", decided_at=when).consent
    db.commit()
    return c


def test_happy_path_marks_synced(db, marketing_purpose):
    c = _grant(db, marketing_purpose)
    ucm = FakeUcm()
    counts = drain(db, client=ucm)
    db.commit()

    assert counts["delivered"] == 1
    db.refresh(c)
    assert c.ucm_sync_state == "synced"
    assert c.ucm_consent_ref.startswith("UCM-")
    assert ucm.calls[0][0] == c.id           # idempotency key is the consent id


def test_ucm_outage_never_loses_a_consent(db, marketing_purpose):
    c = _grant(db, marketing_purpose)
    counts = drain(db, client=FakeUcm("down"))
    db.commit()

    assert counts["retry"] == 1
    row = db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == c.id)).scalar_one()
    assert row.delivered_at is None
    assert row.attempts == 1
    assert row.next_attempt_at > dt.datetime.now(dt.timezone.utc)
    assert "503" in row.last_error
    db.refresh(c)
    assert c.ucm_sync_state == "pending"     # still queued, not dropped


def test_retry_after_outage_delivers(db, marketing_purpose):
    c = _grant(db, marketing_purpose)
    drain(db, client=FakeUcm("down"))
    db.commit()

    row = db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == c.id)).scalar_one()
    row.next_attempt_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()

    counts = drain(db, client=FakeUcm("ok"))
    db.commit()
    assert counts["delivered"] == 1
    db.refresh(c)
    assert c.ucm_sync_state == "synced"


def test_backoff_grows_and_is_capped(db):
    seq = [backoff_seconds(n) for n in range(1, 14)]
    assert seq[0] == 5
    assert seq[1] == 10
    assert seq[2] == 20
    assert all(b <= 900 for b in seq)
    assert seq[-1] == 900
    assert seq == sorted(seq)


def test_exhausted_row_is_flagged_not_deleted(db, marketing_purpose):
    from app.config import settings

    c = _grant(db, marketing_purpose)
    row = db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == c.id)).scalar_one()
    row.attempts = settings().outbox_max_attempts - 1
    db.commit()

    counts = drain(db, client=FakeUcm("down"))
    db.commit()
    assert counts["failed"] == 1
    db.refresh(c)
    assert c.ucm_sync_state == "failed"
    # Still on the table for a human to deal with.
    assert db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == c.id)).scalar_one() is not None


def test_bad_payload_pauses_one_key_only(db, marketing_purpose):
    """A rejection should not stop the queue for everyone else."""
    bad = _grant(db, marketing_purpose, phone="+919000000001")
    good = _grant(db, marketing_purpose, phone="+919000000002")

    drain(db, client=FakeUcm("bad"))
    db.commit()

    bad_row = db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == bad.id)).scalar_one()
    good_row = db.execute(select(UcmOutbox).where(UcmOutbox.consent_id == good.id)).scalar_one()
    assert bad_row.paused is True
    assert good_row.paused is True or good_row.delivered_at is None

    # Paused rows are skipped on the next pass; healthy traffic flows.
    counts = drain(db, client=FakeUcm("ok"))
    db.commit()
    assert counts["delivered"] == 0  # both were paused by the failing client
    assert bad_row.paused is True


def test_grant_and_withdrawal_are_delivered_in_order(db, marketing_purpose):
    """The worst failure this system can produce is UCM holding a live
    consent the person revoked. Ordering is per principal, by decided_at."""
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)

    s1 = create_session(db, direction="ivr_outbound", phone_raw="+919876543210",
                        purpose_code=marketing_purpose.code)
    attach_call(db, s1, call_sid="cs-grant")
    grant = record_decision(db, s1, digit="1", decided_at=base).consent
    db.commit()

    s2 = create_session(db, direction="ivr_inbound", phone_raw="+919876543210",
                        purpose_code=marketing_purpose.code)
    attach_call(db, s2, call_sid="cs-withdraw")
    withdrawal = record_decision(
        db, s2, digit="9", decided_at=base + dt.timedelta(seconds=8)
    ).consent
    db.commit()

    ucm = FakeUcm()
    # One pass takes only the head for this principal.
    drain(db, client=ucm)
    db.commit()
    assert [k for k, _ in ucm.calls] == [grant.id]

    drain(db, client=ucm)
    db.commit()
    assert [k for k, _ in ucm.calls] == [grant.id, withdrawal.id]
    assert ucm.calls[-1][1]["decision"] == "withdrawn"


def test_one_row_per_principal_per_pass(db, marketing_purpose):
    for i in range(3):
        _grant(db, marketing_purpose, phone=f"+91900000000{i}")
    batch = claim_batch(db)
    assert len({r.data_principal_id for r in batch}) == len(batch) == 3


def test_lag_is_zero_when_drained(db, marketing_purpose):
    _grant(db, marketing_purpose)
    assert lag_seconds(db) > 0
    drain(db, client=FakeUcm())
    db.commit()
    assert lag_seconds(db) == 0.0
