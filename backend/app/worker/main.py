"""Bot worker (deployable: `worker`).

Scaffold stub. The worker's lifecycle contract, which every future addition must keep:

1. Start HALTED. No trading state is assumed valid at boot.
2. Load config (fail closed on anything missing).
3. Reconcile against Webull (Invariant 6) — stay halted until reconciliation passes.
4. Only then begin the scheduler loop (market hours per exchange_calendars, never
   hand-rolled).

No trading logic exists yet; this stub starts, logs, and exits cleanly.
"""

import structlog

from app.core.config import load_settings

log = structlog.get_logger()


def main() -> None:
    log.info("worker.starting", state="HALTED")
    settings = load_settings()
    log.info("worker.config_loaded", webull_env=settings.webull_env)
    log.info("worker.stub_exit", reason="no trading logic implemented yet — scaffold only")


if __name__ == "__main__":
    main()
