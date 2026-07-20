"""Pydantic v2 models mirroring the live TradingBot Supabase schema, 1:1.

Source of truth is `supabase/migrations/` (applied to the Supabase dev project
2026-07-20; 15/15 runtime smoke tests passed) -- this module must never invent a
constraint, enum value, or default that isn't literally present in the SQL. If the
SQL changes, this file must be updated in the same review; it is a mirror, not an
independent design.

Naming note: this module's `BotSettings` is the DB row of live risk/control
parameters (`settings` table -- frozen flag, caps, staleness threshold). It is
deliberately NOT named `Settings` to avoid confusion with
`app.core.config.Settings`, the pydantic-settings class that loads process
environment variables (Webull/Anthropic/Supabase credentials, etc). Those two
classes have nothing to do with each other: one is env-var config loaded once at
process start, the other is a mutable database row the worker re-reads before
every order.

Conventions applied throughout:
- No ORM. Plain Pydantic v2 models with `from_attributes=True`, for future
  asyncpg/SQLAlchemy row loading (`Model.model_validate(row)`).
- Every `numeric(p, s)` SQL column is `Decimal` with `max_digits=p,
  decimal_places=s`, plus any CHECK constraint the migration literally declares
  (`ge=0`, `gt=0`, range, etc). Where a column has NO check constraint in SQL
  (e.g. `settings_history` amounts, `trades.entry_price`), none is added here --
  fidelity to the SQL shape means not inventing constraints either.
- Every `timestamptz` column is `pydantic.AwareDatetime`, which rejects naive
  datetimes at validation time (satisfies the "naive datetime is a defect" rule
  and the ruff DTZ lint intent without a hand-rolled validator). Plain SQL `date`
  columns (`equity_snapshots.snapshot_date`) are `datetime.date`, not datetime --
  there is no tz concept for a date.
- Every SQL CHECK-constrained text column with a fixed value set becomes a
  `StrEnum`, with members copied verbatim from the migration's `check (... in
  (...))` list -- see `backend/tests/test_models.py`, which parses the migration
  SQL directly and fails the suite on any drift between the two.
- `uuid` columns are `uuid.UUID`.
- Models for tables where the DB (via trigger or, for app_owner/llm_calls, by
  design/convention with no legitimate app-level UPDATE path) never permits app
  code to mutate an existing row are `frozen=True`: `Decision`, `Order`, `Trade`
  (explicitly append-only or single-guarded-transition per CLAUDE.md invariant
  #5), `SettingsHistory` and `AppOwner` (trigger-enforced or out-of-band-only
  writes -- see docstrings below), and `OrderCurrent` (a read-only view; nothing
  ever writes through it). Tables the DB itself allows to be updated by app code
  (`BotSettings`, `Thesis`, `EquitySnapshot`) are left mutable. Freezing a model
  is an application-layer safety net only -- it changes no SQL shape and is
  reversible without a migration.
- `theses.embedding` (pgvector) is typed `list[float] | None` at this layer.
  pgvector's on-the-wire representation (and any dimension validation against the
  `vector(1024)` column) is a concern of the DB access layer, not this mirror --
  keeping it a plain list avoids coupling this module to a pgvector Python driver
  choice that hasn't been made yet.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Reusable numeric type aliases (numeric(p, s) SQL columns -> Decimal).
# Named for precision/scale/constraint so field declarations stay readable.
# --------------------------------------------------------------------------

# numeric(14, 2), check (... >= 0) -- settings caps, i.e. money amounts that can
# never be negative.
Money14_2NonNeg = Annotated[Decimal, Field(max_digits=14, decimal_places=2, ge=0)]

# numeric(14, 2), no check constraint -- settings_history snapshot columns (the
# constraint lives only on `settings`, not on its append-only history copy).
Money14_2 = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]

# numeric(16, 2), no check constraint -- equity_snapshots amounts.
Money16_2 = Annotated[Decimal, Field(max_digits=16, decimal_places=2)]

# numeric(14, 4), no check constraint -- prices (limit/stop/fill/entry/exit),
# signed (slippage).
Price14_4 = Annotated[Decimal, Field(max_digits=14, decimal_places=4)]

# numeric(14, 4), check (... >= 0) -- trades.fees.
Price14_4NonNeg = Annotated[Decimal, Field(max_digits=14, decimal_places=4, ge=0)]

# numeric(18, 6), check (... > 0) -- order/trade share quantities.
Quantity18_6Positive = Annotated[Decimal, Field(max_digits=18, decimal_places=6, gt=0)]

# numeric(18, 6), check (... >= 0) -- orders.filled_quantity.
Quantity18_6NonNeg = Annotated[Decimal, Field(max_digits=18, decimal_places=6, ge=0)]

# numeric(4, 3), check (0 <= ... <= 1) -- conviction scores.
Conviction4_3 = Annotated[Decimal, Field(max_digits=4, decimal_places=3, ge=0, le=1)]

# numeric(10, 6), check (... >= 0) -- llm_calls.cost_usd.
CostUsd10_6 = Annotated[Decimal, Field(max_digits=10, decimal_places=6, ge=0)]

# The `id boolean primary key default true, check (id)` singleton pattern used by
# app_owner and settings: the column can only ever hold the literal value True.
SingletonId = Literal[True]


# --------------------------------------------------------------------------
# Enums -- members copied verbatim from each migration's CHECK (... in (...))
# list. backend/tests/test_models.py asserts these against the SQL directly.
# --------------------------------------------------------------------------


class DecisionAction(StrEnum):
    """decisions.action -- 20260719000006_decisions.sql: decisions_action_valid."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    NO_TRADE = "no_trade"


class OrderSide(StrEnum):
    """orders.side -- 20260719000007_orders.sql: orders_side_valid."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """orders.order_type -- 20260719000007_orders.sql: orders_order_type_valid."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    """orders.status -- 20260719000007_orders.sql: orders_status_valid."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TradeStatus(StrEnum):
    """trades.status -- 20260719000008_trades.sql: trades_status_valid."""

    OPEN = "open"
    CLOSED = "closed"


class LlmCallPurpose(StrEnum):
    """llm_calls.purpose -- 20260719000011_llm_calls.sql: llm_calls_purpose_valid."""

    NIGHTLY_RESEARCH = "nightly_research"
    INTRADAY_SUMMARY = "intraday_summary"
    CLASSIFICATION = "classification"
    OTHER = "other"


# --------------------------------------------------------------------------
# Models, in migration/dependency order.
# --------------------------------------------------------------------------


class AppOwner(BaseModel):
    """Mirrors `app_owner` (20260719000002_app_owner.sql).

    Singleton allowlist of the one legitimate TradingBot owner. Provisioned once,
    out of band, by a human via the Supabase SQL editor or service_role -- no
    application code path ever writes this table (see the migration's own
    comment: "provisioned once ... out of band"). Frozen here to reflect that
    reality at the application layer, even though the DB itself has no trigger
    forbidding an UPDATE/DELETE on this table.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: SingletonId = True
    user_id: UUID
    created_at: AwareDatetime


class BotSettings(BaseModel):
    """Mirrors `settings` (20260719000004_settings.sql).

    The one intentionally mutable table: the owner's entire control surface
    (freeze/unfreeze, buy-power cap, daily-loss cap, per-trade cap, staleness
    threshold) is "mutate this row". Named `BotSettings` rather than `Settings`
    to avoid collision with `app.core.config.Settings` (process env-var config)
    -- see module docstring. Born frozen=True with all caps at 0.00 in the seed
    migration; the worker must treat an unreadable row the same way (fail
    closed, CLAUDE.md invariant #2/#3).
    """

    model_config = ConfigDict(from_attributes=True)

    id: SingletonId = True
    frozen: bool
    buy_power_cap: Money14_2NonNeg
    max_daily_loss: Money14_2NonNeg
    max_per_trade_cap: Money14_2NonNeg
    staleness_threshold_seconds: Annotated[int, Field(gt=0)]
    updated_at: AwareDatetime
    updated_by: UUID | None = None


class SettingsHistory(BaseModel):
    """Mirrors `settings_history` (20260719000004_settings.sql).

    Append-only snapshot of every `settings` row state, written exclusively by
    the `log_settings_history()` SECURITY DEFINER trigger -- no application code
    INSERTs here directly, and the table has `reject_update_or_delete()` triggers
    on both UPDATE and DELETE, identical in enforcement to `decisions`/`orders`.
    Frozen accordingly. Note: unlike `settings`, this table's numeric columns
    carry no CHECK constraints in SQL (the constraints live only on the live
    `settings` row) -- so no `ge=0`/`gt=0` is applied here, by design.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    changed_at: AwareDatetime
    changed_by: UUID | None = None
    frozen: bool
    buy_power_cap: Money14_2
    max_daily_loss: Money14_2
    max_per_trade_cap: Money14_2
    staleness_threshold_seconds: int


class Thesis(BaseModel):
    """Mirrors `theses` (20260719000005_theses.sql, FK completed in
    20260719000009_theses_outcome_fk.sql).

    Not append-only: `outcome_trade_id`, `realized_pnl`, `outcome_recorded_at`
    are legitimately filled in (NULL -> value) once a resulting trade closes.
    Left mutable to match that DB reality.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: AwareDatetime
    symbol: str
    thesis: str
    conviction: Conviction4_3
    embedding: list[float] | None = None
    model: str
    outcome_trade_id: UUID | None = None
    realized_pnl: Money14_2 | None = None
    outcome_recorded_at: AwareDatetime | None = None


class Decision(BaseModel):
    """Mirrors `decisions` (20260719000006_decisions.sql).

    Append-only per CLAUDE.md invariant #5: `reject_update_or_delete()` triggers
    on UPDATE and DELETE make mutation impossible for every role, including
    service_role. Frozen here so application code cannot even attempt to mutate
    an in-memory instance; corrections are new rows, never edits.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    decided_at: AwareDatetime
    symbol: str
    action: DecisionAction
    rules_fired: list[Any] | dict[str, Any] = Field(default_factory=list)
    llm_rationale: str | None = None
    thesis_id: UUID | None = None
    conviction: Conviction4_3 | None = None
    market_data_as_of: AwareDatetime | None = None
    settings_snapshot: dict[str, Any] | None = None
    created_at: AwareDatetime


class Order(BaseModel):
    """Mirrors `orders` (20260719000007_orders.sql).

    Append-only per CLAUDE.md invariant #5, same trigger enforcement as
    `decisions`. A status transition (e.g. submitted -> filled) is a NEW row
    referencing the prior one via `previous_order_id`, never a mutation of the
    original -- frozen accordingly. `client_order_id` is the idempotency key
    written BEFORE broker submission (CLAUDE.md invariant #4).
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    client_order_id: str
    decision_id: UUID
    previous_order_id: UUID | None = None
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    quantity: Quantity18_6Positive
    limit_price: Price14_4 | None = None
    stop_price: Price14_4 | None = None
    filled_quantity: Quantity18_6NonNeg = Decimal("0")
    average_fill_price: Price14_4 | None = None
    is_paper: bool = True
    submitted_at: AwareDatetime
    broker_acknowledged_at: AwareDatetime | None = None
    terminal_at: AwareDatetime | None = None
    rejection_reason: str | None = None
    created_at: AwareDatetime


class Trade(BaseModel):
    """Mirrors `trades` (20260719000008_trades.sql).

    Not append-only in the trigger sense `decisions`/`orders` are: the DB permits
    exactly one guarded UPDATE (open -> closed, via `guard_trade_close()`),
    rejecting any other change including a second edit to an already-closed row.
    Frozen here per the brief: application code never mutates a `Trade` Python
    instance in place either -- closing a trade means constructing the
    post-close state (new dict/model) and issuing the UPDATE, not `trade.status
    = "closed"` on a live object.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    symbol: str
    entry_order_id: UUID
    exit_order_id: UUID | None = None
    quantity: Quantity18_6Positive
    entry_price: Price14_4
    exit_price: Price14_4 | None = None
    entry_at: AwareDatetime
    exit_at: AwareDatetime | None = None
    fees: Price14_4NonNeg = Decimal("0")
    slippage: Price14_4 | None = None
    realized_pnl: Money14_2 | None = None
    is_paper: bool = True
    status: TradeStatus = TradeStatus.OPEN
    created_at: AwareDatetime


class EquitySnapshot(BaseModel):
    """Mirrors `equity_snapshots` (20260719000010_equity_snapshots.sql).

    Drives the equity curve and the SPY buy-and-hold benchmark comparison
    (CLAUDE.md success criterion). Not append-only -- the migration's own
    comment notes a same-day snapshot may reasonably be recomputed intraday
    (worker restart, corrected reconciliation) -- left mutable.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    snapshot_date: date
    account_equity: Money16_2
    cash_balance: Money16_2
    buying_power: Money16_2
    spy_close_price: Price14_4 | None = None
    spy_benchmark_equity: Money16_2 | None = None
    is_paper: bool = True
    recorded_at: AwareDatetime


class LlmCall(BaseModel):
    """Mirrors `llm_calls` (20260719000011_llm_calls.sql).

    Cost/usage ledger for every Anthropic API call. Not part of the trading
    audit chain and carries no trigger-enforced immutability in SQL, but the
    worker's usage is write-once (one row per call, no lifecycle transitions
    like `orders` has) -- frozen here to reflect that intended usage. If a
    future need arises to correct a logged cost, that should be a new
    superseding row, matching the append-only convention used everywhere else
    in this schema, not a schema change to add mutability.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    called_at: AwareDatetime
    model: str
    purpose: LlmCallPurpose
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)] = 0
    cost_usd: CostUsd10_6
    used_batch_api: bool = False
    thesis_id: UUID | None = None
    decision_id: UUID | None = None


class OrderCurrent(Order):
    """Mirrors the `orders_current` view (defined in
    20260719000007_orders.sql): one row per order lifecycle -- the terminal
    (latest) `orders` row in each `previous_order_id` chain, plus the chain's
    root identifiers. Inherits every `Order` column (`select o.*`) and adds the
    two view-only columns. Frozen like `Order`: this is a read-only view, and
    the class is never used as an INSERT/UPDATE target.
    """

    chain_root_id: UUID
    chain_root_client_order_id: str
