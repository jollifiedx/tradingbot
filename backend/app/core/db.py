"""Async database access layer (Supabase Postgres, via asyncpg -- no ORM).

SECURITY NOTE -- read before wiring any route through this module
-------------------------------------------------------------------
`Database` connects using `settings.database_url` (the direct/`service_role`
Postgres role, per `app/core/config.py`), which **bypasses Row Level Security
entirely**. Every query issued through this module sees every row regardless
of `app_owner`, `settings_select_owner`, `decisions_select_owner`, etc. -- the
same posture the worker already relies on for unrestricted reads/writes.

That means the single-user authorization check ("is this caller actually
Esther?") is NOT enforced here and NOT enforced by RLS for requests that come
in through this module -- it MUST be enforced by FastAPI auth middleware
(verifying the Supabase Auth JWT, comparing the subject against `app_owner`),
built separately, in front of every route that reaches into a `Database`
instance. Never expose a `Database` method on an unauthenticated route.

Fail-closed contract
---------------------
Every read/write helper raises :class:`DatabaseError` on any connectivity or
query failure -- callers (the route layer) must turn that into HTTP 503, never
swallow it into an empty/default result. A genuine zero-row read is NOT an
error and is returned as such (``get_decisions`` -> ``[]``,
``get_latest_equity_snapshot`` -> ``None``) -- those two outcomes are
distinct and callers must not conflate them.

Money stays `Decimal` (asyncpg's built-in numeric<->Decimal codec, no float
ever touches this layer), timestamps stay tz-aware UTC (asyncpg's built-in
timestamptz<->datetime codec, already UTC-aware), and every row is validated
into the matching `app.core.models` Pydantic model before being returned --
malformed/unexpected shapes fail loudly via `pydantic.ValidationError` rather
than being handed to callers as a raw `asyncpg.Record`.

Remaining debt (out of scope for this milestone, by brief): `theses` reads/
writes and pgvector `embedding` (de)serialization are not implemented here.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg
import structlog

from app.core.config import load_settings
from app.core.models import BotSettings, Decision, EquitySnapshot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

log = structlog.get_logger()

# Single-user bot: a small pool is plenty and keeps us well under Supabase's
# connection ceiling even if api + worker both run this module concurrently.
_DEFAULT_MIN_POOL_SIZE = 1
_DEFAULT_MAX_POOL_SIZE = 5
# Fail closed on a hung query rather than block a request/the worker loop
# indefinitely (CLAUDE.md: never trade/serve through uncertainty).
_DEFAULT_COMMAND_TIMEOUT_S = 10.0

# Failure modes that mean "the query/connection attempt did not succeed" --
# translated into DatabaseError at every call site below. Deliberately NOT
# `Exception`/`BaseException`: asyncio.CancelledError (BaseException, py>=3.8)
# and programmer errors must still propagate untouched.
_DB_FAILURE_TYPES = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)


class DatabaseError(Exception):
    """Raised on any DB connectivity or query failure.

    The route layer must catch this and respond HTTP 503 (fail closed) --
    never treat it as "no data" and substitute a default or empty result.
    """


async def _register_codecs(conn: asyncpg.Connection) -> None:
    """Decode jsonb/json columns to native Python objects on every connection.

    Without this, asyncpg returns `jsonb`/`json` columns as raw JSON text
    (`str`), not `dict`/`list` -- relevant this milestone for
    `decisions.rules_fired` / `decisions.settings_snapshot`, both typed
    `list[Any] | dict[str, Any]` on the `Decision` model.
    """
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def _create_pool(database_url: str) -> asyncpg.Pool:
    try:
        return await asyncpg.create_pool(
            dsn=database_url,
            min_size=_DEFAULT_MIN_POOL_SIZE,
            max_size=_DEFAULT_MAX_POOL_SIZE,
            command_timeout=_DEFAULT_COMMAND_TIMEOUT_S,
            init=_register_codecs,
        )
    except _DB_FAILURE_TYPES as exc:
        raise DatabaseError("failed to create the database connection pool") from exc


class Database:
    """Typed async fetch/update helpers over a shared asyncpg pool.

    One instance is meant to be created once (via :meth:`connect`, typically
    from the :func:`lifespan` context manager below) and shared across the
    `api` process -- do not open a pool per request.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> Database:
        return cls(await _create_pool(database_url))

    async def close(self) -> None:
        await self._pool.close()

    # -- reads --------------------------------------------------------- #

    async def get_settings(self) -> BotSettings:
        """Fetch the singleton `settings` row (frozen flag, caps, staleness).

        Raises :class:`DatabaseError` on any connection/query failure, and
        also if the singleton row is somehow missing -- it is seeded by
        migration `20260719000004_settings.sql` and must never legitimately
        be absent, so a missing row is treated the same as an unreadable one:
        fail closed, never assume defaults (CLAUDE.md invariant #2).
        """
        try:
            row = await self._pool.fetchrow("select * from settings where id = true")
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to read settings") from exc
        if row is None:
            raise DatabaseError("settings singleton row is missing")
        return BotSettings.model_validate(dict(row))

    async def get_decisions(self, limit: int = 50, offset: int = 0) -> list[Decision]:
        """Fetch decisions newest-first (`decided_at desc`).

        An empty result (no matching rows) is not an error and is returned as
        `[]`, distinct from a `DatabaseError` on connection/query failure.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        try:
            rows = await self._pool.fetch(
                "select * from decisions order by decided_at desc limit $1 offset $2",
                limit,
                offset,
            )
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to read decisions") from exc
        return [Decision.model_validate(dict(row)) for row in rows]

    async def get_latest_equity_snapshot(self) -> EquitySnapshot | None:
        """Fetch the most recent `equity_snapshots` row (by `snapshot_date`).

        `None` means "no snapshot recorded yet" -- a legitimate, non-error
        result, distinct from `DatabaseError` on connection/query failure.
        """
        try:
            row = await self._pool.fetchrow(
                "select * from equity_snapshots order by snapshot_date desc limit 1"
            )
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to read equity_snapshots") from exc
        if row is None:
            return None
        return EquitySnapshot.model_validate(dict(row))

    async def get_open_position_intents(
        self, *, is_paper: bool = True
    ) -> dict[str, Decimal]:
        """Symbol -> quantity the DB's *intent* record says we currently hold.

        This is the DB side of reconciliation (CLAUDE.md invariant #6: the DB is
        the source of truth for intent, Webull for reality). It is derived from
        `trades`, which is the app's own record of positions opened by the
        audited decision -> order -> trade chain: a trade row is `open` from the
        moment its entry order filled until its exit order fills, so the open
        rows ARE the positions we believe we are holding.

        Today this legitimately returns `{}` -- no order path exists yet, so no
        trades have ever been written. That empty result is a real answer ("we
        intend to hold nothing"), NOT a failure, and is distinct from
        :class:`DatabaseError` on connection/query failure (fail closed).

        `is_paper` scopes the query to one environment's trades, so a paper
        broker account is never reconciled against live rows (or vice versa).
        Callers derive it from the client's environment, never hardcode it.

        LIMITATION to revisit when the order path lands: `trades.quantity` is
        constrained positive and the table carries no side column, so this
        models LONG positions only. If short selling is ever added, this query
        must sign the quantity by side -- otherwise a short would reconcile as a
        long of the same size. That is a safety change, not a refactor.
        """
        try:
            rows = await self._pool.fetch(
                """
                select symbol, sum(quantity) as quantity
                from trades
                where status = 'open' and is_paper = $1
                group by symbol
                order by symbol
                """,
                is_paper,
            )
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to read open trades") from exc
        intents: dict[str, Decimal] = {}
        for row in rows:
            quantity = row["quantity"]
            if quantity is None:
                # Cannot happen with a NOT NULL column, but never substitute a
                # fabricated zero for a quantity we could not read.
                raise DatabaseError("open trade returned a null quantity")
            intents[str(row["symbol"])] = Decimal(quantity)
        return intents

    async def is_owner(self, user_id: UUID) -> bool:
        """True if `user_id` is the single allowlisted owner (`app_owner`).

        The API auth layer calls this after verifying a Supabase JWT, to make
        the single-owner authorization decision that RLS would otherwise make
        (this module connects as a role that bypasses RLS -- see module docs).
        A connection/query failure raises :class:`DatabaseError` (fail closed:
        the caller must deny, never allow, when ownership can't be confirmed).
        """
        try:
            return bool(
                await self._pool.fetchval(
                    "select exists(select 1 from app_owner where user_id = $1)",
                    user_id,
                )
            )
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to read app_owner") from exc

    # -- writes ---------------------------------------------------------- #

    async def update_settings(
        self,
        *,
        updated_by: UUID,
        frozen: bool | None = None,
        buy_power_cap: Decimal | None = None,
        max_daily_loss: Decimal | None = None,
        max_per_trade_cap: Decimal | None = None,
        staleness_threshold_seconds: int | None = None,
    ) -> BotSettings:
        """Update one or more fields on the singleton `settings` row.

        `updated_by` is required -- it attributes the change and is what the
        DB's `log_settings_history()` trigger copies into
        `settings_history.changed_by`, so every call through this helper is
        auditable. Every other field is optional; an omitted (`None`) field
        is left unchanged via SQL `coalesce`, never overwritten with `None`.

        This is the write path a (separately owner-gated) dashboard PATCH
        route will call -- provided now, unwired to any route, per the brief.
        Updates existing `settings` columns only; it does not touch schema
        shape (an audit-table-schema change would need owner approval).
        """
        if all(
            value is None
            for value in (
                frozen,
                buy_power_cap,
                max_daily_loss,
                max_per_trade_cap,
                staleness_threshold_seconds,
            )
        ):
            raise ValueError("update_settings requires at least one field to change")
        try:
            row = await self._pool.fetchrow(
                """
                update settings
                set
                    frozen = coalesce($1, frozen),
                    buy_power_cap = coalesce($2, buy_power_cap),
                    max_daily_loss = coalesce($3, max_daily_loss),
                    max_per_trade_cap = coalesce($4, max_per_trade_cap),
                    staleness_threshold_seconds =
                        coalesce($5, staleness_threshold_seconds),
                    updated_at = now(),
                    updated_by = $6
                where id = true
                returning *
                """,
                frozen,
                buy_power_cap,
                max_daily_loss,
                max_per_trade_cap,
                staleness_threshold_seconds,
                updated_by,
            )
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to update settings") from exc
        if row is None:
            raise DatabaseError("settings singleton row is missing")
        return BotSettings.model_validate(dict(row))

    async def insert_equity_snapshot(
        self,
        *,
        account_equity: Decimal,
        cash_balance: Decimal,
        buying_power: Decimal | None,
        is_paper: bool = True,
        snapshot_date: date | None = None,
    ) -> EquitySnapshot:
        """Store one `equity_snapshots` row (the number the dashboard shows).

        `snapshot_date` defaults to today's UTC date; `recorded_at` is left to the
        column default (`now()`). The `spy_close_price` / `spy_benchmark_equity`
        benchmark columns are deliberately left NULL here -- populating the SPY
        buy-and-hold benchmark is a separate concern (strategy-quant), not part of
        reading the broker balance.

        `snapshot_date` is UNIQUE, and the migration's own comment notes a
        same-day snapshot may reasonably be recomputed intraday (worker restart,
        corrected reconciliation). So this UPSERTs on `snapshot_date`: a re-run on
        the same day overwrites that day's row (refreshing `recorded_at`) rather
        than raising a unique-violation -- matching the table's "not append-only,
        left mutable" design, and keeping the job idempotent per UTC day.

        `buying_power` is nullable at this boundary because the broker balance can
        legitimately not carry it; note however that the `equity_snapshots.buying_
        power` column is `NOT NULL`, so passing `None` fails closed as a
        :class:`DatabaseError` (a row is never written with a fabricated value).
        Raises :class:`DatabaseError` on any connection/query failure (fail
        closed) -- callers must not treat that as "no snapshot written".
        """
        snap_date = snapshot_date or datetime.now(UTC).date()
        try:
            row = await self._pool.fetchrow(
                """
                insert into equity_snapshots
                    (snapshot_date, account_equity, cash_balance, buying_power,
                     is_paper)
                values ($1, $2, $3, $4, $5)
                on conflict (snapshot_date) do update set
                    account_equity = excluded.account_equity,
                    cash_balance = excluded.cash_balance,
                    buying_power = excluded.buying_power,
                    is_paper = excluded.is_paper,
                    recorded_at = now()
                returning *
                """,
                snap_date,
                account_equity,
                cash_balance,
                buying_power,
                is_paper,
            )
        except _DB_FAILURE_TYPES as exc:
            raise DatabaseError("failed to insert equity_snapshot") from exc
        if row is None:
            raise DatabaseError("equity_snapshot insert returned no row")
        return EquitySnapshot.model_validate(dict(row))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: open the shared pool at startup, close it at shutdown.

    Wire in via ``FastAPI(lifespan=lifespan)``; routes then reach the DB
    through ``request.app.state.db`` (a :class:`Database` instance). Not yet
    wired into `app/api/main.py` -- that lands with the first route that
    needs it, per the brief (this module only needs to export the piece).
    """
    db = await Database.connect(load_settings().database_url)
    app.state.db = db
    log.info("db.pool_started")
    try:
        yield
    finally:
        await db.close()
        log.info("db.pool_closed")
