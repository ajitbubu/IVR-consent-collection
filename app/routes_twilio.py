"""Twilio-facing webhook handlers.

The structural difference from the Exotel routes: Twilio signs every request,
so these endpoints authenticate the caller cryptographically rather than
relying on an IP allowlist. The signature is stored with the receipt, which
makes each consent independently re-verifiable afterwards.

Two Twilio behaviours shape the code:

  * Twilio imposes a hard 15-second ceiling on call-related HTTP requests,
    so the handler commits and returns -- the UCM push is already async.
  * By default Twilio retries once, and only on a TCP or TLS failure, not on
    a 5xx or a read timeout. Delivery of a consent keypress is therefore not
    guaranteed, and reconciliation against the REST API is not optional.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
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
from app.identity import NullCrm, enrich_from_crm
from app.models import Consent, DataPrincipal, NoticeVersion, Purpose
from app.telephony.twilio import TwilioProvider, gather_twiml
from app.webhook_common import ingest

log = logging.getLogger("twilio")
router = APIRouter(prefix="/twilio", tags=["twilio"])

_provider = TwilioProvider()

XML = "application/xml"


def _hangup(message: str) -> Response:
    from xml.sax.saxutils import escape

    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Response><Say>{escape(message)}</Say><Hangup/></Response>",
        media_type=XML,
    )


def _rejected() -> Response:
    """An unverified webhook is a forgery. 403 and nothing else."""
    return Response(status_code=403)


def _action_url(path: str, session_id: str) -> str:
    base = settings().public_base_url.rstrip("/")
    return f"{base}/twilio/{path}?session={session_id}"


@router.post("/voice")
async def voice(request: Request, db: Session = Depends(get_session)) -> Response:
    """Entry point. Twilio POSTs here when the call connects; we answer with
    the TwiML that reads the notice and gathers one key."""
    event, verification, sess = await ingest(db, request, "twilio", "voice")
    if settings().twilio_enforce_signature and not verification.ok:
        log.warning("twilio /voice rejected: %s", verification.reason)
        return _rejected()

    if sess is None:
        purpose_code = request.query_params.get("purpose")
        if not purpose_code:
            log.warning("twilio /voice: no session and no purpose")
            return _hangup("We are unable to continue this call right now.")
        sess = create_session(
            db,
            direction=event.direction or "ivr_inbound",
            phone_raw=event.from_number or "",
            purpose_code=purpose_code,
            language=request.query_params.get("lang", "eng"),
            provider="twilio",
        )

    if sess.call_sid is None and sess.provider != "twilio":
        # The session was created before the provider was settled; the call
        # that actually arrived decides. A provider change after the session
        # is bound to a call would be a misconfiguration, so it is not allowed.
        sess.provider = "twilio"
        db.flush()

    if event.call_ref:
        attach_call(db, sess, call_sid=event.call_ref, call_to=event.to_number)

    principal = db.get(DataPrincipal, sess.data_principal_id)
    try:
        enrich_from_crm(db, principal, NullCrm())
    except Exception as exc:
        log.warning("crm lookup failed for session=%s: %s", sess.id, exc)

    notice = db.get(NoticeVersion, sess.notice_version_id)
    record_notice_served(db, sess)

    s = settings()
    return Response(
        gather_twiml(
            notice_text=notice.body_text,
            action_url=_action_url("decision", sess.id),
            language=s.twilio_language,
            voice=s.twilio_voice or None,
            # A pre-recorded file gives a byte-identical, reproducible
            # disclosure; a TTS render can change between provider versions.
            audio_url=notice.audio_url,
        ),
        media_type=XML,
    )


@router.post("/decision")
async def decision(request: Request, db: Session = Depends(get_session)) -> Response:
    """The <Gather> action URL.

    actionOnEmptyResult is set on the Gather, so this fires on silence too --
    a non-response becomes an affirmatively recorded outcome rather than a
    silent fall-through.
    """
    event, verification, sess = await ingest(db, request, "twilio", "decision")
    if settings().twilio_enforce_signature and not verification.ok:
        log.warning("twilio /decision rejected: %s", verification.reason)
        return _rejected()

    if sess is None:
        log.error("twilio /decision: no session for CallSid=%s", event.call_ref)
        return _hangup("We could not record your response. Someone will call you back.")

    if sess.call_sid is None and sess.provider != "twilio":
        sess.provider = "twilio"
        db.flush()

    # Consent taken from a voicemail system is not consent.
    if (event.answered_by or "").startswith("machine"):
        record_no_decision(db, sess, "answering_machine")
        return _hangup("Goodbye.")

    if not event.digits:
        record_no_decision(db, sess, "no_input")
        return _hangup("We did not receive a response. Nothing has changed. Goodbye.")

    try:
        result = record_decision(
            db, sess, digit=event.digits,
            decided_at=event.occurred_at or dt.datetime.now(dt.timezone.utc),
        )
    except UnknownDigit:
        record_no_decision(db, sess, "invalid_key")
        return _hangup("That was not one of the options. Nothing has changed. Goodbye.")
    except VerificationRequired:
        record_no_decision(db, sess, "verification_required")
        return _hangup("We need to verify your identity first. Someone will call you back.")
    except Exception as exc:
        log.exception("twilio decision write failed for session=%s: %s", sess.id, exc)
        return _hangup("We could not record your response. Someone will call you back.")

    log.info("consent %s recorded session=%s replayed=%s",
             result.consent.decision, sess.id, result.replayed)
    return Response(
        _provider.decision_response(
            committed=True, next_url=_action_url("readback", sess.id)
        )[0],
        media_type=XML,
    )


@router.post("/readback")
async def readback(request: Request, db: Session = Depends(get_session)) -> Response:
    event, verification, sess = await ingest(db, request, "twilio", "readback")
    if settings().twilio_enforce_signature and not verification.ok:
        return _rejected()
    if sess is None:
        return _hangup("Thank you.")

    consent = db.execute(
        select(Consent).where(Consent.ivr_session_id == sess.id)
        .order_by(Consent.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if consent is None:
        return _hangup("We could not record your response. Someone will call you back.")

    purpose = db.get(Purpose, consent.purpose_id)
    phrase = {
        "granted": f"You have agreed to {purpose.name}.",
        "declined": f"You have declined {purpose.name}.",
        "withdrawn": f"You have withdrawn your consent for {purpose.name}.",
    }[consent.decision]
    return _hangup(f"{phrase} Your reference is {consent.id[-6:]}. Thank you.")


@router.post("/status")
async def status_callback(request: Request, db: Session = Depends(get_session)) -> Response:
    """StatusCallback. Twilio does not guarantee ordering, so SequenceNumber
    decides which update wins."""
    event, verification, sess = await ingest(db, request, "twilio", "status")
    if settings().twilio_enforce_signature and not verification.ok:
        return _rejected()
    if sess is None:
        return Response(status_code=204)

    sess.call_status = event.call_status
    if event.answered_by:
        sess.answered_by = event.answered_by
    if event.ended_at:
        sess.ended_at = event.ended_at
    db.flush()
    return Response(status_code=204)


@router.post("/recording")
async def recording_callback(request: Request, db: Session = Depends(get_session)) -> Response:
    """RecordingStatusCallback -- the reliable path for the audio. Twilio
    enforces HTTP Basic auth on all media URLs, so unlike Exotel the fetcher
    needs an API key to retrieve it."""
    event, verification, sess = await ingest(db, request, "twilio", "recording")
    if settings().twilio_enforce_signature and not verification.ok:
        return _rejected()
    if sess is None or not event.recording_url:
        return Response(status_code=204)

    from app.evidence import queue_recording_fetch

    queue_recording_fetch(db, sess, event.recording_url)
    return Response(status_code=204)
