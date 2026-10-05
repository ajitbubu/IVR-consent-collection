"""Service-facing API. mTLS in production; the certificate subject maps to a role."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.consent_service import (
    ConsentError,
    create_session,
    current_consents,
    record_decision,
)
from app.db import get_session
from app.evidence import evidence_bundle
from app.identity import get_or_create_principal, normalise_e164
from app.models import Consent, DataPrincipal, IvrSession, NoticeVersion, Purpose
from app.outbox import lag_seconds

router = APIRouter(prefix="/v1", tags=["service"])


class SessionCreate(BaseModel):
    direction: str = Field(pattern="^(ivr_inbound|ivr_outbound)$")
    phone_e164: str
    purpose_key: str
    language: str = "eng"
    provider: str = Field(default="exotel", pattern="^(exotel|twilio)$")


class SessionCreated(BaseModel):
    session_id: str
    custom_field: str
    notice_version_id: str
    provider: str


@router.post("/sessions", response_model=SessionCreated, status_code=201)
def create_session_endpoint(
    body: SessionCreate, db: Session = Depends(get_session)
) -> SessionCreated:
    """The id must exist before the call so it can ride in Exotel's
    CustomField, which caps at 128 characters. A ULID is 26."""
    try:
        sess = create_session(
            db,
            direction=body.direction,
            phone_raw=body.phone_e164,
            purpose_code=body.purpose_key,
            language=body.language,
            provider=body.provider,
        )
    except ConsentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SessionCreated(
        session_id=sess.id,
        custom_field=sess.id,
        notice_version_id=sess.notice_version_id,
        provider=sess.provider,
    )


@router.get("/consents")
def read_consents(
    phone_e164: str | None = Query(None),
    data_principal_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> dict:
    if not phone_e164 and not data_principal_id:
        raise HTTPException(400, "phone_e164 or data_principal_id is required")

    if data_principal_id:
        dp = db.get(DataPrincipal, data_principal_id)
    else:
        dp = db.execute(
            select(DataPrincipal).where(
                DataPrincipal.phone_e164 == normalise_e164(phone_e164)
            )
        ).scalar_one_or_none()
    if dp is None:
        raise HTTPException(404, "unknown data principal")

    out = []
    for c in current_consents(db, dp.id):
        purpose = db.get(Purpose, c.purpose_id)
        out.append({
            "consent_id": c.id,
            "purpose_key": purpose.code,
            "status": c.status,
            "decision": c.decision,
            "permits_processing": c.permits_processing,
            "decided_at": c.decided_at.isoformat(),
            "channel": c.channel,
            "verification_level": c.verification_level,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "ucm_sync_state": c.ucm_sync_state,
        })
    return {"data_principal_id": dp.id, "consents": out}


@router.get("/consents/{consent_id}")
def read_consent(consent_id: str, db: Session = Depends(get_session)) -> dict:
    c = db.get(Consent, consent_id)
    if c is None:
        raise HTTPException(404, "unknown consent")
    purpose = db.get(Purpose, c.purpose_id)
    return {
        "consent_id": c.id,
        "data_principal_id": c.data_principal_id,
        "purpose_key": purpose.code,
        "decision": c.decision,
        "status": c.status,
        "is_current": c.is_current,
        "superseded_by": c.superseded_by,
        "permits_processing": c.permits_processing,
        "decided_at": c.decided_at.isoformat(),
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "notice_version_id": c.notice_version_id,
        "ucm_sync_state": c.ucm_sync_state,
        "ucm_consent_ref": c.ucm_consent_ref,
    }


@router.get("/consents/{consent_id}/evidence")
def read_evidence(consent_id: str, db: Session = Depends(get_session)) -> dict:
    try:
        return evidence_bundle(db, consent_id)
    except KeyError as exc:
        raise HTTPException(404, "unknown consent") from exc


class WithdrawRequest(BaseModel):
    purpose_key: str
    channel: str = Field(default="agent", pattern="^(ivr_inbound|ivr_outbound|web|app|agent)$")
    reason: str | None = None


@router.post("/consents/withdraw", status_code=201)
def withdraw(
    body: WithdrawRequest,
    phone_e164: str = Query(...),
    db: Session = Depends(get_session),
) -> dict:
    """Withdrawal arriving from a channel other than the IVR. Creates a new
    row -- it never deletes the evidence of the original grant, because
    proving you had a valid consent for the period you relied on it is
    exactly what you will be asked for."""
    try:
        sess = create_session(
            db,
            direction=body.channel,
            phone_raw=phone_e164,
            purpose_code=body.purpose_key,
        )
        result = record_decision(db, sess, decision="withdrawn")
    except ConsentError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"consent_id": result.consent.id, "status": result.consent.status}


@router.get("/purposes")
def list_purposes(db: Session = Depends(get_session)) -> list[dict]:
    out = []
    for p in db.execute(select(Purpose).order_by(Purpose.code)).scalars():
        notices = db.execute(
            select(NoticeVersion).where(
                NoticeVersion.purpose_id == p.id,
                NoticeVersion.published_at.isnot(None),
                NoticeVersion.retired_at.is_(None),
            )
        ).scalars()
        out.append({
            "purpose_key": p.code,
            "name": p.name,
            "retention_days": p.retention_days,
            "requires_verification": p.requires_verification,
            "ucm_purpose_key": p.ucm_purpose_key,
            "live_notices": [
                {"language": n.language, "version": n.version, "id": n.id} for n in notices
            ],
        })
    return out


health = APIRouter(tags=["ops"])


@health.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@health.get("/readyz")
def readyz(db: Session = Depends(get_session)) -> dict:
    db.execute(select(1))
    lag = lag_seconds(db)
    return {"status": "ok" if lag < 3600 else "degraded", "outbox_lag_seconds": lag}
