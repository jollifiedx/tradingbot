"""Dashboard REST API (deployable: `api`).

Serves the React PWA. Never talks to Webull (Architecture Invariant 2) — the UI
mutates `settings` in the database and the worker obeys.

Every data route is gated by the single-owner auth dependency (`require_owner`
in `app/api/deps.py`), because the DB layer connects as a role that bypasses
RLS. `/health` is the only unauthenticated route.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import account, decisions, settings
from app.core.db import lifespan

app = FastAPI(title="TradingBot API", version="0.1.0", lifespan=lifespan)

# CORS: the browser blocks the PWA (a different origin than this API) from
# calling these routes unless we explicitly allow its origin. These are the
# LOCAL DEV frontend origins only. The deployed frontend origin (Vercel) must
# be added here — ideally via env/config — before the API is deployed; do NOT
# widen this to "*", which would let any site call the owner's control surface.
_DEV_FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEV_FRONTEND_ORIGINS,
    allow_credentials=False,  # auth is a Bearer token, not cookies
    allow_methods=["GET", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(settings.router)
app.include_router(decisions.router)
app.include_router(account.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
