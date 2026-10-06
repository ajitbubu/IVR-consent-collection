"""Sprinklr REST API: OAuth code grant, encrypted token storage, refresh."""
from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import sprinklr_api
from app.models import SprinklrOauthToken
from app.telephony.base import CallLookupUnavailable

SECRET = "s3cret-value"


@pytest.fixture()
def spr(monkeypatch):
    from app.config import reset_settings

    monkeypatch.setenv("SPRINKLR_API_KEY", "key-123")
    monkeypatch.setenv("SPRINKLR_API_SECRET", SECRET)
    monkeypatch.setenv("SPRINKLR_ENV", "prod2")
    monkeypatch.setenv("SPRINKLR_REDIRECT_URI", "https://localhost:8088/sprinklr/oauth/callback")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture()
def https_client(database):
    from app.main import create_app

    # The state cookie is Secure, so the browser side must be https.
    return TestClient(create_app(), base_url="https://localhost:8088")


class FakeTokenEndpoint:
    def __init__(self, tokens=("access-1", "refresh-1"), status=200, expires_in=2591999):
        self.calls: list[tuple[str, dict]] = []
        self.tokens, self.status, self.expires_in = list(tokens), status, expires_in

    def __call__(self, url, data, headers, timeout):
        self.calls.append((url, dict(data)))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "invalid_grant"})
        return httpx.Response(200, json={
            "access_token": self.tokens[0], "refresh_token": self.tokens[1],
            "token_type": "Bearer", "expires_in": self.expires_in,
        })


def _login(client):
    r = client.get("/sprinklr/oauth/login", follow_redirects=False)
    assert r.status_code == 302
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0], r


# ------------------------------------------------------------------ urls


def test_authorize_url_follows_the_documented_shape(spr):
    url = urlparse(sprinklr_api.authorize_url("st"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://api3.sprinklr.com/prod2/oauth/authorize"
    q = parse_qs(url.query)
    assert q == {"client_id": ["key-123"], "response_type": ["code"],
                 "redirect_uri": ["https://localhost:8088/sprinklr/oauth/callback"],
                 "state": ["st"]}


def test_prod_has_no_environment_segment(spr, monkeypatch):
    from app.config import reset_settings

    monkeypatch.setenv("SPRINKLR_ENV", "prod")
    reset_settings()
    assert sprinklr_api.env_base() == "https://api3.sprinklr.com"


# ------------------------------------------------------------------ login + callback


def test_login_without_credentials_is_503(database):
    from app.main import create_app

    r = TestClient(create_app()).get("/sprinklr/oauth/login", follow_redirects=False)
    assert r.status_code == 503


def test_code_is_exchanged_and_tokens_stored_encrypted(db, spr, https_client, monkeypatch,
                                                         caplog):
    fake = FakeTokenEndpoint(tokens=("access-AAA", "refresh-BBB"))
    monkeypatch.setattr(sprinklr_api.httpx, "post", fake)
    state, _ = _login(https_client)

    with caplog.at_level(logging.DEBUG):
        r = https_client.get("/sprinklr/oauth/callback", params={"code": "c0de", "state": state})
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is True
    assert "access-AAA" not in r.text and "refresh-BBB" not in r.text

    url, form = fake.calls[0]
    assert url == "https://api3.sprinklr.com/prod2/oauth/token"
    assert form == {"client_id": "key-123", "client_secret": SECRET, "code": "c0de",
                    "grant_type": "authorization_code",
                    "redirect_uri": "https://localhost:8088/sprinklr/oauth/callback"}
    assert SECRET not in url

    row = db.execute(select(SprinklrOauthToken)).scalar_one()
    assert b"access-AAA" not in row.access_token_enc
    assert sprinklr_api.access_token(db) == "access-AAA"
    assert row.expires_at - row.issued_at == dt.timedelta(seconds=2591999)
    for secret in ("access-AAA", "refresh-BBB", SECRET):
        assert secret not in caplog.text


def test_state_mismatch_is_refused_and_nothing_exchanged(db, spr, https_client, monkeypatch):
    fake = FakeTokenEndpoint()
    monkeypatch.setattr(sprinklr_api.httpx, "post", fake)
    _login(https_client)

    r = https_client.get("/sprinklr/oauth/callback", params={"code": "c0de", "state": "forged"})
    assert r.status_code == 400
    assert fake.calls == []
    assert db.execute(select(SprinklrOauthToken)).scalars().all() == []


def test_callback_without_login_cookie_is_refused(db, spr, https_client):
    r = https_client.get("/sprinklr/oauth/callback", params={"code": "c0de", "state": "x"})
    assert r.status_code == 400


def test_sprinklr_error_is_reported_without_exchanging(db, spr, https_client):
    state, _ = _login(https_client)
    r = https_client.get("/sprinklr/oauth/callback",
                         params={"error": "access_denied", "state": state})
    assert r.status_code == 400 and "access_denied" in r.json()["detail"]


def test_refused_exchange_is_502_with_no_detail_leak(db, spr, https_client, monkeypatch):
    monkeypatch.setattr(sprinklr_api.httpx, "post", FakeTokenEndpoint(status=400))
    state, _ = _login(https_client)
    r = https_client.get("/sprinklr/oauth/callback", params={"code": "old", "state": state})
    assert r.status_code == 502
    assert r.json()["detail"] == "token endpoint HTTP 400"


# ------------------------------------------------------------------ refresh


def _seed(db, monkeypatch, *, age_fraction: float, lifetime_s: int = 1000):
    monkeypatch.setattr(sprinklr_api.httpx, "post",
                        FakeTokenEndpoint(tokens=("access-1", "refresh-1"), expires_in=lifetime_s))
    sprinklr_api.exchange_code(db, "c0de")
    row = db.execute(select(SprinklrOauthToken)).scalar_one()
    shift = dt.timedelta(seconds=lifetime_s * age_fraction)
    row.issued_at -= shift
    row.expires_at -= shift
    db.commit()
    return row


def test_token_is_refreshed_once_most_of_its_life_is_gone(db, spr, monkeypatch):
    _seed(db, monkeypatch, age_fraction=0.85)
    fake = FakeTokenEndpoint(tokens=("access-2", "refresh-2"), expires_in=28799)
    monkeypatch.setattr(sprinklr_api.httpx, "post", fake)

    assert sprinklr_api.access_token(db) == "access-2"
    assert fake.calls[0][1]["grant_type"] == "refresh_token"
    assert fake.calls[0][1]["refresh_token"] == "refresh-1"
    # The refresh token is single-use: the new one replaces it.
    row = db.execute(select(SprinklrOauthToken)).scalar_one()
    from app.crypto import decrypt_attribute
    assert decrypt_attribute(row.refresh_token_enc) == "refresh-2"


def test_fresh_token_is_not_refreshed(db, spr, monkeypatch):
    _seed(db, monkeypatch, age_fraction=0.1)
    fake = FakeTokenEndpoint()
    monkeypatch.setattr(sprinklr_api.httpx, "post", fake)
    assert sprinklr_api.access_token(db) == "access-1"
    assert fake.calls == []


def test_not_connected_says_where_to_go(db, spr):
    with pytest.raises(sprinklr_api.SprinklrAuthError, match="/sprinklr/oauth/login"):
        sprinklr_api.access_token(db)


def test_jobs_step_is_off_until_enabled(db, spr, monkeypatch):
    from app.config import reset_settings

    assert sprinklr_api.refresh_if_due(db) == "disabled"
    monkeypatch.setenv("SPRINKLR_API_ENABLED", "true")
    reset_settings()
    assert sprinklr_api.refresh_if_due(db) == "not_connected"
    _seed(db, monkeypatch, age_fraction=0.9)
    monkeypatch.setattr(sprinklr_api.httpx, "post", FakeTokenEndpoint(tokens=("a3", "r3")))
    assert sprinklr_api.refresh_if_due(db) == "refreshed"
    assert sprinklr_api.refresh_if_due(db) == "fresh"


# ------------------------------------------------------------------ client + call lookup


def test_api_calls_send_bearer_and_key(db, spr, monkeypatch):
    _seed(db, monkeypatch, age_fraction=0.1)
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return httpx.Response(200, json={"name": "x"})

    monkeypatch.setattr(sprinklr_api.httpx, "get", fake_get)
    sprinklr_api.SprinklrClient(db).get("/api/v2/me")
    assert seen["url"] == "https://api3.sprinklr.com/prod2/api/v2/me"
    assert seen["headers"] == {"Authorization": "Bearer access-1", "key": "key-123"}


def test_call_lookup_is_only_used_when_enabled(spr, monkeypatch):
    from app.config import reset_settings
    from app.reconcile import _lookups

    assert "sprinklr" not in _lookups()
    monkeypatch.setenv("SPRINKLR_API_ENABLED", "true")
    reset_settings()
    assert "sprinklr" in _lookups()
    with pytest.raises(CallLookupUnavailable):
        sprinklr_api.fetch_call_details("spr-1")


def test_access_log_redacts_the_authorization_code():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"',
                               ("127.0.0.1", "GET", "/sprinklr/oauth/callback?code=abc&state=s"),
                               None)
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    assert "abc" not in record.getMessage()


@pytest.mark.parametrize("existing", [False, True])
def test_commit_failure_reports_disconnected_and_rolls_back(
    db, spr, https_client, monkeypatch, caplog, existing
):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session
    from app.crypto import decrypt_attribute
    from app.db import session_factory

    if existing:
        _seed(db, monkeypatch, age_fraction=0.1)
    fake = FakeTokenEndpoint(tokens=("new-access-secret", "new-refresh-secret"))
    monkeypatch.setattr(sprinklr_api.httpx, "post", fake)
    state, _ = _login(https_client)
    original_commit = Session.commit
    original_rollback = Session.rollback
    failed, rolled_back = [], []

    def fail_token_commit(session):
        # exchange_code has already flushed; fail only its active transaction.
        if session.in_transaction() and not failed:
            failed.append(True)
            raise OperationalError("token SQL", {}, Exception("new-access-secret"))
        return original_commit(session)

    def track_rollback(session):
        rolled_back.append(True)
        return original_rollback(session)

    monkeypatch.setattr(Session, "commit", fail_token_commit)
    monkeypatch.setattr(Session, "rollback", track_rollback)
    with caplog.at_level(logging.ERROR):
        response = https_client.get("/sprinklr/oauth/callback",
                                    params={"code": "single-use-code", "state": state})
    assert response.status_code == 503
    assert response.json()["connected"] is False
    assert "/sprinklr/oauth/login" in response.json()["detail"]
    assert failed and rolled_back and len(fake.calls) == 1
    assert "sprinklr_oauth_state" not in https_client.cookies
    assert "new-access-secret" not in response.text + caplog.text
    with session_factory()() as independent:
        row = independent.execute(select(SprinklrOauthToken)).scalar_one_or_none()
        if existing:
            assert decrypt_attribute(row.access_token_enc) == "access-1"
            assert decrypt_attribute(row.refresh_token_enc) == "refresh-1"
        else:
            assert row is None
