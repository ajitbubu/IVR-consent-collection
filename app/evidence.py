"""Recording ingest, chain anchoring, and the evidence bundle."""
from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from app.hashchain import chain_head, verify_chain
from app.models import (
    CallArtifact,
    Consent,
    ConsentEvent,
    DataPrincipal,
    IvrSession,
    NoticeVersion,
    Purpose,
)


def queue_recording_fetch(db: Session, sess: IvrSession, exotel_url: str) -> CallArtifact:
    """Record the intent to fetch. A worker copies the object into our own
    bucket, hashes it, and fills storage_uri -- Exotel's URL is never the
    long-term reference."""
    purpose = db.get(Purpose, sess.purpose_id) if sess.purpose_id else None
    retention_days = purpose.retention_days if purpose else 365

    existing = db.execute(
        select(CallArtifact).where(
            CallArtifact.ivr_session_id == sess.id,
            CallArtifact.kind == "recording",
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    art = CallArtifact(
        id=str(ULID()),
        ivr_session_id=sess.id,
        kind="recording",
        storage_uri=None,
        purge_after=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=retention_days),
    )
    db.add(art)
    db.flush()
    return art


def store_recording(
    db: Session, artifact: CallArtifact, content: bytes, storage_uri: str
) -> CallArtifact:
    artifact.storage_uri = storage_uri
    artifact.sha256 = hashlib.sha256(content).digest()
    artifact.bytes = len(content)
    db.flush()
    return artifact


def purge_expired_artifacts(db: Session, now: dt.datetime | None = None) -> int:
    """Delete the object, keep the row and its hash. What remains is a
    verifiable claim that a recording existed and what it hashed to."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = list(
        db.execute(
            select(CallArtifact).where(
                CallArtifact.purge_after <= now,
                CallArtifact.purged_at.is_(None),
            )
        ).scalars()
    )
    for art in rows:
        # delete_object(art.storage_uri) -- storage backend call
        art.storage_uri = None
        art.purged_at = now
    db.flush()
    return len(rows)


def daily_digest(db: Session, day: dt.date) -> dict:
    """Head hash of every chain touched that day, folded into one digest.
    The digest goes to WORM storage: rewriting history then means forging a
    value committed to immutable storage before the dispute existed."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    keys = list(
        db.execute(
            select(ConsentEvent.chain_key)
            .where(ConsentEvent.occurred_at >= start, ConsentEvent.occurred_at < end)
            .distinct()
            .order_by(ConsentEvent.chain_key)
        ).scalars()
    )
    h = hashlib.sha256()
    heads = {}
    for k in keys:
        head = chain_head(db, k)
        heads[k] = head.hex() if head else None
        h.update(k.encode() + (head or b""))
    return {"date": day.isoformat(), "chains": len(keys), "digest": h.hexdigest(), "heads": heads}


def evidence_bundle(db: Session, consent_id: str) -> dict:
    """Everything a Data Principal, the DPO or the Board would need. A read
    with no human judgement in it -- evidence you assemble by hand is
    evidence you assemble inconsistently."""
    consent = db.get(Consent, consent_id)
    if consent is None:
        raise KeyError(consent_id)

    principal = db.get(DataPrincipal, consent.data_principal_id)
    purpose = db.get(Purpose, consent.purpose_id)
    notice = db.get(NoticeVersion, consent.notice_version_id)
    sess = db.get(IvrSession, consent.ivr_session_id) if consent.ivr_session_id else None

    events = list(
        db.execute(
            select(ConsentEvent)
            .where(ConsentEvent.chain_key == principal.id)
            .order_by(ConsentEvent.seq.asc())
        ).scalars()
    )
    chain_ok, broken_at = verify_chain(db, principal.id)

    artifacts = []
    if sess is not None:
        for art in db.execute(
            select(CallArtifact).where(CallArtifact.ivr_session_id == sess.id)
        ).scalars():
            artifacts.append({
                "kind": art.kind,
                "storage_uri": art.storage_uri,
                "sha256": art.sha256.hex() if art.sha256 else None,
                "bytes": art.bytes,
                "purged_at": art.purged_at.isoformat() if art.purged_at else None,
            })

    return {
        "consent": {
            "id": consent.id,
            "decision": consent.decision,
            "status": consent.status,
            "is_current": consent.is_current,
            "purpose": purpose.code,
            "channel": consent.channel,
            "language": consent.language,
            "verification_level": consent.verification_level,
            "decided_at": consent.decided_at.isoformat(),
            "expires_at": consent.expires_at.isoformat() if consent.expires_at else None,
        },
        "data_principal": {"id": principal.id, "phone_e164": principal.phone_e164},
        "notice": {
            "version_id": notice.id,
            "version": notice.version,
            "language": notice.language,
            "body_text": notice.body_text,
            "sha256": notice.body_sha256.hex(),
            "audio_url": notice.audio_url,
        },
        "call": None if sess is None else {
            "call_sid": sess.call_sid,
            "direction": sess.direction,
            "started_at": sess.started_at.isoformat(),
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
            "answered_by": sess.answered_by,
            "outcome": sess.outcome,
            "reconciled_at": sess.reconciled_at.isoformat() if sess.reconciled_at else None,
        },
        "artifacts": artifacts,
        "chain": {
            "verified": chain_ok,
            "broken_at_seq": broken_at,
            "head": (chain_head(db, principal.id) or b"").hex() or None,
            "events": [
                {
                    "seq": e.seq,
                    "event_type": e.event_type,
                    "occurred_at": e.occurred_at.isoformat(),
                    "payload": e.payload,
                    "prev_hash": e.prev_hash.hex() if e.prev_hash else None,
                    "entry_hash": e.entry_hash.hex(),
                }
                for e in events
            ],
        },
    }
