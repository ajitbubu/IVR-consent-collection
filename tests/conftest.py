from __future__ import annotations

import datetime as dt
import os
import subprocess

import pytest

os.environ.setdefault("PHONE_HMAC_KEY", "test-hmac-key")
os.environ.setdefault("ATTRIBUTE_KEY", "test-attr-key")
os.environ.setdefault("UCM_BASE_URL", "http://ucm.invalid")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("PUBLIC_BASE_URL", "https://consent.test")
os.environ.setdefault("TWILIO_ENFORCE_SIGNATURE", "true")
os.environ.setdefault("SPRINKLR_WEBHOOK_TOKEN", "test-sprinklr-token")

TEST_DB = os.environ.get("TEST_DB", "ivr_consent_test")
PGHOST = os.environ.get("PGHOST", "/tmp")
os.environ["DATABASE_URL"] = f"postgresql+psycopg2://postgres@/{TEST_DB}?host={PGHOST}"


def _psql(sql: str, db: str = "postgres") -> None:
    subprocess.run(
        ["psql", "-h", PGHOST, "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="session", autouse=True)
def database():
    _psql(f"DROP DATABASE IF EXISTS {TEST_DB}")
    _psql(f"CREATE DATABASE {TEST_DB}")
    from app.db import apply_migrations, reset_engine

    reset_engine()
    apply_migrations()
    yield
    reset_engine()


@pytest.fixture()
def db(database):
    from app.db import session_factory
    from sqlalchemy import text

    s = session_factory()()
    # Clean slate per test.
    s.execute(text(
        "TRUNCATE webhook_receipt, ucm_outbox, consent_event, consent, call_artifact, "
        "ivr_session, identity_attribute, notice_version, purpose, data_principal, "
        "sprinklr_oauth_token "
        "RESTART IDENTITY CASCADE"
    ))
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def marketing_purpose(db):
    from ulid import ULID
    from app.consent_service import notice_digest
    from app.models import NoticeVersion, Purpose

    p = Purpose(
        id=str(ULID()),
        code="marketing_outreach",
        name="marketing calls and messages",
        retention_days=365,
        requires_verification=False,
        ucm_purpose_key="marketing_outreach",
    )
    db.add(p)
    db.flush()
    body = (
        "Data Safeguard would like to contact you about our products. "
        "Your phone number and name will be used for this purpose only, "
        "kept for one year, and you may withdraw at any time by calling "
        "this number. To agree press 1. To decline press 2."
    )
    nv = NoticeVersion(
        id=str(ULID()),
        purpose_id=p.id,
        version=1,
        language="eng",
        body_text=body,
        body_sha256=notice_digest(body),
        audio_url="https://cdn.example.com/notice/marketing_outreach.v1.eng.wav",
        published_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(nv)
    db.commit()
    return p


@pytest.fixture()
def verified_purpose(db):
    from ulid import ULID
    from app.consent_service import notice_digest
    from app.models import NoticeVersion, Purpose

    p = Purpose(
        id=str(ULID()),
        code="credit_profiling",
        name="credit profiling",
        retention_days=1095,
        requires_verification=True,
        ucm_purpose_key="credit_profiling",
    )
    db.add(p)
    db.flush()
    body = "Notice for credit profiling. To agree press 1."
    db.add(NoticeVersion(
        id=str(ULID()), purpose_id=p.id, version=1, language="eng",
        body_text=body, body_sha256=notice_digest(body),
        published_at=dt.datetime.now(dt.timezone.utc),
    ))
    db.commit()
    return p


@pytest.fixture()
def client(database):
    from fastapi.testclient import TestClient
    from app.main import create_app

    return TestClient(create_app())
