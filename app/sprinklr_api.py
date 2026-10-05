"""Sprinklr REST API: OAuth 2.0 code grant, token storage, and the client.

This is the direction from us to Sprinklr. (Sprinklr calling us is
app/routes_sprinklr.py, authenticated by SPRINKLR_WEBHOOK_TOKEN instead.)

Everything below follows dev.sprinklr.com (Getting Started, Refreshing
Access Token, Authorization Troubleshooting; read 2026-10-05):

  authorize  GET  {base}/{env}/oauth/authorize?client_id&response_type=code&redirect_uri
  token      POST {base}/{env}/oauth/token   grant_type=authorization_code | refresh_token
  API calls  Authorization: Bearer {access_token}  +  key: {api_key}

  * "prod" has no {env} path segment; every other environment does.
  * Only one token exists per API key: issuing one invalidates the last.
  * A refresh token never expires but works once; each refresh returns a
    new one. Two processes refreshing at once would lock each other out, so
    refreshes run under a row lock.
  * Token parameters may go in the query or the body. They go in the body
    here, so the client secret never appears in a URL or a request log.

Tokens are stored encrypted and never logged.
"""
from __future__ import annotations

import datetime as dt
import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import decrypt_attribute, encrypt_attribute
from app.models import SprinklrOauthToken

log = logging.getLogger("sprinklr_api")

# Refresh once 80% of a token's lifetime has passed. Lifetimes vary (30 days
# from a code grant, 8 hours in the documented refresh example), so a fixed
# margin would either refresh constantly or too late.
_REFRESH_AT = 0.8


class SprinklrAuthError(Exception):
    """Not connected, misconfigured, or Sprinklr refused the token request.
    Messages never contain a token or the client secret."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def configured() -> bool:
    s = settings()
    return all(v and v != "unset" for v in (s.sprinklr_api_key, s.sprinklr_api_secret))


def env_base() -> str:
    s = settings()
    base = s.sprinklr_api_base.rstrip("/")
    env = s.sprinklr_env.strip().lower()
    return base if env in ("", "prod") else f"{base}/{env}"


def authorize_url(state: str) -> str:
    # TODO(sprinklr): `state` is standard OAuth 2.0 but not mentioned in
    # Sprinklr's docs. Confirm the authorize step echoes it back: the callback
    # refuses a response without it.
    s = settings()
    return f"{env_base()}/oauth/authorize?" + urlencode({
        "client_id": s.sprinklr_api_key,
        "response_type": "code",
        "redirect_uri": s.sprinklr_redirect_uri,
        "state": state,
    })


def _token_request(params: dict) -> dict:
    s = settings()
    form = {
        "client_id": s.sprinklr_api_key,
        "client_secret": s.sprinklr_api_secret,
        "redirect_uri": s.sprinklr_redirect_uri,
        **params,
    }
    try:
        resp = httpx.post(
            f"{env_base()}/oauth/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise SprinklrAuthError(f"token endpoint unreachable: {type(exc).__name__}") from None
    if resp.status_code != 200:
        # The status alone: an error body is not worth the risk of echoing input.
        raise SprinklrAuthError(f"token endpoint HTTP {resp.status_code}")
    body = resp.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise SprinklrAuthError("token response is missing a token")
    return body


def _store(db: Session, body: dict) -> SprinklrOauthToken:
    now = _now()
    env = settings().sprinklr_env
    row = db.get(SprinklrOauthToken, env) or SprinklrOauthToken(env=env)
    row.access_token_enc = encrypt_attribute(body["access_token"])
    row.refresh_token_enc = encrypt_attribute(body["refresh_token"])
    row.issued_at = now
    row.expires_at = now + dt.timedelta(seconds=int(body["expires_in"]))
    row.updated_at = now
    db.add(row)
    db.flush()
    log.info("sprinklr token stored env=%s expires_at=%s", env, row.expires_at.isoformat())
    return row


def exchange_code(db: Session, code: str) -> SprinklrOauthToken:
    """Step 2 of the code grant. The code is valid for 10 minutes, once."""
    return _store(db, _token_request({"grant_type": "authorization_code", "code": code}))


def _due(row: SprinklrOauthToken, now: dt.datetime) -> bool:
    return now >= row.issued_at + (row.expires_at - row.issued_at) * _REFRESH_AT


def refresh(db: Session, *, force: bool = False) -> SprinklrOauthToken:
    """Refresh if due (or forced). The row lock means a second process waits,
    re-reads the token the first one stored, and finds it no longer due."""
    row = db.execute(
        select(SprinklrOauthToken)
        .where(SprinklrOauthToken.env == settings().sprinklr_env)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise SprinklrAuthError("not connected: open /sprinklr/oauth/login")
    if not force and not _due(row, _now()):
        return row
    return _store(db, _token_request({
        "grant_type": "refresh_token",
        "refresh_token": decrypt_attribute(row.refresh_token_enc),
    }))


def refresh_if_due(db: Session) -> str:
    """The app.jobs step: keeps the token fresh even when nothing uses it."""
    if not settings().sprinklr_api_enabled:
        return "disabled"
    row = db.get(SprinklrOauthToken, settings().sprinklr_env)
    if row is None:
        return "not_connected"
    before = row.issued_at
    return "refreshed" if refresh(db).issued_at != before else "fresh"


def access_token(db: Session) -> str:
    return decrypt_attribute(refresh(db).access_token_enc)


class SprinklrClient:
    def __init__(self, db: Session):
        self.db = db

    def get(self, path: str) -> httpx.Response:
        """GET {env_base}{path}, e.g. "/api/v2/me" to check the connection."""
        return httpx.get(
            f"{env_base()}{path}",
            headers={
                "Authorization": f"Bearer {access_token(self.db)}",
                "key": settings().sprinklr_api_key,
            },
            timeout=15.0,
        )

