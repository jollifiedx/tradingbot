"""GET + PATCH /settings -- the owner's freeze/buy-power-cap control surface.

GET reads the singleton `settings` row; PATCH is the write path for the owner's
whole control surface (freeze/unfreeze, caps, staleness threshold). Both are
gated by the single-owner auth dependency (`require_owner`), and PATCH writes to
Supabase only -- it NEVER calls Webull (Architecture Invariant 2). The UI mutates
`settings`; the worker re-reads `settings` before every order and obeys.

Validation of the PATCH body mirrors the DB CHECK constraints
(20260719000004_settings.sql) at the API layer, so malformed input is a clean
422 here rather than surfacing as a 503 from the database. Money is `Decimal`,
never float. The write itself goes through `Database.update_settings`, which
attributes the change (`updated_by`) and thereby fires the `log_settings_history`
audit trigger.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from app.api.deps import DbDep, OwnerId
from app.core.db import DatabaseError
from app.core.models import BotSettings, Money14_2NonNeg

router = APIRouter(tags=["settings"])

# Same runtime validation as the response money type (numeric(14,2), >= 0), but
# force the *input* JSON Schema to a plain string so the generated TS client
# treats money as a string and never a JS `number` (which would lose precision).
# WithJsonSchema changes only the emitted schema; the underlying Decimal core
# schema still parses and enforces the constraints at runtime.
MoneyPatchInput = Annotated[
    Money14_2NonNeg, WithJsonSchema({"type": "string"}, mode="validation")
]


class SettingsUpdate(BaseModel):
    """Partial update to the singleton `settings` row (owner control surface).

    PATCH semantics: every field is optional; an omitted field is left
    unchanged (coalesced by the DB helper). Validation mirrors the SQL CHECK
    constraints -- caps are non-negative, `staleness_threshold_seconds` is
    positive -- so bad input fails 422 at the edge instead of hitting the
    database. Money fields are `Decimal` (the same annotated types the response
    model uses), never float. Unknown fields are rejected (`extra="forbid"`) so
    a caller cannot smuggle in `id`/`updated_at`/`updated_by`, and an all-empty
    body is rejected because there is nothing to update.
    """

    model_config = ConfigDict(extra="forbid")

    frozen: bool | None = None
    buy_power_cap: MoneyPatchInput | None = None
    max_daily_loss: MoneyPatchInput | None = None
    max_per_trade_cap: MoneyPatchInput | None = None
    staleness_threshold_seconds: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _require_at_least_one(self) -> SettingsUpdate:
        if all(
            value is None
            for value in (
                self.frozen,
                self.buy_power_cap,
                self.max_daily_loss,
                self.max_per_trade_cap,
                self.staleness_threshold_seconds,
            )
        ):
            raise ValueError("at least one field must be provided")
        return self


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


@router.patch("/settings", response_model=BotSettings)
async def update_settings(
    owner: OwnerId, db: DbDep, body: SettingsUpdate
) -> BotSettings:
    """Apply a partial update to the owner's control surface, return the result.

    Owner-gated (unreachable without the single-owner auth gate). Writes
    `settings` in Supabase only -- never Webull (Invariant 2). `owner` (the
    authenticated owner's id) is recorded as `updated_by`, which drives the
    `settings_history` audit trigger. Only fields the caller actually supplied
    are forwarded; omitted fields are left unchanged. A DB failure maps to 503
    (fail closed); the helper's "no fields" guard maps to 422 (Pydantic
    normally rejects an empty body first).
    """
    provided = body.model_dump(exclude_none=True)
    try:
        return await db.update_settings(updated_by=owner, **provided)
    except ValueError as exc:
        # Defensive: SettingsUpdate rejects an all-None body before we get here.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except DatabaseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not update settings",
        ) from exc
