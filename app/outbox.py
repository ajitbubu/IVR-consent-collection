"""Outbox worker: drains queued consents into UCM.

Two guarantees that matter more than throughput:

  * strict per-principal ordering by decided_at -- a grant and a withdrawal
    delivered out of order leaves UCM holding a live consent the person has
    revoked, which is the worst failure this system can produce;
  * nothing is ever dropped -- exhausted rows are marked failed and alerted
    on, and stay in the table.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Consent, UcmOutbox
from app.ucm import UcmClient, UcmRejected, UcmUnavailable

log = logging.getLogger("outbox")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def backoff_seconds(attempts: int) -> int:
    s = settings()
    return min(s.outbox_base_backoff_s * (2 ** max(attempts - 1, 0)), s.outbox_max_backoff_s)


def claim_batch(db: Session, limit: int = 50) -> list[UcmOutbox]:
    """One row per principal per pass: the oldest undelivered decision for
    each. Advancing past a principal's head would break ordering."""
    now = _now()
    rows = list(
        db.execute(
            select(UcmOutbox)
            .where(
                UcmOutbox.delivered_at.is_(None),
                UcmOutbox.paused.is_(False),
                UcmOutbox.next_attempt_at <= now,
            )
            .order_by(UcmOutbox.data_principal_id, UcmOutbox.decided_at, UcmOutbox.id)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    seen: set[str] = set()
    head_of_each: list[UcmOutbox] = []
    for r in rows:
        if r.data_principal_id in seen:
            continue
        seen.add(r.data_principal_id)
        head_of_each.append(r)
        if len(head_of_each) >= limit:
            break
    return head_of_each


def deliver_one(db: Session, row: UcmOutbox, client: UcmClient) -> str:
    """Returns 'delivered', 'retry', 'failed' or 'paused'."""
    row.attempts += 1
    consent = db.get(Consent, row.consent_id)
    try:
        ref = client.push(row.payload, row.idempotency_key)
    except UcmUnavailable as exc:
        row.last_error = str(exc)[:500]
        if row.attempts >= settings().outbox_max_attempts:
            consent.ucm_sync_state = "failed"
            row.next_attempt_at = _now() + dt.timedelta(seconds=settings().outbox_max_backoff_s)
            log.error("outbox exhausted consent_id=%s err=%s", row.consent_id, exc)
            db.flush()
            return "failed"
        row.next_attempt_at = _now() + dt.timedelta(seconds=backoff_seconds(row.attempts))
        db.flush()
        return "retry"
    except UcmRejected as exc:
        # A bad payload should not stop the queue for everyone else.
        row.paused = True
        row.last_error = str(exc)[:500]
        consent.ucm_sync_state = "failed"
        log.error("outbox paused on rejection consent_id=%s err=%s", row.consent_id, exc)
        db.flush()
        return "paused"

    row.delivered_at = _now()
    row.last_error = None
    consent.ucm_sync_state = "synced"
    consent.ucm_consent_ref = ref
    db.flush()
    return "delivered"


def drain(db: Session, client: UcmClient | None = None, limit: int = 50) -> dict[str, int]:
    client = client or UcmClient()
    counts = {"delivered": 0, "retry": 0, "failed": 0, "paused": 0}
    for row in claim_batch(db, limit=limit):
        counts[deliver_one(db, row, client)] += 1
    return counts


def lag_seconds(db: Session) -> float:
    """Oldest undelivered decision age -- the number to alert on."""
    oldest = db.execute(
        select(UcmOutbox.decided_at)
        .where(UcmOutbox.delivered_at.is_(None), UcmOutbox.paused.is_(False))
        .order_by(UcmOutbox.decided_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if oldest is None:
        return 0.0
    return (_now() - oldest).total_seconds()
