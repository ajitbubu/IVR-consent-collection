"""Sprinklr-facing endpoints, called from HTTP nodes in a Sprinklr IVR flow.

The contract is JSON in, JSON out, and the flow branches on fields rather
than status codes:

  POST /sprinklr/start     -> {proceed, session_id, say, audio_url}
      play `audio_url` if present, else speak `say`; stop if not `proceed`
  POST /sprinklr/decision  -> {committed, outcome, say}
      speak `say`; `committed` is true only once the consent row is durable
  POST /sprinklr/status    -> 204

Every request must carry "Authorization: Bearer <SPRINKLR_WEBHOOK_TOKEN>";
anything else is a 401 with a receipt kept.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.consent_service import (
    UnknownDigit,
    VerificationRequired,
    attach_call,
    create_session,
    record_decision,
    record_no_decision,
    record_notice_served,
)
from app.db import get_session
from app.identity import NullCrm, PhoneNormalisationError, enrich_from_crm
from app.models import Consent, DataPrincipal, NoticeVersion, Purpose
from app.webhook_common import ingest

log = logging.getLogger("sprinklr")
router = APIRouter(prefix="/sprinklr", tags=["sprinklr"])

CALL_BACK = "We could not record your response. Someone will call you back."


def _rejected() -> Response:
    return Response(status_code=401)


def _stop(say: str) -> JSONResponse:
    return JSONResponse({"proceed": False, "session_id": None, "say": say, "audio_url": None})


def _no_decision(outcome: str, say: str) -> JSONResponse:
    return JSONResponse({"committed": False, "outcome": outcome, "say": say})


@router.post("/start")
async def start(request: Request, db: Session = Depends(get_session)) -> Response:
    """Bind the call to its session (outbound: the flow passes session_id;
    inbound: created here from `purpose`) and hand back the pinned notice."""
    event, verification, sess = await ingest(db, request, "sprinklr", "start")
    if not verification.ok:
        log.warning("sprinklr /start rejected: %s", verification.reason)
        return _rejected()

    if sess is None:
        purpose_code = event.raw.get("purpose")
        if not purpose_code:
            log.warning("sprinklr /start: no session and no purpose, call_id=%s", event.call_ref)
            return _stop("We are unable to continue this call right now.")
        try:
            sess = create_session(
                db,
                direction=event.direction or "ivr_inbound",
                phone_raw=event.from_number or "",
                purpose_code=purpose_code,
                language=event.raw.get("language") or "eng",
                provider="sprinklr",
            )
        except PhoneNormalisationError:
            return _stop("We need your caller ID to record your choice. "
                         "Please call again without hiding your number. Goodbye.")

    if event.call_ref:
        attach_call(db, sess, call_sid=event.call_ref, call_to=event.to_number)

    receipt = request.state.webhook_receipt
    if receipt.ivr_session_id is None:
        receipt.ivr_session_id = sess.id
        db.flush()

    principal = db.get(DataPrincipal, sess.data_principal_id)
    try:
        enrich_from_crm(db, principal, NullCrm())
    except Exception as exc:  # never let enrichment break a live call
        log.warning("crm lookup failed for session=%s: %s", sess.id, exc)

    notice = db.get(NoticeVersion, sess.notice_version_id)
    record_notice_served(db, sess)
    return JSONResponse({
        "proceed": True,
        "session_id": sess.id,
        "say": notice.body_text,
        "audio_url": notice.audio_url,
    })


@router.post("/decision")
async def decision(request: Request, db: Session = Depends(get_session)) -> Response:
    event, verification, sess = await ingest(db, request, "sprinklr", "decision")
    if not verification.ok:
        log.warning("sprinklr /decision rejected: %s", verification.reason)
        return _rejected()

    if sess is None:
        log.error("sprinklr /decision: no session for call_id=%s", event.call_ref)
        return _no_decision("no_session", CALL_BACK)

    if not event.digits:
        record_no_decision(db, sess, "no_input")
        return _no_decision("no_input", "We did not receive a response. Nothing has changed. Goodbye.")

    try:
        result = record_decision(
            db, sess, digit=event.digits,
            decided_at=event.occurred_at or dt.datetime.now(dt.timezone.utc),
        )
        # The request's own commit runs after the response is sent -- too late
        # to take back `committed: true`. Commit first.
        db.commit()
    except UnknownDigit:
        record_no_decision(db, sess, "invalid_key")
        return _no_decision("invalid_key",
                            "That was not one of the options. Nothing has changed. Goodbye.")
    except VerificationRequired:
        record_no_decision(db, sess, "verification_required")
        return _no_decision("verification_required",
                            "We need to verify your identity first. Someone will call you back.")
    except Exception as exc:
        db.rollback()
        log.exception("sprinklr decision write failed for session=%s: %s", sess.id, exc)
        return _no_decision("error", CALL_BACK)

    log.info("consent %s recorded session=%s replayed=%s",
             result.consent.decision, sess.id, result.replayed)
    return JSONResponse({
        "committed": True,
        "outcome": result.consent.decision,
        "say": _confirmation(db, result.consent),
    })


def _confirmation(db: Session, consent: Consent) -> str:
    purpose = db.get(Purpose, consent.purpose_id)
    phrase = {
        "granted": f"You have agreed to {purpose.name}.",
        "declined": f"You have declined {purpose.name}.",
        "withdrawn": f"You have withdrawn your consent for {purpose.name}.",
    }[consent.decision]
    return f"{phrase} Your reference is {consent.id[-6:]}. Thank you."


@router.post("/status")
async def status_callback(request: Request, db: Session = Depends(get_session)) -> Response:
    """End of call. Nothing a consent depends on waits on this."""
    event, verification, sess = await ingest(db, request, "sprinklr", "status")
    if not verification.ok:
        return _rejected()
    if sess is None:
        return Response(status_code=204)

    if event.call_status:
        sess.call_status = event.call_status
    if event.ended_at:
        sess.ended_at = event.ended_at
    if event.recording_url:
        from app.evidence import queue_recording_fetch

        queue_recording_fetch(db, sess, event.recording_url)
    db.flush()
    return Response(status_code=204)
