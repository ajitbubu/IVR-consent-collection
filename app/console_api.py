"""Read APIs the admin console runs on.

Deliberately separate from /v1: these are operator-facing, they page, and
they never return decrypted identity attributes in list views.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.identity import normalise_e164
from app.models import (
    Consent,
    ConsentEvent,
    DataPrincipal,
    IvrSession,
    NoticeVersion,
    Purpose,
    UcmOutbox,
    WebhookReceipt,
)
from app.outbox import lag_seconds
from app.telephony import names as provider_names

router = APIRouter(prefix="/api/console", tags=["console"])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _mask(phone: str) -> str:
    """List views never show a full number. The detail view does, for an
    operator who has already been given a reason to look."""
    return phone[:3] + "•" * max(len(phone) - 7, 0) + phone[-4:] if len(phone) > 7 else phone


@router.get("/consents")
def list_consents(
    q: str | None = Query(None, description="phone in any shape, or a consent id"),
    decision: str | None = None,
    status: str | None = None,
    purpose: str | None = None,
    provider: str | None = None,
    sync: str | None = None,
    current_only: bool = True,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_session),
) -> dict:
    stmt = (
        select(Consent, Purpose, DataPrincipal)
        .join(Purpose, Purpose.id == Consent.purpose_id)
        .join(DataPrincipal, DataPrincipal.id == Consent.data_principal_id)
    )
    if current_only:
        stmt = stmt.where(Consent.is_current.is_(True))
    if decision:
        stmt = stmt.where(Consent.decision == decision)
    if status:
        stmt = stmt.where(Consent.status == status)
    if purpose:
        stmt = stmt.where(Purpose.code == purpose)
    if provider:
        stmt = stmt.where(Consent.provider == provider)
    if sync:
        stmt = stmt.where(Consent.ucm_sync_state == sync)
    if q:
        q = q.strip()
        if q.startswith("01") and len(q) == 26:
            stmt = stmt.where(Consent.id == q)
        else:
            try:
                stmt = stmt.where(DataPrincipal.phone_e164 == normalise_e164(q))
            except Exception:
                stmt = stmt.where(DataPrincipal.phone_e164.like(f"%{q}%"))

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    rows = db.execute(
        stmt.order_by(Consent.decided_at.desc()).limit(limit).offset(offset)
    ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "consent_id": c.id,
                "phone_masked": _mask(dp.phone_e164),
                "data_principal_id": dp.id,
                "purpose_key": p.code,
                "purpose_name": p.name,
                "decision": c.decision,
                "status": c.status,
                "permits_processing": c.permits_processing,
                "is_current": c.is_current,
                "provider": c.provider,
                "channel": c.channel,
                "language": c.language,
                "verification_level": c.verification_level,
                "decided_at": c.decided_at.isoformat(),
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "ucm_sync_state": c.ucm_sync_state,
            }
            for c, p, dp in rows
        ],
    }


@router.get("/consents/{consent_id}")
def consent_detail(consent_id: str, db: Session = Depends(get_session)) -> dict:
    c = db.get(Consent, consent_id)
    if c is None:
        raise HTTPException(404, "unknown consent")
    p = db.get(Purpose, c.purpose_id)
    dp = db.get(DataPrincipal, c.data_principal_id)
    nv = db.get(NoticeVersion, c.notice_version_id)
    sess = db.get(IvrSession, c.ivr_session_id) if c.ivr_session_id else None

    history = db.execute(
        select(Consent)
        .where(Consent.data_principal_id == c.data_principal_id,
               Consent.purpose_id == c.purpose_id)
        .order_by(Consent.decided_at.desc())
    ).scalars().all()

    receipts = []
    if sess is not None:
        receipts = [
            {
                "route": r.route,
                "provider": r.provider,
                "signature_ok": r.signature_ok,
                "has_signature": r.signature is not None,
                "received_at": r.received_at.isoformat(),
            }
            for r in db.execute(
                select(WebhookReceipt)
                .where(WebhookReceipt.ivr_session_id == sess.id)
                .order_by(WebhookReceipt.received_at)
            ).scalars()
        ]

    outbox = db.execute(
        select(UcmOutbox).where(UcmOutbox.consent_id == c.id)
    ).scalar_one_or_none()

    return {
        "consent": {
            "consent_id": c.id, "decision": c.decision, "status": c.status,
            "is_current": c.is_current, "superseded_by": c.superseded_by,
            "permits_processing": c.permits_processing, "provider": c.provider,
            "channel": c.channel, "language": c.language,
            "verification_level": c.verification_level,
            "decided_at": c.decided_at.isoformat(),
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "ucm_sync_state": c.ucm_sync_state, "ucm_consent_ref": c.ucm_consent_ref,
        },
        "data_principal": {"id": dp.id, "phone_e164": dp.phone_e164,
                           "external_ref": dp.external_ref},
        "purpose": {"code": p.code, "name": p.name, "retention_days": p.retention_days,
                    "requires_verification": p.requires_verification},
        "notice": {"id": nv.id, "version": nv.version, "language": nv.language,
                   "body_text": nv.body_text, "sha256": nv.body_sha256.hex(),
                   "audio_url": nv.audio_url},
        "call": None if sess is None else {
            "session_id": sess.id, "call_sid": sess.call_sid, "provider": sess.provider,
            "direction": sess.direction, "outcome": sess.outcome,
            "answered_by": sess.answered_by,
            "started_at": sess.started_at.isoformat(),
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
            "reconciled_at": sess.reconciled_at.isoformat() if sess.reconciled_at else None,
        },
        "webhook_receipts": receipts,
        "outbox": None if outbox is None else {
            "attempts": outbox.attempts, "paused": outbox.paused,
            "last_error": outbox.last_error,
            "delivered_at": outbox.delivered_at.isoformat() if outbox.delivered_at else None,
            "next_attempt_at": outbox.next_attempt_at.isoformat(),
        },
        "history": [
            {"consent_id": h.id, "decision": h.decision, "status": h.status,
             "decided_at": h.decided_at.isoformat(), "channel": h.channel,
             "provider": h.provider, "is_current": h.is_current}
            for h in history
        ],
    }


@router.get("/stats")
def stats(days: int = Query(30, le=365), db: Session = Depends(get_session)) -> dict:
    since = _now() - dt.timedelta(days=days)

    by_decision = dict(
        db.execute(
            select(Consent.decision, func.count())
            .where(Consent.decided_at >= since)
            .group_by(Consent.decision)
        ).all()
    )
    by_provider = dict(
        db.execute(
            select(Consent.provider, func.count())
            .where(Consent.decided_at >= since)
            .group_by(Consent.provider)
        ).all()
    )
    by_purpose = [
        {"purpose_key": code, "granted": g or 0, "declined": d or 0, "withdrawn": w or 0}
        for code, g, d, w in db.execute(
            select(
                Purpose.code,
                func.sum(func.cast(Consent.decision == "granted", Integer)),
                func.sum(func.cast(Consent.decision == "declined", Integer)),
                func.sum(func.cast(Consent.decision == "withdrawn", Integer)),
            )
            .join(Consent, Consent.purpose_id == Purpose.id)
            .where(Consent.decided_at >= since)
            .group_by(Purpose.code)
            .order_by(Purpose.code)
        ).all()
    ]
    daily = [
        {"day": d.date().isoformat(), "granted": g or 0, "declined": dec or 0,
         "withdrawn": w or 0}
        for d, g, dec, w in db.execute(
            select(
                func.date_trunc("day", Consent.decided_at).label("d"),
                func.sum(func.cast(Consent.decision == "granted", Integer)),
                func.sum(func.cast(Consent.decision == "declined", Integer)),
                func.sum(func.cast(Consent.decision == "withdrawn", Integer)),
            )
            .where(Consent.decided_at >= since)
            .group_by("d").order_by("d")
        ).all()
    ]

    session_outcomes = dict(
        db.execute(
            select(IvrSession.outcome, func.count())
            .where(IvrSession.started_at >= since)
            .group_by(IvrSession.outcome)
        ).all()
    )

    granted = by_decision.get("granted", 0)
    decided = granted + by_decision.get("declined", 0)

    return {
        "window_days": days,
        "totals": {
            "consents": sum(by_decision.values()),
            "granted": granted,
            "declined": by_decision.get("declined", 0),
            "withdrawn": by_decision.get("withdrawn", 0),
            "grant_rate": round(granted / decided, 4) if decided else None,
        },
        "by_provider": by_provider,
        "by_purpose": by_purpose,
        "daily": daily,
        "session_outcomes": session_outcomes,
        "providers_available": provider_names(),
    }


@router.get("/health")
def console_health(db: Session = Depends(get_session)) -> dict:
    """The four numbers an operator actually needs at a glance."""
    pending = db.execute(
        select(func.count()).select_from(UcmOutbox)
        .where(UcmOutbox.delivered_at.is_(None), UcmOutbox.paused.is_(False))
    ).scalar_one()
    paused = db.execute(
        select(func.count()).select_from(UcmOutbox).where(UcmOutbox.paused.is_(True))
    ).scalar_one()
    failed = db.execute(
        select(func.count()).select_from(Consent).where(Consent.ucm_sync_state == "failed")
    ).scalar_one()
    unreconciled = db.execute(
        select(func.count()).select_from(IvrSession)
        .where(IvrSession.reconciled_at.is_(None),
               IvrSession.started_at < _now() - dt.timedelta(minutes=5))
    ).scalar_one()
    # An unsigned-but-expected receipt means either a misconfiguration or a
    # forgery attempt. Either way somebody should look.
    bad_signatures = db.execute(
        select(func.count()).select_from(WebhookReceipt)
        .where(WebhookReceipt.signature_ok.is_(False))
    ).scalar_one()

    lag = lag_seconds(db)
    return {
        "outbox_pending": pending,
        "outbox_paused": paused,
        "sync_failed": failed,
        "unreconciled_sessions": unreconciled,
        "failed_signature_checks": bad_signatures,
        "outbox_lag_seconds": round(lag, 1),
        "status": "ok" if lag < 3600 and paused == 0 and bad_signatures == 0 else "attention",
    }


@router.get("/sessions")
def list_sessions(
    limit: int = Query(50, le=200),
    provider: str | None = None,
    outcome: str | None = None,
    db: Session = Depends(get_session),
) -> dict:
    stmt = select(IvrSession, Purpose).outerjoin(Purpose, Purpose.id == IvrSession.purpose_id)
    if provider:
        stmt = stmt.where(IvrSession.provider == provider)
    if outcome:
        stmt = stmt.where(IvrSession.outcome == outcome)
    rows = db.execute(stmt.order_by(IvrSession.started_at.desc()).limit(limit)).all()
    return {
        "items": [
            {
                "session_id": s.id, "call_sid": s.call_sid, "provider": s.provider,
                "direction": s.direction, "outcome": s.outcome,
                "purpose_key": p.code if p else None,
                "answered_by": s.answered_by,
                "started_at": s.started_at.isoformat(),
                "reconciled": s.reconciled_at is not None,
            }
            for s, p in rows
        ]
    }


@router.get("/purposes")
def list_purposes(db: Session = Depends(get_session)) -> dict:
    out = []
    for p in db.execute(select(Purpose).order_by(Purpose.code)).scalars():
        notices = db.execute(
            select(NoticeVersion).where(NoticeVersion.purpose_id == p.id)
            .order_by(NoticeVersion.language, NoticeVersion.version.desc())
        ).scalars().all()
        live = db.execute(
            select(func.count()).select_from(Consent)
            .where(Consent.purpose_id == p.id, Consent.is_current.is_(True),
                   Consent.status == "active")
        ).scalar_one()
        out.append({
            "purpose_key": p.code, "name": p.name,
            "retention_days": p.retention_days,
            "requires_verification": p.requires_verification,
            "ucm_purpose_key": p.ucm_purpose_key,
            "active_consents": live,
            "notices": [
                {"id": n.id, "version": n.version, "language": n.language,
                 "published": n.published_at is not None,
                 "retired": n.retired_at is not None,
                 "sha256": n.body_sha256.hex()[:16],
                 "has_audio": bool(n.audio_url),
                 "body_text": n.body_text}
                for n in notices
            ],
        })
    return {"items": out}


@router.get("/events")
def recent_events(limit: int = Query(100, le=500), db: Session = Depends(get_session)) -> dict:
    rows = db.execute(
        select(ConsentEvent).order_by(ConsentEvent.seq.desc()).limit(limit)
    ).scalars().all()
    return {
        "items": [
            {"seq": e.seq, "event_type": e.event_type,
             "occurred_at": e.occurred_at.isoformat(),
             "consent_id": e.consent_id, "session_id": e.ivr_session_id,
             "payload": e.payload}
            for e in rows
        ]
    }
