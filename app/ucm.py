"""UCM client and payload construction."""
from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import decrypt_attribute
from app.models import (
    CallArtifact,
    Consent,
    DataPrincipal,
    IdentityAttribute,
    IvrSession,
    NoticeVersion,
    Purpose,
)


class UcmRejected(Exception):
    """4xx other than 429: the payload itself is wrong. Pause this key."""


class UcmUnavailable(Exception):
    """5xx, timeout, 429: retry with backoff."""


def _live_attrs(db: Session, principal_id: str) -> dict[str, str]:
    rows = db.execute(
        select(IdentityAttribute).where(
            IdentityAttribute.data_principal_id == principal_id,
            IdentityAttribute.superseded_at.is_(None),
        )
    ).scalars()
    return {r.attr: decrypt_attribute(r.value_enc) for r in rows}


def build_payload(
    db: Session, consent: Consent, principal: DataPrincipal, purpose: Purpose
) -> dict:
    notice = db.get(NoticeVersion, consent.notice_version_id)
    attrs = _live_attrs(db, principal.id)

    evidence: dict = {}
    if consent.ivr_session_id:
        sess = db.get(IvrSession, consent.ivr_session_id)
        if sess and sess.call_sid:
            evidence["call_sid"] = sess.call_sid
        rec = db.execute(
            select(CallArtifact).where(
                CallArtifact.ivr_session_id == consent.ivr_session_id,
                CallArtifact.kind == "recording",
            )
        ).scalar_one_or_none()
        if rec is not None and rec.storage_uri:
            evidence["recording_uri"] = rec.storage_uri
            if rec.sha256:
                evidence["recording_sha256"] = rec.sha256.hex()

    principal_block: dict = {"phone_e164": principal.phone_e164}
    if principal.external_ref:
        principal_block["external_ref"] = principal.external_ref
    if "name" in attrs:
        principal_block["name"] = attrs["name"]
    if "email" in attrs:
        principal_block["email"] = attrs["email"]

    return {
        "source": "ivr",
        "external_consent_id": consent.id,
        "data_principal": principal_block,
        "purpose_key": purpose.ucm_purpose_key,
        "decision": consent.decision,
        "decided_at": consent.decided_at.isoformat(),
        "channel": consent.channel,
        "provider": consent.provider,
        "language": consent.language,
        "notice": {
            "version_id": notice.id,
            "sha256": notice.body_sha256.hex(),
        },
        "verification_level": consent.verification_level,
        "evidence": evidence,
    }


class UcmClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        s = settings()
        self.base_url = (base_url or s.ucm_base_url).rstrip("/")
        self.timeout = timeout or s.ucm_timeout_s

    def push(self, payload: dict, idempotency_key: str) -> str | None:
        """Returns UCM's own consent reference. Raises on failure."""
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/consents",
                json=payload,
                headers={"Idempotency-Key": idempotency_key},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise UcmUnavailable(str(exc)) from exc

        if resp.status_code in (200, 201, 409):
            # 409 means UCM already has this key: treat as delivered.
            try:
                return resp.json().get("consent_ref")
            except Exception:
                return None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise UcmUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        raise UcmRejected(f"HTTP {resp.status_code}: {resp.text[:200]}")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
