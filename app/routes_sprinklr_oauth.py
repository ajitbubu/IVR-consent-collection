"""Connect this service to the Sprinklr REST API (OAuth 2.0 code grant).

An operator opens /sprinklr/oauth/login in a browser, signs in to Sprinklr,
chooses the environments to grant, and lands back on /sprinklr/oauth/callback,
which stores the tokens encrypted. This is an admin action: like the console,
it belongs behind the PMP gateway in production.
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.sprinklr_api import SprinklrAuthError, authorize_url, configured, exchange_code

log = logging.getLogger("sprinklr_oauth")
router = APIRouter(prefix="/sprinklr/oauth", tags=["sprinklr"])

STATE_COOKIE = "sprinklr_oauth_state"
COOKIE_PATH = "/sprinklr/oauth"


class _RedactCode(logging.Filter):
    """The access log would otherwise record the authorization code from the
    callback's query string."""

    _code = re.compile(r"(code=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._code.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactCode())


def _error(status: int, detail: str) -> JSONResponse:
    resp = JSONResponse({"connected": False, "detail": detail}, status_code=status)
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp


@router.get("/login")
def login() -> Response:
    if not configured():
        return JSONResponse(
            {"detail": "SPRINKLR_API_KEY and SPRINKLR_API_SECRET are not set"}, status_code=503
        )
    state = secrets.token_urlsafe(32)
    resp = RedirectResponse(authorize_url(state), status_code=302)
    # The authorization code is valid for 10 minutes; so is the state.
    resp.set_cookie(STATE_COOKIE, state, max_age=600, path=COOKIE_PATH,
                    httponly=True, secure=True, samesite="lax")
    return resp


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_session),
) -> JSONResponse:
    if error:
        return _error(400, f"Sprinklr returned an error: {error}")
    expected = request.cookies.get(STATE_COOKIE)
    if not state or not expected or not hmac.compare_digest(state, expected):
        log.warning("sprinklr oauth callback rejected: state mismatch")
        return _error(400, "state mismatch: start again at /sprinklr/oauth/login")
    if not code:
        return _error(400, "no authorization code in the callback")

    try:
        row = exchange_code(db, code)
    except SprinklrAuthError as exc:
        log.warning("sprinklr code exchange failed: %s", exc)
        return _error(502, str(exc))

    resp = JSONResponse({
        "connected": True,
        "env": settings().sprinklr_env,
        "expires_at": row.expires_at.isoformat(),
    })
    resp.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return resp
