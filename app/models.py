"""ORM mapping over migrations/001_init.sql. The SQL file is the source of truth."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


ConsentDecision = ENUM(
    "granted", "declined", "withdrawn", name="consent_decision", create_type=False
)
ConsentStatus = ENUM(
    "active", "declined", "withdrawn", "expired", "superseded",
    name="consent_status", create_type=False,
)
SyncState = ENUM("pending", "synced", "failed", "skipped", name="sync_state", create_type=False)
ConsentChannel = ENUM(
    "ivr_inbound", "ivr_outbound", "web", "app", "agent",
    name="consent_channel", create_type=False,
)
VerificationLevel = ENUM(
    "ani_only", "verified", name="verification_level", create_type=False
)

_now = lambda: dt.datetime.now(dt.timezone.utc)  # noqa: E731


class DataPrincipal(Base):
    __tablename__ = "data_principal"
    id = Column(CHAR(26), primary_key=True)
    phone_e164 = Column(Text, nullable=False, unique=True)
    phone_hash = Column(LargeBinary, nullable=False)
    external_ref = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class IdentityAttribute(Base):
    __tablename__ = "identity_attribute"
    id = Column(CHAR(26), primary_key=True)
    data_principal_id = Column(CHAR(26), ForeignKey("data_principal.id"), nullable=False)
    attr = Column(Text, nullable=False)
    value_enc = Column(LargeBinary, nullable=False)
    source = Column(Text, nullable=False)
    confidence = Column(Numeric(3, 2))
    captured_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    superseded_at = Column(DateTime(timezone=True))


class Purpose(Base):
    __tablename__ = "purpose"
    id = Column(CHAR(26), primary_key=True)
    code = Column(Text, nullable=False, unique=True)
    name = Column(Text, nullable=False)
    retention_days = Column(Integer, nullable=False)
    requires_verification = Column(Boolean, nullable=False, default=False)
    ucm_purpose_key = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class NoticeVersion(Base):
    __tablename__ = "notice_version"
    id = Column(CHAR(26), primary_key=True)
    purpose_id = Column(CHAR(26), ForeignKey("purpose.id"), nullable=False)
    version = Column(Integer, nullable=False)
    language = Column(CHAR(3), nullable=False)
    body_text = Column(Text, nullable=False)
    body_sha256 = Column(LargeBinary, nullable=False)
    audio_url = Column(Text)
    published_at = Column(DateTime(timezone=True))
    retired_at = Column(DateTime(timezone=True))
    purpose = relationship("Purpose")


class IvrSession(Base):
    __tablename__ = "ivr_session"
    id = Column(CHAR(26), primary_key=True)
    call_sid = Column(Text, unique=True)
    direction = Column(ConsentChannel, nullable=False)
    data_principal_id = Column(CHAR(26), ForeignKey("data_principal.id"))
    purpose_id = Column(CHAR(26), ForeignKey("purpose.id"))
    notice_version_id = Column(CHAR(26), ForeignKey("notice_version.id"))
    language = Column(CHAR(3), nullable=False, default="eng")
    from_number = Column(Text)
    to_number = Column(Text)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    ended_at = Column(DateTime(timezone=True))
    call_status = Column(Text)
    answered_by = Column(Text)
    outcome = Column(Text, nullable=False, default="in_progress")
    reconciled_at = Column(DateTime(timezone=True))
    provider = Column(Text, nullable=False, default="exotel")


class CallArtifact(Base):
    __tablename__ = "call_artifact"
    id = Column(CHAR(26), primary_key=True)
    ivr_session_id = Column(CHAR(26), ForeignKey("ivr_session.id"), nullable=False)
    kind = Column(Text, nullable=False)
    storage_uri = Column(Text)
    sha256 = Column(LargeBinary)
    bytes = Column(BigInteger)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    purge_after = Column(DateTime(timezone=True), nullable=False)
    purged_at = Column(DateTime(timezone=True))


class WebhookReceipt(Base):
    """What arrived, from where, and whether it was cryptographically
    verifiable. Twilio's signature is kept so each consent stays
    independently re-verifiable; for Exotel these stay null."""

    __tablename__ = "webhook_receipt"
    id = Column(CHAR(26), primary_key=True)
    ivr_session_id = Column(CHAR(26), ForeignKey("ivr_session.id"))
    provider = Column(Text, nullable=False)
    route = Column(Text, nullable=False)
    request_url = Column(Text, nullable=False)
    signature = Column(Text)
    signature_ok = Column(Boolean)
    body_sha256 = Column(LargeBinary)
    params = Column(JSONB, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Consent(Base):
    __tablename__ = "consent"
    id = Column(CHAR(26), primary_key=True)
    data_principal_id = Column(CHAR(26), ForeignKey("data_principal.id"), nullable=False)
    purpose_id = Column(CHAR(26), ForeignKey("purpose.id"), nullable=False)
    notice_version_id = Column(CHAR(26), ForeignKey("notice_version.id"), nullable=False)
    ivr_session_id = Column(CHAR(26), ForeignKey("ivr_session.id"))
    decision = Column(ConsentDecision, nullable=False)
    status = Column(ConsentStatus, nullable=False)
    channel = Column(ConsentChannel, nullable=False)
    language = Column(CHAR(3), nullable=False)
    verification_level = Column(VerificationLevel, nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True))
    is_current = Column(Boolean, nullable=False, default=True)
    superseded_by = Column(CHAR(26), ForeignKey("consent.id"))
    ucm_sync_state = Column(SyncState, nullable=False, default="pending")
    ucm_consent_ref = Column(Text)
    provider = Column(Text, nullable=False, default="exotel")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    purpose = relationship("Purpose")
    notice_version = relationship("NoticeVersion")

    @property
    def permits_processing(self) -> bool:
        if not self.is_current or self.status != "active":
            return False
        if self.expires_at is not None and self.expires_at <= _now():
            return False
        return True


class ConsentEvent(Base):
    __tablename__ = "consent_event"
    seq = Column(BigInteger, primary_key=True, autoincrement=True)
    chain_key = Column(CHAR(26), nullable=False)
    consent_id = Column(CHAR(26), ForeignKey("consent.id"))
    ivr_session_id = Column(CHAR(26), ForeignKey("ivr_session.id"))
    event_type = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    prev_hash = Column(LargeBinary)
    entry_hash = Column(LargeBinary, nullable=False)


class UcmOutbox(Base):
    __tablename__ = "ucm_outbox"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    consent_id = Column(CHAR(26), ForeignKey("consent.id"), nullable=False)
    data_principal_id = Column(CHAR(26), ForeignKey("data_principal.id"), nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=False)
    idempotency_key = Column(String, nullable=False, unique=True)
    payload = Column(JSONB, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_error = Column(Text)
    paused = Column(Boolean, nullable=False, default=False)
    delivered_at = Column(DateTime(timezone=True))
