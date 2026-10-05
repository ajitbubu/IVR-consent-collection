"""Call reconciliation: corroborate every finished call against the
provider's authenticated call-details API.

Exotel's webhooks are unsigned, and Twilio does not retry a lost keypress,
so a webhook alone is not proof a call happened as described. Once a call is
over, this asks the provider directly. A call that does not corroborate --
unknown to the provider, a different number, never connected -- is written
into the audit chain, and any consent it produced is held back from UCM for a
person to look at.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.hashchain import append_event
from app.identity import PhoneNormalisationError, normalise_e164
from app.models import Consent, DataPrincipal, IvrSession, UcmOutbox
from app import sprinklr_api
from app.telephony import exotel, twilio
from app.telephony.base import CallDetails, CallLookupUnavailable

log = logging.getLogger("reconcile")

Lookup = Callable[[str], CallDetails | None]

LOOKUPS: dict[str, Lookup] = {
    "exotel": exotel.fetch_call_details,
    "twilio": twilio.fetch_call_details,
}

# Not finished yet at the provider: look again on a later pass.
_IN_PROGRESS = {"queued", "initiated", "ringing", "in-progress"}


def _lookups() -> dict[str, Lookup]:
    if settings().sprinklr_api_enabled:
        return {**LOOKUPS, "sprinklr": sprinklr_api.fetch_call_details}
    return LOOKUPS


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _numbers(details: CallDetails) -> set[str]:
    out = set()
    for raw in (details.from_number, details.to_number):
        try:
            out.add(normalise_e164(raw or ""))
        except PhoneNormalisationError:
            pass
    return out


def _judge(details: CallDetails | None, phone: str, consent: Consent | None) -> str:
    if details is None:
        return "not_found"
    if phone not in _numbers(details):
        return "number_mismatch"
    if consent is not None and details.status != "completed":
        return "not_connected"
    return "ok"


def _hold(db: Session, consent: Consent, result: str) -> None:
    """Keep an uncorroborated consent out of UCM. One already delivered
    cannot be recalled; the audit event and the log are what remain."""
    row = db.execute(
        select(UcmOutbox).where(UcmOutbox.consent_id == consent.id)
    ).scalar_one_or_none()
    if row is not None and row.delivered_at is None:
        row.paused = True
        row.last_error = f"reconciliation: {result}"
        consent.ucm_sync_state = "failed"
        log.error("consent held from UCM consent_id=%s result=%s", consent.id, result)
    else:
        log.error("consent already in UCM did not reconcile consent_id=%s result=%s",
                  consent.id, result)


def reconcile_due(
    db: Session, lookups: dict[str, Lookup] | None = None, limit: int = 50
) -> dict[str, int]:
    lookups = lookups if lookups is not None else _lookups()
    cutoff = _now() - dt.timedelta(seconds=settings().reconcile_after_s)
    sessions = list(
        db.execute(
            select(IvrSession)
            .where(
                IvrSession.reconciled_at.is_(None),
                IvrSession.call_sid.isnot(None),
                IvrSession.started_at < cutoff,
                IvrSession.provider.in_(list(lookups)),
            )
            .order_by(IvrSession.started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )

    counts = {"ok": 0, "mismatch": 0, "deferred": 0}
    for sess in sessions:
        try:
            details = lookups[sess.provider](sess.call_sid)
        except CallLookupUnavailable as exc:
            log.warning("reconcile deferred session=%s: %s", sess.id, exc)
            counts["deferred"] += 1
            continue
        if details is not None and details.status in _IN_PROGRESS:
            counts["deferred"] += 1
            continue

        # Lock the principal before touching their chain.
        principal = db.execute(
            select(DataPrincipal)
            .where(DataPrincipal.id == sess.data_principal_id)
            .with_for_update()
        ).scalar_one()
        consent = db.execute(
            select(Consent).where(Consent.ivr_session_id == sess.id)
        ).scalar_one_or_none()
        result = _judge(details, principal.phone_e164, consent)

        if details is not None:
            sess.call_status = sess.call_status or details.status
            sess.answered_by = sess.answered_by or details.answered_by
            sess.ended_at = sess.ended_at or details.ended_at
        sess.reconciled_at = _now()
        sess.reconcile_result = result

        append_event(
            db,
            chain_key=principal.id,
            event_type="call.reconciled",
            payload={
                "session_id": sess.id,
                "call_sid": sess.call_sid,
                "provider": sess.provider,
                "result": result,
                "provider_status": details.status if details else None,
                "consent_id": consent.id if consent else None,
            },
            ivr_session_id=sess.id,
        )
        if result == "ok":
            counts["ok"] += 1
        else:
            counts["mismatch"] += 1
            if consent is not None:
                _hold(db, consent, result)
        db.flush()
    return counts
