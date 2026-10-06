"""Evidence jobs loop. Run as its own process, beside the API and the
outbox worker: `python -m app.jobs`.

Each pass reconciles finished calls, copies queued recordings into our
storage, purges recordings past retention, anchors yesterday's chain
digest once, and refreshes the Sprinklr API token when it is due (only with
SPRINKLR_API_ENABLED). Every step is safe to repeat, so a crash just means the next
pass picks up where this one stopped.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from app.db import session_scope
from app.evidence import anchor_daily_digest, fetch_pending_recordings, purge_expired_artifacts
from app.reconcile import reconcile_due
from app.sprinklr_api import refresh_if_due

log = logging.getLogger("jobs")


def run_once(today: dt.date | None = None) -> dict:
    yesterday = (today or dt.datetime.now(dt.timezone.utc).date()) - dt.timedelta(days=1)
    steps = {
        "reconcile": reconcile_due,
        "recordings": fetch_pending_recordings,
        "purged": purge_expired_artifacts,
        # Yesterday is complete; today's chains are still growing.
        "digest": lambda db: anchor_daily_digest(db, yesterday),
        "sprinklr_token": refresh_if_due,
    }
    out: dict = {}
    # One transaction per step: a failure in one never rolls back or skips another.
    for name, step in steps.items():
        try:
            with session_scope() as db:
                out[name] = step(db)
        except Exception:
            log.exception("job step %s failed", name)
            out[name] = "error"
    return out


def run_forever(interval_s: float = 60.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    while True:
        log.info("jobs %s", run_once())
        time.sleep(interval_s)


if __name__ == "__main__":
    run_forever()
