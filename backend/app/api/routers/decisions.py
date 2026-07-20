"""GET /decisions -- read the append-only decision audit log (owner-only).

Read-only by design: `decisions` is an append-only audit table (Invariant 5),
so this module exposes no write verb. Writes happen only in the worker, via
the service_role, never through the dashboard API.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbDep, OwnerId
from app.core.db import DatabaseError
from app.core.models import Decision

router = APIRouter(tags=["decisions"])


@router.get("/decisions", response_model=list[Decision])
async def get_decisions(
    owner: OwnerId,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Decision]:
    """Return decisions newest-first (`decided_at desc`), paginated."""
    try:
        return await db.get_decisions(limit=limit, offset=offset)
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not read decisions",
        ) from exc
