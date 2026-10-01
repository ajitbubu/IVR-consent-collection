"""Outbox drain loop. Run as a separate process from the API."""
from __future__ import annotations

import logging
import time

from app.db import session_scope
from app.outbox import drain, lag_seconds
from app.ucm import UcmClient

log = logging.getLogger("worker")


def run_once() -> dict[str, int]:
    client = UcmClient()
    with session_scope() as db:
        counts = drain(db, client=client)
        counts["lag_seconds"] = int(lag_seconds(db))
    return counts


def run_forever(interval_s: float = 2.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    while True:
        try:
            counts = run_once()
            if any(counts[k] for k in ("delivered", "retry", "failed", "paused")):
                log.info("drain %s", counts)
        except Exception:
            log.exception("drain failed")
        time.sleep(interval_s)


if __name__ == "__main__":
    run_forever()
