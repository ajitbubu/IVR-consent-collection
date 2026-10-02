"""Exotel-facing webhook handlers.

These endpoints are unauthenticated by necessity: Exotel does not sign its
webhooks -- there is no HMAC header, no shared secret, no verification
mechanism documented. They therefore live on their own hostname behind an IP
allowlist, they never return data, and nothing they assert is trusted until
it corroborates against the authenticated Call Details API.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
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
from app.identity import NullCrm, enrich_from_crm, normalise_e164
from app.models import Consent, DataPrincipal, IvrSession, NoticeVersion, Purpose
from app.telephony.exotel import FROZEN_END_TIME, parse_exotel_time, trim_digits  # noqa: F401
from app.webhook_common import ingest_sync

log = logging.getLogger("exotel")
router = APIRouter(prefix="/exotel", tags=["exotel"])

def _find_session(db: Session, custom_field: str | None, call_sid: str | None) -> IvrSession | None:
    if custom_field:
        sess = db.get(IvrSession, custom_field.strip())
        if sess:
            return sess
    if call_sid:
        return db.execute(
            select(IvrSession).where(IvrSession.call_sid == call_sid)
        ).scalar_one_or_none()
    return None


@router.get("/identify")
def identify(
    request: Request,
    db: Session = Depends(get_session),
) -> Response:
    """Passthru, async mode. Exotel does not wait, so a slow CRM never stalls
    the call. Creates the session for inbound calls, which have no CustomField."""
    q = request.query_params
    call_sid = q.get("CallSid")
    sess = _find_session(db, q.get("CustomField"), call_sid)

    if sess is None:
        purpose_code = q.get("purpose")
        if not purpose_code:
            log.warning("identify: no session and no purpose, CallSid=%s", call_sid)
            return Response(status_code=200)
        direction = (
            "ivr_inbound" if (q.get("Direction") or "").startswith("incoming")
            else "ivr_outbound"
        )
        sess = create_session(
            db,
            direction=direction,
            phone_raw=q.get("CallFrom") or "",
            purpose_code=purpose_code,
            language=q.get("lang", "eng"),
            provider="exotel",
        )

    if call_sid:
        attach_call(db, sess, call_sid=call_sid, call_to=q.get("CallTo"))

    principal = db.get(DataPrincipal, sess.data_principal_id)
    try:
        enrich_from_crm(db, principal, NullCrm())
    except Exception as exc:  # never let enrichment break a live call
        log.warning("crm lookup failed for session=%s: %s", sess.id, exc)

    return Response(status_code=200)


@router.api_route("/notice", methods=["GET", "HEAD"])
def notice(request: Request, db: Session = Depends(get_session)) -> Response:
    """Dynamic Greeting. Exotel requires text/plain, and requires the server
    to answer HEAD with the same headers as GET -- it fails silently if you
    only implement GET."""
    q = request.query_params
    sess = _find_session(db, q.get("CustomField"), q.get("CallSid"))
    if sess is None:
        return Response("We are unable to continue this call right now.",
                        media_type="text/plain", status_code=200)

    nv = db.get(NoticeVersion, sess.notice_version_id)
    if request.method == "GET":
        record_notice_served(db, sess)
    return Response(nv.body_text, media_type="text/plain", status_code=200)


@router.get("/decision")
def decision(request: Request, db: Session = Depends(get_session)) -> Response:
    """Passthru, sync mode. The caller is holding, so this must answer fast.

    200 means the consent row is committed, play the confirmation.
    302 means the write failed, apologise and promise a callback.
    A caller is never told their consent was recorded when it was not.
    """
    q = request.query_params
    event, _verification, sess = ingest_sync(db, request, "exotel", "decision")
    if sess is None:
        log.error("decision: no session for CallSid=%s", q.get("CallSid"))
        return Response(status_code=302)

    digit = event.digits
    decided_at = event.occurred_at or dt.datetime.now(dt.timezone.utc)

    if digit is None:
        record_no_decision(db, sess, "no_input")
        return Response(status_code=302)

    try:
        result = record_decision(db, sess, digit=digit, decided_at=decided_at)
    except UnknownDigit:
        record_no_decision(db, sess, "invalid_key")
        return Response(status_code=302)
    except VerificationRequired:
        record_no_decision(db, sess, "verification_required")
        return Response(status_code=302)
    except Exception as exc:
        log.exception("decision write failed for session=%s: %s", sess.id, exc)
        return Response(status_code=302)

    log.info(
        "consent %s recorded session=%s replayed=%s",
        result.consent.decision, sess.id, result.replayed,
    )
    return Response(status_code=200)


@router.api_route("/readback", methods=["GET", "HEAD"])
def readback(request: Request, db: Session = Depends(get_session)) -> Response:
    q = request.query_params
    sess = _find_session(db, q.get("CustomField"), q.get("CallSid"))
    if sess is None:
        return Response("Thank you.", media_type="text/plain")

    consent = db.execute(
        select(Consent)
        .where(Consent.ivr_session_id == sess.id)
        .order_by(Consent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if consent is None:
        # Silence or an unoffered key is a recorded non-decision, not a failure:
        # say so, and promise a callback only when one is actually owed.
        if sess.outcome in ("no_input", "invalid_key"):
            text = "We did not receive a valid response. Nothing has changed. Thank you."
        elif sess.outcome == "verification_required":
            text = "We need to verify your identity first. Someone will call you back."
        else:
            text = "We could not record your response. Someone will call you back."
        return Response(text, media_type="text/plain")

    purpose = db.get(Purpose, consent.purpose_id)
    phrase = {
        "granted": f"You have agreed to {purpose.name}.",
        "declined": f"You have declined {purpose.name}.",
        "withdrawn": f"You have withdrawn your consent for {purpose.name}.",
    }[consent.decision]
    return Response(
        f"{phrase} Your reference is {consent.id[-6:]}. Thank you.",
        media_type="text/plain",
    )


@router.post("/status")
async def status_callback(request: Request, db: Session = Depends(get_session)) -> Response:
    """StatusCallback. Delivery is explicitly not guaranteed and the retry
    policy is undocumented, so nothing a consent depends on waits on this.
    It supplies RecordingUrl, AnsweredBy and real durations."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    call_sid = body.get("CallSid")
    sess = _find_session(db, body.get("CustomField"), call_sid)
    if sess is None:
        log.warning("status: unknown CallSid=%s", call_sid)
        return Response(status_code=200)  # always 200 to Exotel

    sess.call_status = body.get("Status")
    sess.ended_at = parse_exotel_time(body.get("EndTime"))
    legs = body.get("Legs") or []
    if isinstance(legs, list) and len(legs) > 1 and isinstance(legs[1], dict):
        sess.answered_by = legs[1].get("AnsweredBy")

    recording_url = body.get("RecordingUrl")
    if recording_url:
        # Queued, not fetched inline: Exotel's copy is transient and its
        # region and access model are not documented. The fetcher copies it
        # into our own ap-south-1 bucket and hashes it there.
        from app.evidence import queue_recording_fetch

        queue_recording_fetch(db, sess, recording_url)

    db.flush()
    return Response(status_code=200)
