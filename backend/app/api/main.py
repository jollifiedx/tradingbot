"""Dashboard REST API (deployable: `api`).

Serves the React PWA. Never talks to Webull (Architecture Invariant 2) — the UI
mutates `settings` in the database and the worker obeys.

Every data route is gated by the single-owner auth dependency (`require_owner`
in `app/api/deps.py`), because the DB layer connects as a role that bypasses
RLS. `/health` is the only unauthenticated route.
"""

from fastapi import FastAPI

from app.api.routers import account, decisions, settings
from app.core.db import lifespan

app = FastAPI(title="TradingBot API", version="0.1.0", lifespan=lifespan)

app.include_router(settings.router)
app.include_router(decisions.router)
app.include_router(account.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
