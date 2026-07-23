"""Pluggable strategy interface and the pure value types it trades in.

The rules engine runs one or more **strategy modules** (swing now, an intraday
module later -- owner ruling 2026-07-21, docs/decisions.md). This module defines
the contract every such module implements and the immutable inputs/outputs it
speaks, and it holds **no strategy logic of its own**. It is deliberately light:
standard library only, no pandas, no I/O -- importing the interface must never
pull a heavy numerics stack.

The contract, mirrored on ``safety_gate.py`` / ``reconciliation.py``
--------------------------------------------------------------------
A strategy is a **pure decision function wearing a class**: it takes market data
(OHLCV bars, oldest->newest) plus the current position state as INPUTS and
returns an immutable :class:`StrategyDecision`. Same inputs -> same output.

- no I/O, no network, no DB, no wall-clock read, no logging, no globals, no
  randomness. Everything it needs is passed in.
- it **produces signals only**. It never places an order, never computes a final
  share count against the real buy-power cap, and never touches the safety gate.
  Sizing and execution are the (future) order path's job. The strategy *suggests*
  ``buy``/``sell``/``hold``/``no_trade`` and a conviction; something else decides
  how much, and whether it is safe (that is what ``evaluate_order_safety`` is for).
- it never raises on market data. Empty bars, gaps, too little history, a flat
  market -> a ``NO_TRADE``/``HOLD`` decision with a plain-English rationale, never
  an exception. (A malformed *caller-built* input -- e.g. a short position, which
  this long-only bot cannot hold -- is a programmer error and may raise, exactly
  as ``ReconciliationResult.__post_init__`` does.)

Alignment with the ``decisions`` audit table (NOT a DB write)
-------------------------------------------------------------
:class:`StrategyDecision` is shaped to map cleanly onto a ``decisions`` row
(``symbol``, ``action``, ``rules_fired``, ``conviction``, ``market_data_as_of``)
so the order path can persist it without translation -- see
:meth:`StrategyDecision.as_decision_fields`. But this module writes nothing: the
mapping is a convenience for a later, audited writer. Note one deliberate gap:
``decisions.llm_rationale`` is the *LLM's* field; a strategy's
:attr:`~StrategyDecision.rationale` is a **deterministic** explanation of which
rules fired, and must not be stored as if a model wrote it.

Money is :class:`~decimal.Decimal` throughout -- never float. (Indicator values
such as an SMA or RSI are inherently floating-point and live in the concrete
strategy modules; every *price* and *quantity* that crosses this interface is a
Decimal.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

# One decimal place count is shared with the DB's conviction column
# (numeric(4,3) -> 3 places); conviction is quantized to this so a decision maps
# to `decisions.conviction` without a lossy re-rounding at the DB boundary.
_CONVICTION_QUANTUM = Decimal("0.001")


class StrategyAction(StrEnum):
    """What a strategy recommends. Values match ``decisions.action`` 1:1.

    Deliberately its own enum rather than importing
    :class:`app.core.models.DecisionAction`, so this interface carries no
    dependency on the DB mirror -- but the values are identical and a drift-guard
    test (``tests/test_strategy_base.py``) fails if they ever diverge, the same
    way ``tests/test_models.py`` pins the models against the SQL.

    The four are exhaustive and mutually exclusive for a long-only bot:

    - ``BUY``      -- flat, and the entry conditions are met. Open a position.
    - ``SELL``     -- holding, and an exit condition fired. Close the position.
    - ``HOLD``     -- holding, and no exit fired. Stay in.
    - ``NO_TRADE`` -- flat, and no entry fired (the idle default). Do nothing.

    ``HOLD`` vs ``NO_TRADE`` is the in-position/flat distinction: both mean "place
    no order", but keeping them separate keeps the audit log honest about whether
    the bot was holding something or sitting in cash.
    """

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    NO_TRADE = "no_trade"


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle. Immutable; every price/volume is a :class:`Decimal`.

    ``timestamp`` is the bar's open time and MUST be timezone-aware (UTC, per the
    project's datetime rule); a naive timestamp is a programmer error and is
    rejected at construction. This is the strategy's own input type, kept free of
    any broker dependency -- the backtest harness converts a broker
    ``OHLCVBar`` into one of these at the boundary (see ``backtest.py``).
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Bar.timestamp must be timezone-aware (UTC)")
        # A non-finite (NaN/Inf) or non-positive price is a malformed tick, NOT
        # ordinary market data -- reject it here at the boundary (mirroring
        # PositionState.entry_price's finiteness guard). Otherwise such a value
        # flows in as data and makes the Decimal stop-loss comparison raise
        # `decimal.InvalidOperation` on a held position -- skipping the stop
        # exactly when a bad tick arrives. Failing here instead lets the
        # market-data-stream adapter catch it and halt/stale (fail closed), and
        # keeps `Strategy.evaluate` total on WELL-FORMED market data.
        for name, price in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not price.is_finite() or price <= 0:
                raise ValueError(f"Bar.{name} must be a finite, positive Decimal")
        if not self.volume.is_finite() or self.volume < 0:
            raise ValueError("Bar.volume must be a finite, non-negative Decimal")


@dataclass(frozen=True, slots=True)
class MarketData:
    """The bars a strategy reasons over: one symbol, oldest -> newest.

    ``bars`` is ordered from oldest to newest and may be empty (no data yet) or
    shorter than a strategy needs (insufficient history); both are normal inputs
    a strategy must answer with a decision, not an exception. The sequence is by
    *bar order*, not calendar -- a weekend/holiday gap between two daily bars is
    simply two adjacent entries, which is why gaps do not need special handling.
    """

    symbol: str
    bars: tuple[Bar, ...]

    @property
    def latest(self) -> Bar | None:
        """The most recent bar, or ``None`` when there is no data at all."""
        return self.bars[-1] if self.bars else None


@dataclass(frozen=True, slots=True)
class PositionState:
    """The current holding in one symbol, as the caller knows it. Long-only.

    ``quantity`` is ``0`` when flat and ``> 0`` when holding (this bot never
    shorts -- mirrors ``db.get_open_position_intents``' long-only guard). When
    holding, ``entry_price`` MUST be present: the exit logic needs it to place a
    stop relative to the fill. An inconsistent combination is a programmer error
    (the caller mis-built the state) and is rejected at construction -- it is not
    market data, so raising here does not violate the "never raise on market
    data" contract.
    """

    symbol: str
    quantity: Decimal
    entry_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError(
                "PositionState.quantity must be a finite, non-negative Decimal "
                "(long-only: no short positions)"
            )
        if self.is_open:
            if self.entry_price is None:
                raise ValueError(
                    "an open position must carry an entry_price (needed to place "
                    "the protective stop)"
                )
            if not self.entry_price.is_finite() or self.entry_price <= 0:
                raise ValueError("entry_price must be a finite, positive Decimal")
        elif self.entry_price is not None:
            raise ValueError("a flat position (quantity 0) must not carry an entry_price")

    @property
    def is_open(self) -> bool:
        """True iff we currently hold shares of this symbol."""
        return self.quantity > 0

    @classmethod
    def flat(cls, symbol: str) -> PositionState:
        """The idle state: holding nothing in ``symbol``."""
        return cls(symbol=symbol, quantity=Decimal(0), entry_price=None)


@dataclass(frozen=True, slots=True)
class RuleResult:
    """One rule's evaluation: its name, whether it fired, and a short reason.

    Every rule a strategy checks is recorded -- fired or not -- so the audit log
    can answer "why did (or didn't) it trade?" from the decision alone. The
    collection maps onto ``decisions.rules_fired`` (jsonb); see
    :meth:`StrategyDecision.as_decision_fields`.
    """

    name: str
    fired: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        """jsonb-friendly rendering for ``decisions.rules_fired``."""
        return {"name": self.name, "fired": self.fired, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Immutable output of :meth:`Strategy.evaluate`. A recommendation, not an order.

    Shaped to map onto a ``decisions`` row (see :meth:`as_decision_fields`), but
    this is a value object -- constructing it writes nothing.

    Fields
    ------
    symbol:
        The instrument the decision is about.
    action:
        One of :class:`StrategyAction`.
    conviction:
        Advisory confidence in ``action``, a Decimal in ``[0, 1]`` quantized to
        3 places (matches ``decisions.conviction``). **Advisory only**: it does
        NOT gate the action and does NOT size the order -- a later layer may use
        it to scale or rank, but the strategy trades the same whatever it is.
    rationale:
        A deterministic, human-readable one-liner. NOT an LLM rationale (see the
        module docstring) -- do not persist it into ``decisions.llm_rationale``.
    rules:
        Every rule evaluated this bar, fired or not (audit trail).
    as_of:
        The timestamp of the most recent bar used, or ``None`` if there were no
        bars. Maps to ``decisions.market_data_as_of`` and lets the caller reason
        about data freshness.
    """

    symbol: str
    action: StrategyAction
    conviction: Decimal
    rationale: str
    rules: tuple[RuleResult, ...]
    as_of: datetime | None

    @property
    def fired_rules(self) -> tuple[str, ...]:
        """Names of the rules that fired, in evaluation order."""
        return tuple(r.name for r in self.rules if r.fired)

    def as_decision_fields(self) -> dict[str, object]:
        """Render onto ``decisions`` columns for the (future) audited writer.

        Deliberately omits ``llm_rationale`` (that belongs to the LLM) and every
        DB-managed column (id/timestamps). ``rules_fired`` carries the FULL rule
        set with each rule's ``fired`` flag, not just the ones that fired, so a
        postmortem can see what was checked and rejected too.
        """
        return {
            "symbol": self.symbol,
            "action": self.action.value,
            "conviction": self.conviction,
            "rules_fired": [r.as_dict() for r in self.rules],
            "market_data_as_of": self.as_of,
        }


def quantize_conviction(value: Decimal) -> Decimal:
    """Clamp to ``[0, 1]`` and round to 3 places (the ``decisions`` column scale).

    Pure helper shared by strategy modules so conviction is produced in exactly
    one shape. Clamps first (a strength score can compute slightly outside the
    unit interval), then quantizes half-up.
    """
    clamped = min(Decimal(1), max(Decimal(0), value))
    return clamped.quantize(_CONVICTION_QUANTUM)


class Strategy(ABC):
    """The pluggable interface every strategy module implements.

    An ABC (not a bare Protocol) because a strategy is a small object that
    carries its own immutable config and a stable ``name``, and because a future
    registry of strategies wants a common base to iterate. Subclasses implement
    exactly one behaviour -- :meth:`evaluate` -- and expose two read-only facts:
    :attr:`name` (stable identifier, stored with the decision) and
    :attr:`min_bars` (how much history the strategy needs before it can produce
    anything but ``NO_TRADE``/``HOLD``).

    Implementations MUST honour the purity contract in the module docstring.
    """

    #: Stable, human-readable identifier persisted with each decision.
    name: str = "strategy"

    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Minimum number of bars required to evaluate the full ruleset."""
        raise NotImplementedError

    @abstractmethod
    def evaluate(
        self, *, market_data: MarketData, position: PositionState
    ) -> StrategyDecision:
        """Decide what to do with ``market_data`` given the current ``position``.

        Keyword-only on purpose: in the trading path an argument in the wrong
        position is a bug, so callers must name both inputs. Pure and total --
        returns a :class:`StrategyDecision` for every input, never raises on
        market data.
        """
        raise NotImplementedError
