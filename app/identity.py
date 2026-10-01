"""Phone normalisation, CRM lookup, and the Data Principal record.

ANI proves a handset, not a person. Everything here records where a value
came from so that a purpose can later refuse an under-verified consent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from app.crypto import encrypt_attribute, phone_hash
from app.config import settings
from app.models import DataPrincipal, IdentityAttribute

_DIGITS = re.compile(r"\D")


class PhoneNormalisationError(ValueError):
    pass


def normalise_e164(raw: str, default_cc: str | None = None) -> str:
    """Exotel returns numbers in mixed shapes across parameters. Matching
    +919876543210 against 09876543210 silently splits one person into two
    records with contradictory consents, so everything normalises on entry."""
    if raw is None:
        raise PhoneNormalisationError("empty number")
    cc = default_cc or settings().default_country_code
    s = raw.strip()
    had_plus = s.startswith("+")
    digits = _DIGITS.sub("", s)
    if not digits:
        raise PhoneNormalisationError(f"no digits in {raw!r}")

    if had_plus:
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+" + cc + digits.lstrip("0")
    if digits.startswith(cc) and len(digits) > 10:
        return "+" + digits
    return "+" + cc + digits


@dataclass(frozen=True)
class CrmRecord:
    external_ref: str | None = None
    name: str | None = None
    email: str | None = None


class CrmPort(Protocol):
    """Whatever the CRM of record turns out to be sits behind this."""

    def lookup_by_phone(self, phone_e164: str) -> CrmRecord | None: ...


class NullCrm:
    """Default. Consent still works; identity stays phone-only."""

    def lookup_by_phone(self, phone_e164: str) -> CrmRecord | None:
        return None


def get_or_create_principal(
    db: Session, phone_e164: str, *, lock: bool = False
) -> DataPrincipal:
    stmt = select(DataPrincipal).where(DataPrincipal.phone_e164 == phone_e164)
    if lock:
        stmt = stmt.with_for_update()
    dp = db.execute(stmt).scalar_one_or_none()
    if dp is not None:
        return dp

    dp = DataPrincipal(
        id=str(ULID()),
        phone_e164=phone_e164,
        phone_hash=phone_hash(phone_e164),
    )
    db.add(dp)
    db.flush()
    if lock:
        # Re-select with the lock now that the row exists.
        dp = db.execute(
            select(DataPrincipal)
            .where(DataPrincipal.id == dp.id)
            .with_for_update()
        ).scalar_one()
    return dp


def set_attribute(
    db: Session,
    principal: DataPrincipal,
    attr: str,
    value: str,
    source: str,
    confidence: float | None = None,
) -> IdentityAttribute:
    """Supersede rather than overwrite -- the old value stays for the audit
    trail and exactly one row is current."""
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    existing = db.execute(
        select(IdentityAttribute).where(
            IdentityAttribute.data_principal_id == principal.id,
            IdentityAttribute.attr == attr,
            IdentityAttribute.superseded_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.superseded_at = now
        db.flush()

    row = IdentityAttribute(
        id=str(ULID()),
        data_principal_id=principal.id,
        attr=attr,
        value_enc=encrypt_attribute(value),
        source=source,
        confidence=confidence,
        captured_at=now,
    )
    db.add(row)
    db.flush()
    return row


def enrich_from_crm(db: Session, principal: DataPrincipal, crm: CrmPort) -> CrmRecord | None:
    rec = crm.lookup_by_phone(principal.phone_e164)
    if rec is None:
        return None
    if rec.external_ref and not principal.external_ref:
        principal.external_ref = rec.external_ref
    if rec.name:
        set_attribute(db, principal, "name", rec.name, "crm", 0.9)
    if rec.email:
        set_attribute(db, principal, "email", rec.email, "crm", 0.9)
    db.flush()
    return rec
