"""GET /account -- latest account equity snapshot (owner-only).

Invariant 2: the dashboard API never talks to Webull. Account figures are
served from the `equity_snapshots` table (written by the worker's reconcile
loop), NOT fetched live from the broker. Returns 404 until the worker has
recorded the first snapshot.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbDep, OwnerId
from app.core.db import DatabaseError
from app.core.models import EquitySnapshot

router = APIRouter(tags=["account"])


@router.get("/account", response_model=EquitySnapshot)
async def get_account(owner: OwnerId, db: DbDep) -> EquitySnapshot:
    """Return the most recent equity snapshot, or 404 if none recorded yet."""
    try:
        snapshot = await db.get_latest_equity_snapshot()
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not read account snapshot",
        ) from exc
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no account snapshot recorded yet",
        )
    return snapshot
