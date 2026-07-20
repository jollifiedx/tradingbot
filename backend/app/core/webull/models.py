"""Pydantic request/response models for the Webull client wrapper.

This module imports **no** SDK symbols on purpose: the models are the wrapper's
public contract and must stay validatable without the (heavy, network-touching)
``webull`` package installed. ``client.py`` is the single place that imports the
SDK and is responsible for translating between these models and the SDK's
loosely-typed dict responses.

Conventions honoured here (from CLAUDE.md):
- money is ``Decimal``, never ``float`` — a ``BeforeValidator`` stringifies any
  incoming float so we never inherit binary-float rounding.
- all datetimes are timezone-aware UTC (ruff DTZ rules are on).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def _coerce_money(value: object) -> object:
    """Normalise money input to something ``Decimal`` parses exactly.

    Webull returns money as JSON strings, but we defend against numeric JSON by
    routing floats through ``repr`` so ``0.1`` never becomes
    ``0.1000000000000000055...``. Strings/ints/Decimals pass through untouched
    and let Pydantic raise a clean validation error on garbage.
    """
    if isinstance(value, float):
        return repr(value)
    return value


Money = Annotated[Decimal, BeforeValidator(_coerce_money)]
"""A Decimal money field that refuses to silently absorb float imprecision."""


class BarTimespan(StrEnum):
    """Granularities the wrapper exposes for historical OHLCV bars.

    Values are the exact strings the Webull SDK's ``Timespan`` enum serialises
    to (its ``__str__`` returns the member name), so ``client.py`` forwards
    ``timespan.value`` straight to the SDK without importing it.
    """

    MIN_1 = "M1"
    MIN_5 = "M5"
    MIN_15 = "M15"
    MIN_30 = "M30"
    MIN_60 = "M60"
    MIN_120 = "M120"
    MIN_240 = "M240"
    DAY = "D"
    WEEK = "W"
    MONTH = "M"


class MarketCategory(StrEnum):
    """Security category. US equities/ETFs are all this bot will ever request."""

    US_STOCK = "US_STOCK"
    US_ETF = "US_ETF"


class OrderStatus(StrEnum):
    """Order lifecycle states mirrored from the SDK's ``OrderStatus`` enum.

    ``UNKNOWN`` is a wrapper-only fallback: an unrecognised status from the
    broker is surfaced as ``UNKNOWN`` rather than crashing, so the caller can
    fail closed on an ambiguous status instead of on a parse error.
    """

    SUBMITTED = "SUBMITTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    FILLED = "FILLED"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _LenientResponseModel(BaseModel):
    """Base for response models: ignore unknown broker fields, stay immutable.

    Webull adds fields over time; ``extra="ignore"`` means a new field is not a
    breaking change, while missing *required* fields still raise (→ wrapper maps
    that to ``WebullMalformedResponseError``).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)


class AccountSnapshotRequest(_StrictModel):
    """Inputs for :meth:`WebullClient.get_account_snapshot`.

    The SDK 2.0.14 v2 assets endpoints (``account_v2.get_account_balance`` /
    ``get_account_position``) take only an ``account_id`` — balance is not
    currency-parameterised and positions are returned in a single, un-paged
    response — so this request carries only the account id. Discover the id via
    :meth:`WebullClient.list_accounts` (``/openapi/account/list``).
    """

    account_id: str = Field(min_length=1)


class HistoricalBarsRequest(_StrictModel):
    """Inputs for :meth:`WebullClient.get_historical_bars`."""

    symbol: str = Field(min_length=1)
    timespan: BarTimespan
    count: int = Field(default=200, ge=1, le=1200)
    category: MarketCategory = MarketCategory.US_STOCK


class OrderStatusRequest(_StrictModel):
    """Inputs for :meth:`WebullClient.get_order_status` (read-only lookup)."""

    account_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1, max_length=40)


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #


class AccountInfo(_LenientResponseModel):
    """One brokerage account as returned by ``/openapi/account/list``.

    Used to *discover* the ``account_id`` the read paths key on. Only
    ``account_id`` is required; the rest are best-effort metadata that Webull may
    or may not populate per region. ``account_number`` is a sensitive value — the
    wrapper never logs it; callers that surface it should mask to the last 4.
    """

    account_id: str
    account_number: str | None = None
    account_type: str | None = None
    currency: str | None = None
    status: str | None = None


class AccountBalance(_LenientResponseModel):
    """Cash / buying-power view of the account (the numbers the cap logic reads)."""

    account_id: str
    currency: str
    net_liquidation: Money | None = None
    total_cash: Money | None = None
    buying_power: Money | None = None
    settled_funds: Money | None = None


class Position(_LenientResponseModel):
    """A single open position. Webull is the source of truth for these."""

    instrument_id: str
    symbol: str
    quantity: Money
    cost_price: Money | None = None
    market_value: Money | None = None
    unrealized_pnl: Money | None = None


class AccountSnapshot(_LenientResponseModel):
    """Balance + open positions, captured at :attr:`captured_at` (UTC)."""

    balance: AccountBalance
    positions: tuple[Position, ...]
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def _must_be_utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "captured_at must be timezone-aware (UTC)"
            raise ValueError(msg)
        return value


class OHLCVBar(_LenientResponseModel):
    """One OHLCV candle. ``timestamp`` is the bar's open time, UTC."""

    timestamp: datetime
    open: Money
    high: Money
    low: Money
    close: Money
    volume: Money
    vwap: Money | None = None

    @field_validator("timestamp")
    @classmethod
    def _must_be_utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "bar timestamp must be timezone-aware (UTC)"
            raise ValueError(msg)
        return value


class HistoricalBars(_LenientResponseModel):
    """Ordered (oldest→newest) OHLCV series for one symbol."""

    symbol: str
    timespan: BarTimespan
    bars: tuple[OHLCVBar, ...]


class OrderStatusResult(_LenientResponseModel):
    """Read-only status of a previously-submitted order, keyed by client order id.

    NOTE: this is a *status lookup* only. Placing / modifying / cancelling
    orders is intentionally NOT modelled here — that surface belongs to
    execution-guardian's audited order path, never to this read-only wrapper.
    """

    client_order_id: str
    broker_order_id: str | None = None
    symbol: str | None = None
    status: OrderStatus
    filled_quantity: Money | None = None
    total_quantity: Money | None = None
    avg_fill_price: Money | None = None
