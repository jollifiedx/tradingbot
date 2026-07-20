"""GET /settings -- read the singleton control-surface row (owner-only).

The PATCH counterpart (the freeze/cap write path) lives in its own module,
built and owned separately (execution-guardian) because it mutates a safety
mechanism.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbDep, OwnerId
from app.core.db import DatabaseError
from app.core.models import BotSettings

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=BotSettings)
async def get_settings(owner: OwnerId, db: DbDep) -> BotSettings:
    """Return the current bot settings (frozen flag, caps, staleness)."""
    try:
        return await db.get_settings()
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not read settings",
        ) from exc
