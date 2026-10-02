"""Shared plumbing for provider webhooks: URL reconstruction, verification,
receipt recording, session lookup."""
from __future__ import annotations

import hashlib
import logging

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from ulid import ULID

from app.config import settings
from app.models import IvrSession, WebhookReceipt
from app.telephony import get as get_provider
from app.telephony.base import Verification, WebhookEvent

log = logging.getLogger("webhook")


def public_url(request: Request) -> str:
    """Rebuild the URL Twilio actually signed.

    Behind a TLS-terminating proxy the framework reports http://, and the
    signature then never validates. PUBLIC_BASE_URL is the external origin;
    X-Forwarded-Proto is the fallback.
    """
    base = settings().public_base_url.rstrip("/")
    path = request.url.path
    query = request.url.query
    url = f"{base}{path}"
    return f"{url}?{query}" if query else url


async def read_request(request: Request) -> tuple[dict, bytes]:
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return {}, body
    if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/"):
        form = await request.form()
        return {k: str(v) for k, v in form.items()}, body
    return {}, body


async def ingest(
    db: Session, request: Request, provider_name: str, route: str
) -> tuple[WebhookEvent, Verification, IvrSession | None]:
    """Verify, record a receipt, and resolve the session. The receipt is
    written whether or not verification passed -- a rejected webhook is
    exactly the thing you want a record of."""
    provider = get_provider(provider_name)
    form, body = await read_request(request)
    url = public_url(request)
    headers = {k.lower(): v for k, v in request.headers.items()}

    verification = provider.verify(url=url, headers=headers, form=form, body=body)
    event = provider.parse(form=form, query=dict(request.query_params))
    sess = find_session(db, event)

    # Store exactly the parameters the signature covers -- the POST form,
    # not a merge with the query string, which rides on the URL instead.
    # Merging makes the stored triple fail to re-verify later, which defeats
    # the point of keeping it.
    receipt = WebhookReceipt(
        id=str(ULID()),
        ivr_session_id=sess.id if sess else None,
        provider=provider_name,
        route=route,
        request_url=url,
        signature=verification.signature,
        signature_ok=verification.ok if verification.cryptographic else None,
        body_sha256=hashlib.sha256(body).digest() if body else None,
        params={k: v for k, v in form.items() if k.lower() != "authtoken"},
    )
    db.add(receipt)
    db.flush()
    # An inbound call's first webhook arrives before its session exists; the
    # route that creates the session links this receipt to it afterwards.
    request.state.webhook_receipt = receipt
    return event, verification, sess


def find_session(db: Session, event: WebhookEvent) -> IvrSession | None:
    if event.session_ref:
        sess = db.get(IvrSession, event.session_ref)
        if sess:
            return sess
    if event.call_ref:
        return db.execute(
            select(IvrSession).where(IvrSession.call_sid == event.call_ref)
        ).scalar_one_or_none()
    return None


def ingest_sync(
    db: Session, request: Request, provider_name: str, route: str
) -> tuple[WebhookEvent, Verification, IvrSession | None]:
    """Same as ingest, for providers that only ever send GET with query
    parameters and therefore need no body read."""
    provider = get_provider(provider_name)
    url = public_url(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    verification = provider.verify(url=url, headers=headers, form={}, body=b"")
    event = provider.parse(form={}, query=dict(request.query_params))
    sess = find_session(db, event)

    db.add(WebhookReceipt(
        id=str(ULID()),
        ivr_session_id=sess.id if sess else None,
        provider=provider_name,
        route=route,
        request_url=url,
        signature=verification.signature,
        signature_ok=verification.ok if verification.cryptographic else None,
        body_sha256=None,
        params=dict(event.raw or {}),
    ))
    db.flush()
    return event, verification, sess
