"""The consent state machine.

One rule governs everything here: a consent is a fact that happened at a
moment, not a flag that gets edited. Rows are superseded, never updated in
place, and the consent, its audit event and its outbox row are committed in
a single transaction or not at all.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from app.crypto import sha256
from app.hashchain import append_event
from app.identity import get_or_create_principal, normalise_e164
from app.models import (
    Consent,
    DataPrincipal,
    IvrSession,
    NoticeVersion,
    Purpose,
    UcmOutbox,
)

# Keypad mapping. Anything not in here is not a decision.
DIGIT_TO_DECISION = {"1": "granted", "2": "declined", "9": "withdrawn"}

DECISION_TO_STATUS = {
    "granted": "active",
    "declined": "declined",
    "withdrawn": "withdrawn",
}


class ConsentError(Exception):
    pass


class UnknownDigit(ConsentError):
    pass


class VerificationRequired(ConsentError):
    """The purpose demands a verified identity and the call only has ANI."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------------ sessions


def live_notice(db: Session, purpose: Purpose, language: str) -> NoticeVersion:
    nv = db.execute(
        select(NoticeVersion)
        .where(
            NoticeVersion.purpose_id == purpose.id,
            NoticeVersion.language == language,
            NoticeVersion.published_at.isnot(None),
            NoticeVersion.retired_at.is_(None),
        )
        .order_by(NoticeVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if nv is None:
        raise ConsentError(
            f"no published notice for purpose {purpose.code!r} in language {language!r}"
        )
    return nv


def create_session(
    db: Session,
    *,
    direction: str,
    phone_raw: str,
    purpose_code: str,
    language: str = "eng",
    provider: str = "exotel",
) -> IvrSession:
    """Pins the notice version now, not at decision time. A notice
    republished mid-call would otherwise leave you unable to say which
    wording the caller actually heard."""
    phone = normalise_e164(phone_raw)
    purpose = db.execute(
        select(Purpose).where(Purpose.code == purpose_code)
    ).scalar_one_or_none()
    if purpose is None:
        raise ConsentError(f"unknown purpose {purpose_code!r}")

    principal = get_or_create_principal(db, phone, lock=True)
    notice = live_notice(db, purpose, language)

    sess = IvrSession(
        id=str(ULID()),
        direction=direction,
        data_principal_id=principal.id,
        purpose_id=purpose.id,
        notice_version_id=notice.id,
        language=language,
        from_number=phone,
        provider=provider,
    )
    db.add(sess)
    db.flush()

    append_event(
        db,
        chain_key=principal.id,
        event_type="session.created",
        payload={"session_id": sess.id, "purpose": purpose.code, "language": language,
                 "direction": direction, "provider": provider},
        ivr_session_id=sess.id,
    )
    return sess


def attach_call(
    db: Session, sess: IvrSession, *, call_sid: str, call_to: str | None = None
) -> IvrSession:
    if sess.call_sid and sess.call_sid != call_sid:
        raise ConsentError("session is already bound to a different CallSid")
    sess.call_sid = call_sid
    if call_to:
        sess.to_number = call_to
    db.flush()
    return sess


def record_notice_served(db: Session, sess: IvrSession) -> None:
    notice = db.get(NoticeVersion, sess.notice_version_id)
    append_event(
        db,
        chain_key=sess.data_principal_id,
        event_type="notice.served",
        payload={
            "session_id": sess.id,
            "notice_version_id": notice.id,
            "notice_sha256": notice.body_sha256.hex(),
            "language": notice.language,
        },
        ivr_session_id=sess.id,
    )


# ------------------------------------------------------------------ decisions


@dataclass
class DecisionResult:
    consent: Consent
    replayed: bool


def record_decision(
    db: Session,
    sess: IvrSession,
    *,
    digit: str | None = None,
    decision: str | None = None,
    verification_level: str = "ani_only",
    decided_at: dt.datetime | None = None,
) -> DecisionResult:
    """Commit a decision. Idempotent per (session, purpose): a replayed
    webhook returns the original row rather than creating a second consent."""
    if decision is None:
        if digit is None or digit not in DIGIT_TO_DECISION:
            raise UnknownDigit(f"{digit!r} is not an offered key")
        decision = DIGIT_TO_DECISION[digit]

    decided_at = decided_at or _now()

    # Lock the principal: serialises the chain head read and the current-row swap.
    principal = db.execute(
        select(DataPrincipal)
        .where(DataPrincipal.id == sess.data_principal_id)
        .with_for_update()
    ).scalar_one()

    existing = db.execute(
        select(Consent).where(
            Consent.ivr_session_id == sess.id,
            Consent.purpose_id == sess.purpose_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return DecisionResult(consent=existing, replayed=True)

    purpose = db.get(Purpose, sess.purpose_id)
    if purpose.requires_verification and verification_level != "verified":
        raise VerificationRequired(
            f"purpose {purpose.code!r} requires a verified identity"
        )

    current = db.execute(
        select(Consent)
        .where(
            Consent.data_principal_id == principal.id,
            Consent.purpose_id == purpose.id,
            Consent.is_current.is_(True),
        )
        .with_for_update()
    ).scalar_one_or_none()

    new_id = str(ULID())

    # Clear the flag before the insert -- the partial unique index has to hold
    # at every instant, and the pointer can only be set once the new row exists.
    if current is not None:
        current.is_current = False
        db.flush()

    status = DECISION_TO_STATUS[decision]
    expires_at = (
        decided_at + dt.timedelta(days=purpose.retention_days)
        if decision == "granted"
        else None
    )

    consent = Consent(
        id=new_id,
        data_principal_id=principal.id,
        purpose_id=purpose.id,
        notice_version_id=sess.notice_version_id,
        ivr_session_id=sess.id,
        decision=decision,
        status=status,
        channel=sess.direction,
        language=sess.language,
        verification_level=verification_level,
        decided_at=decided_at,
        expires_at=expires_at,
        is_current=True,
        provider=sess.provider,
        ucm_sync_state="pending",
    )
    db.add(consent)
    db.flush()

    if current is not None:
        current.superseded_by = new_id
        db.flush()

    append_event(
        db,
        chain_key=principal.id,
        event_type=f"consent.{decision}",
        payload={
            "consent_id": new_id,
            "session_id": sess.id,
            "purpose": purpose.code,
            "digit": digit,
            "notice_version_id": sess.notice_version_id,
            "verification_level": verification_level,
            "provider": sess.provider,
            "supersedes": current.id if current else None,
        },
        consent_id=new_id,
        ivr_session_id=sess.id,
        occurred_at=decided_at,
    )

    sess.outcome = f"decision_{decision}"
    db.add(_outbox_row(db, consent, principal, purpose))
    db.flush()
    return DecisionResult(consent=consent, replayed=False)


def record_no_decision(db: Session, sess: IvrSession, outcome: str) -> None:
    """Silence, a timeout, a hangup, an unoffered key. Never a consent."""
    sess.outcome = outcome
    append_event(
        db,
        chain_key=sess.data_principal_id,
        event_type="session.no_decision",
        payload={"session_id": sess.id, "outcome": outcome},
        ivr_session_id=sess.id,
    )
    db.flush()


# ------------------------------------------------------------------ outbox


def _outbox_row(
    db: Session, consent: Consent, principal: DataPrincipal, purpose: Purpose
) -> UcmOutbox:
    from app.identity import IdentityAttribute  # noqa: F401
    from app.ucm import build_payload

    return UcmOutbox(
        consent_id=consent.id,
        data_principal_id=principal.id,
        decided_at=consent.decided_at,
        idempotency_key=consent.id,
        payload=build_payload(db, consent, principal, purpose),
    )


# ------------------------------------------------------------------ reads


def current_consents(db: Session, principal_id: str) -> list[Consent]:
    return list(
        db.execute(
            select(Consent)
            .where(
                Consent.data_principal_id == principal_id,
                Consent.is_current.is_(True),
            )
            .order_by(Consent.decided_at.desc())
        ).scalars()
    )


def notice_digest(body_text: str) -> bytes:
    return sha256(body_text.encode())
