"""Bot worker entrypoint (deployable: `worker`).

Thin by design: everything with a decision in it lives in
:mod:`app.worker.scheduler`, which is exhaustively tested with no network. This
file only builds the real collaborators and hands them over.

Lifecycle contract (see the scheduler module docstring for the full rules):

1. Start HALTED. No trading state is assumed valid at boot.
2. Load config (fail closed on anything missing).
3. Open the DB, probe `settings`, reconcile against Webull (invariant #6).
4. Only then schedule jobs -- market hours per `exchange_calendars`, never
   hand-rolled -- and only register trading jobs if that startup tick came back
   CLEAR.

No trading logic exists yet: there is no rules engine and no order path, so the
scheduler runs reconciliation, the posture tick, and the daily equity snapshot.
"""

from __future__ import annotations

import asyncio
from functools import partial

import structlog

from app.core.config import load_settings
from app.core.db import Database
from app.core.webull import WebullClient
from app.worker.market_hours import MarketClock
from app.worker.reconciliation import reconcile
from app.worker.scheduler import Worker
from app.worker.snapshot import take_snapshot

log = structlog.get_logger()


async def run_worker() -> None:
    """Build the real worker and run it until the process is stopped."""
    settings = load_settings()
    log.info("worker.config_loaded", webull_env=settings.webull_env)
    client = WebullClient(settings)
    db = await Database.connect(settings.database_url)
    try:
        worker = Worker(
            db=db,
            reconcile_fn=partial(reconcile, settings=settings, client=client, db=db),
            snapshot_fn=partial(
                take_snapshot, settings=settings, client=client, db=db
            ),
            market_clock=MarketClock(),
        )
        await worker.run()
    finally:
        await db.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
