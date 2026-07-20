"""Dashboard REST API (deployable: `api`).

Serves the React PWA. Never talks to Webull (Architecture Invariant 2) — the UI
mutates `settings` in the database and the worker obeys.
"""

from fastapi import FastAPI

app = FastAPI(title="TradingBot API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
