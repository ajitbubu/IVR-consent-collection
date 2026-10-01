"""Append-only, hash-linked event log.

One chain per Data Principal: verification cost is bounded by that person's
history rather than by total system volume, and the write lock is already
held on their row.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import canonical_json, sha256
from app.models import ConsentEvent


def canonical_instant(ts: dt.datetime) -> str:
    """Hash the instant, never its rendering.

    Postgres returns timestamptz in the connection's TimeZone, so the same
    moment comes back as -04:00 on one connection and +05:30 on another.
    Hashing isoformat() directly makes every historical hash fail to verify
    the moment a client's timezone differs from the writer's -- which is a
    silent, total loss of the audit trail. Normalise to UTC first.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).isoformat()


def compute_entry_hash(
    prev_hash: bytes | None,
    event_type: str,
    payload: Any,
    occurred_at: dt.datetime,
) -> bytes:
    return sha256(
        (prev_hash or b"")
        + event_type.encode()
        + canonical_json(payload)
        + canonical_instant(occurred_at).encode()
    )


def append_event(
    db: Session,
    *,
    chain_key: str,
    event_type: str,
    payload: dict,
    consent_id: str | None = None,
    ivr_session_id: str | None = None,
    occurred_at: dt.datetime | None = None,
) -> ConsentEvent:
    """Caller must already hold the row lock on the Data Principal, so that
    two writers cannot read the same head and fork the chain."""
    occurred_at = occurred_at or dt.datetime.now(dt.timezone.utc)
    head = db.execute(
        select(ConsentEvent.entry_hash)
        .where(ConsentEvent.chain_key == chain_key)
        .order_by(ConsentEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()

    entry_hash = compute_entry_hash(head, event_type, payload, occurred_at)
    ev = ConsentEvent(
        chain_key=chain_key,
        consent_id=consent_id,
        ivr_session_id=ivr_session_id,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
        prev_hash=head,
        entry_hash=entry_hash,
    )
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session, chain_key: str) -> tuple[bool, str | None]:
    """Walk a chain from the start. Returns (ok, first_broken_seq)."""
    events = db.execute(
        select(ConsentEvent)
        .where(ConsentEvent.chain_key == chain_key)
        .order_by(ConsentEvent.seq.asc())
    ).scalars().all()

    prev: bytes | None = None
    for ev in events:
        if ev.prev_hash != prev:
            return False, str(ev.seq)
        expected = compute_entry_hash(ev.prev_hash, ev.event_type, ev.payload, ev.occurred_at)
        if expected != ev.entry_hash:
            return False, str(ev.seq)
        prev = ev.entry_hash
    return True, None


def chain_head(db: Session, chain_key: str) -> bytes | None:
    return db.execute(
        select(ConsentEvent.entry_hash)
        .where(ConsentEvent.chain_key == chain_key)
        .order_by(ConsentEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
