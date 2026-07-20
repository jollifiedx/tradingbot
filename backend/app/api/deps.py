"""FastAPI dependencies: the single-owner auth gate and DB access.

Why this exists (read `app/core/db.py` security note first): the API reaches
the database through a role that BYPASSES Row Level Security, so RLS does not
enforce "is this caller Esther?" for API requests. This module is where that
check lives instead, and it must guard EVERY route that touches the DB.

Auth model (owner-approved): a caller presents a Supabase Auth JWT as a Bearer
token. We verify it by introspection -- `GET {SUPABASE_URL}/auth/v1/user` with
the token -- which returns the authenticated user if (and only if) the token is
valid and unexpired. We then check that user's id against the `app_owner`
allowlist (the single source of truth). Anyone who is not the one allowlisted
owner is refused, even with a valid Supabase token.

Fail closed: if Supabase can't be reached, or the ownership lookup fails, the
request is refused (503) -- never allowed through on uncertainty.

Note on approach: introspection makes one network call per request. For a
single-user dashboard that is fine and avoids managing JWT signing keys. Local
JWT signature verification (JWKS) is a possible future optimization; it would
not change this module's contract.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import httpx
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, load_settings
from app.core.db import Database, DatabaseError

log = structlog.get_logger()

# auto_error=False so we return our own 401 (not FastAPI's 403) when the
# Authorization header is missing.
_bearer = HTTPBearer(auto_error=False)

_INTROSPECT_TIMEOUT_S = 8.0


def get_db(request: Request) -> Database:
    """The shared `Database` opened by the lifespan handler."""
    db = getattr(request.app.state, "db", None)
    if not isinstance(db, Database):  # pragma: no cover -- misconfiguration
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database not initialized",
        )
    return db


def get_config() -> Settings:
    return load_settings()


async def _introspect(token: str, config: Settings) -> UUID:
    """Return the Supabase user id for a valid token, else raise 401/503."""
    url = f"{config.supabase_url.rstrip('/')}/auth/v1/user"
    headers = {"Authorization": f"Bearer {token}", "apikey": config.supabase_anon_key}
    try:
        async with httpx.AsyncClient(timeout=_INTROSPECT_TIMEOUT_S) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        # Can't verify the token -> refuse (fail closed), don't allow through.
        log.warning("auth.introspect_unreachable", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service unavailable",
        ) from exc
    if resp.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        )
    user_id = resp.json().get("id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token did not resolve to a user",
        )
    return UUID(str(user_id))


async def require_owner(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Settings, Depends(get_config)],
) -> UUID:
    """Gate a route to the single allowlisted owner; return their user id.

    Raises 401 (missing/invalid token), 403 (valid token, not the owner), or
    503 (can't verify -- fail closed). The returned UUID is the authenticated
    owner's id, suitable for attribution (e.g. `settings.updated_by`).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = await _introspect(credentials.credentials, config)
    try:
        is_owner = await db.is_owner(user_id)
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not verify ownership",
        ) from exc
    if not is_owner:
        # Valid Supabase user, but not THE owner. Single-tenant, forever.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not authorized"
        )
    return user_id


OwnerId = Annotated[UUID, Depends(require_owner)]
DbDep = Annotated[Database, Depends(get_db)]
