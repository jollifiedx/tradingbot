"""Tests for OBSERVE MODE (`app.worker.observe`).

Observe mode is the safe precursor to the order path: it reads real daily bars,
runs the strategy, and APPENDS the decision to `decisions` -- it must place no
order and be structurally unable to. These tests drive the orchestration with a
fake read-only bar source and a recording decision sink (no network, no DB), and
assert the SAFE behaviour: exactly one recorded row per usable symbol, a flat
position every time, a per-symbol failure isolated, an unusable series skipped
rather than guessed, and no order surface anywhere in reach.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from structlog.testing import capture_logs

from app.core.db import Database, DatabaseError
from app.core.models import Decision, DecisionAction
from app.core.webull import (
    BarTimespan,
    HistoricalBars,
    HistoricalBarsRequest,
    MarketCategory,
    OHLCVBar,
    WebullClient,
)
from app.worker.observe import (
    WATCHLIST,
    WatchlistEntry,
    observe_watchlist,
)
from app.worker.strategy.backtest import bars_from_ohlcv
from app.worker.strategy.base import (
    MarketData,
    PositionState,
    RuleResult,
    Strategy,
    StrategyAction,
    StrategyDecision,
)
from app.worker.strategy.swing import SwingStrategy

T0 = datetime(2026, 7, 21, 21, 0, tzinfo=UTC)


def _cat(name: str) -> MarketCategory:
    return MarketCategory(name)


# --------------------------------------------------------------------------
# Fakes: a read-only bar source and a recording decision sink.
# --------------------------------------------------------------------------


def _bar(ts: datetime, close: float) -> OHLCVBar:
    """A well-formed OHLCV bar at ``close`` (flat OHLC around it, unit volume)."""
    c = Decimal(str(close))
    return OHLCVBar(
        timestamp=ts, open=c, high=c, low=c, close=c, volume=Decimal("1000")
    )


def _series(count: int, *, start: float = 100.0, step: float = 0.0) -> HistoricalBars:
    """A HistoricalBars of ``count`` daily bars, oldest->newest."""
    base = T0 - timedelta(days=count)
    bars = tuple(
        _bar(base + timedelta(days=i), start + step * i) for i in range(count)
    )
    return HistoricalBars(symbol="X", timespan=BarTimespan.DAY, bars=bars)


class FakeBarSource:
    """Read-only market data under test control. Structurally: no order method.

    Returns a preset :class:`HistoricalBars` per symbol, or raises a preset
    exception per symbol. There is deliberately nothing here that could place an
    order -- it satisfies :class:`app.worker.observe.BarSource` and no more.
    """

    def __init__(
        self,
        by_symbol: dict[str, HistoricalBars] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._by_symbol = by_symbol or {}
        self._errors = errors or {}
        self.requests: list[HistoricalBarsRequest] = []

    def get_historical_bars(self, request: HistoricalBarsRequest) -> HistoricalBars:
        self.requests.append(request)
        if request.symbol in self._errors:
            raise self._errors[request.symbol]
        return self._by_symbol.get(
            request.symbol,
            HistoricalBars(symbol=request.symbol, timespan=BarTimespan.DAY, bars=()),
        )


class RecordingSink:
    """Records every `insert_decision` and returns a real :class:`Decision`.

    Optionally raises on configured symbols, to prove a DB failure for one symbol
    does not abort the rest.
    """

    def __init__(self, fail_symbols: set[str] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail = fail_symbols or set()

    async def insert_decision(
        self,
        *,
        symbol: str,
        action: str,
        conviction: Decimal | None,
        rules_fired: list[object] | dict[str, object],
        market_data_as_of: datetime | None,
    ) -> Decision:
        if symbol in self._fail:
            raise DatabaseError("failed to insert decision")
        fields = {
            "symbol": symbol,
            "action": action,
            "conviction": conviction,
            "rules_fired": rules_fired,
            "market_data_as_of": market_data_as_of,
        }
        self.calls.append(fields)
        now = datetime.now(UTC)
        return Decision(
            id=uuid4(),
            decided_at=now,
            symbol=symbol,
            action=DecisionAction(action),
            rules_fired=list(rules_fired) if isinstance(rules_fired, list) else rules_fired,
            conviction=conviction,
            market_data_as_of=market_data_as_of,
            created_at=now,
        )


class StubStrategy(Strategy):
    """Returns a fixed decision and captures every position it was evaluated with."""

    name = "stub_v1"

    def __init__(
        self, decision_factory: Callable[[MarketData], StrategyDecision]
    ) -> None:
        self._make = decision_factory
        self.positions: list[PositionState] = []

    @property
    def min_bars(self) -> int:
        return 1

    def evaluate(
        self, *, market_data: MarketData, position: PositionState
    ) -> StrategyDecision:
        self.positions.append(position)
        return self._make(market_data)


def _no_trade(market_data: MarketData) -> StrategyDecision:
    return StrategyDecision(
        symbol=market_data.symbol,
        action=StrategyAction.NO_TRADE,
        conviction=Decimal("0.000"),
        rationale="stub no-trade",
        rules=(RuleResult(name="stub", fired=False, detail="stub"),),
        as_of=market_data.latest.timestamp if market_data.latest else None,
    )


# --------------------------------------------------------------------------
# The watchlist itself.
# --------------------------------------------------------------------------


def test_watchlist_is_a_small_fixed_manual_list() -> None:
    symbols = [e.symbol for e in WATCHLIST]
    assert symbols == ["AAPL", "MSFT", "SPY"]
    # SPY is an ETF; the two single names are common stock -- category is explicit.
    by_symbol = {e.symbol: e.category.value for e in WATCHLIST}
    assert by_symbol["SPY"] == "US_ETF"
    assert by_symbol["AAPL"] == "US_STOCK"


# --------------------------------------------------------------------------
# One recorded decision per usable symbol; flat position; exact fields.
# --------------------------------------------------------------------------


async def test_records_one_decision_per_symbol() -> None:
    watchlist = (
        WatchlistEntry("AAA", _cat("US_STOCK")),
        WatchlistEntry("BBB", _cat("US_STOCK")),
        WatchlistEntry("CCC", _cat("US_ETF")),
    )
    source = FakeBarSource(
        {sym.symbol: _series(3) for sym in watchlist}
    )
    sink = RecordingSink()
    strategy = StubStrategy(_no_trade)

    recorded = await observe_watchlist(
        client=source, db=sink, strategy=strategy, watchlist=watchlist
    )

    assert [c["symbol"] for c in sink.calls] == ["AAA", "BBB", "CCC"]
    assert all(c["action"] == "no_trade" for c in sink.calls)
    assert len(recorded) == 3
    # Every evaluation used a FLAT position -- the bot holds nothing in observe.
    assert len(strategy.positions) == 3
    assert all(not p.is_open and p.quantity == Decimal(0) for p in strategy.positions)


async def test_fetches_daily_bars_with_symbol_category_and_count() -> None:
    entry = WatchlistEntry("AAA", _cat("US_ETF"))
    source = FakeBarSource({"AAA": _series(3)})
    await observe_watchlist(
        client=source, db=RecordingSink(), strategy=StubStrategy(_no_trade),
        watchlist=(entry,),
    )
    assert len(source.requests) == 1
    req = source.requests[0]
    assert req.symbol == "AAA"
    assert req.timespan is BarTimespan.DAY
    assert req.category.value == "US_ETF"
    assert req.count >= SwingStrategy().min_bars  # enough history for a real signal


async def test_maps_exact_decision_fields() -> None:
    as_of = T0
    rules = (
        RuleResult(name="trend_up", fired=True, detail="above trend"),
        RuleResult(name="momentum_up", fired=True, detail="fast>slow"),
    )

    def _buy(market_data: MarketData) -> StrategyDecision:
        return StrategyDecision(
            symbol=market_data.symbol,
            action=StrategyAction.BUY,
            conviction=Decimal("0.512"),
            rationale="stub buy",
            rules=rules,
            as_of=as_of,
        )

    sink = RecordingSink()
    await observe_watchlist(
        client=FakeBarSource({"AAA": _series(3)}),
        db=sink,
        strategy=StubStrategy(_buy),
        watchlist=(WatchlistEntry("AAA", _cat("US_STOCK")),),
    )

    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["action"] == "buy"
    assert call["conviction"] == Decimal("0.512")
    assert call["market_data_as_of"] == as_of
    # rules_fired carries the FULL ruleset (fired and not), as as_decision_fields does.
    assert call["rules_fired"] == [r.as_dict() for r in rules]


# --------------------------------------------------------------------------
# Isolation: one symbol failing must not abort the others.
# --------------------------------------------------------------------------


async def test_a_fetch_failure_is_isolated_per_symbol() -> None:
    watchlist = (
        WatchlistEntry("AAA", _cat("US_STOCK")),
        WatchlistEntry("BBB", _cat("US_STOCK")),
        WatchlistEntry("CCC", _cat("US_STOCK")),
    )
    source = FakeBarSource(
        by_symbol={"AAA": _series(3), "CCC": _series(3)},
        errors={"BBB": RuntimeError("broker API blew up")},
    )
    sink = RecordingSink()

    with capture_logs() as logs:
        recorded = await observe_watchlist(
            client=source, db=sink, strategy=StubStrategy(_no_trade),
            watchlist=watchlist,
        )

    assert [c["symbol"] for c in sink.calls] == ["AAA", "CCC"]
    assert len(recorded) == 2
    failed = [entry for entry in logs if entry["event"] == "observe.symbol_failed"]
    assert [entry["symbol"] for entry in failed] == ["BBB"]


async def test_a_db_insert_failure_is_isolated_per_symbol() -> None:
    watchlist = (
        WatchlistEntry("AAA", _cat("US_STOCK")),
        WatchlistEntry("BBB", _cat("US_STOCK")),
    )
    source = FakeBarSource({"AAA": _series(3), "BBB": _series(3)})
    sink = RecordingSink(fail_symbols={"AAA"})

    recorded = await observe_watchlist(
        client=source, db=sink, strategy=StubStrategy(_no_trade), watchlist=watchlist
    )

    # AAA's insert raised; BBB still recorded.
    assert [c["symbol"] for c in sink.calls] == ["BBB"]
    assert [r.symbol for r in recorded] == ["BBB"]


# --------------------------------------------------------------------------
# Unusable bars are SKIPPED, not guessed.
# --------------------------------------------------------------------------


async def test_empty_bars_are_skipped_not_recorded() -> None:
    watchlist = (
        WatchlistEntry("AAA", _cat("US_STOCK")),
        WatchlistEntry("BBB", _cat("US_STOCK")),
    )
    source = FakeBarSource(
        {
            "AAA": HistoricalBars(symbol="AAA", timespan=BarTimespan.DAY, bars=()),
            "BBB": _series(3),
        }
    )
    sink = RecordingSink()

    with capture_logs() as logs:
        recorded = await observe_watchlist(
            client=source, db=sink, strategy=StubStrategy(_no_trade),
            watchlist=watchlist,
        )

    assert [c["symbol"] for c in sink.calls] == ["BBB"]
    assert len(recorded) == 1
    skipped = [entry for entry in logs if entry["event"] == "observe.symbol_skipped"]
    assert [entry["symbol"] for entry in skipped] == ["AAA"]


async def test_a_malformed_bar_skips_the_symbol_fail_closed() -> None:
    """A non-positive price bar makes the strategy Bar reject at construction.

    That rejection (the same NaN/boundary defence the strategy relies on) is
    caught per-symbol and the symbol is SKIPPED -- a bad tick never becomes a
    recorded decision.
    """
    good = _series(3)
    # A zero-price bar: valid as a broker OHLCVBar, rejected as a strategy Bar.
    bad_bar = OHLCVBar(
        timestamp=T0,
        open=Decimal("0"),
        high=Decimal("0"),
        low=Decimal("0"),
        close=Decimal("0"),
        volume=Decimal("1"),
    )
    bad = HistoricalBars(
        symbol="AAA", timespan=BarTimespan.DAY, bars=(*good.bars, bad_bar)
    )
    source = FakeBarSource({"AAA": bad, "BBB": good})
    sink = RecordingSink()

    with capture_logs() as logs:
        await observe_watchlist(
            client=source, db=sink, strategy=StubStrategy(_no_trade),
            watchlist=(
                WatchlistEntry("AAA", _cat("US_STOCK")),
                WatchlistEntry("BBB", _cat("US_STOCK")),
            ),
        )

    assert [c["symbol"] for c in sink.calls] == ["BBB"]
    failed = [entry for entry in logs if entry["event"] == "observe.symbol_failed"]
    assert [entry["symbol"] for entry in failed] == ["AAA"]


# --------------------------------------------------------------------------
# End-to-end with the REAL swing strategy: conversion + evaluate wiring.
# --------------------------------------------------------------------------


async def test_real_swing_strategy_end_to_end() -> None:
    """The real SwingStrategy runs over real converted bars and records its call.

    Asserts the recorded action equals SwingStrategy.evaluate on the identical
    bars (computed independently) -- proving the broker->strategy conversion and
    the flat-position evaluation are wired correctly, without pinning a specific
    action (that is the strategy suite's job).
    """
    bars = _series(220, start=50.0, step=0.10)  # a gentle, long uptrend
    source = FakeBarSource({"AAA": bars})
    sink = RecordingSink()

    recorded = await observe_watchlist(
        client=source, db=sink, strategy=SwingStrategy(),
        watchlist=(WatchlistEntry("AAA", _cat("US_STOCK")),),
    )

    # Independently compute what the strategy should have said on these bars.
    strat_bars = bars_from_ohlcv(
        [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in bars.bars]
    )
    expected = SwingStrategy().evaluate(
        market_data=MarketData(symbol="AAA", bars=strat_bars),
        position=PositionState.flat("AAA"),
    )

    assert len(recorded) == 1
    assert sink.calls[0]["action"] == expected.action.value
    assert sink.calls[0]["market_data_as_of"] == bars.bars[-1].timestamp


# --------------------------------------------------------------------------
# Structural: nothing in observe's reach can place an order.
# --------------------------------------------------------------------------


def test_read_only_client_exposes_no_order_surface() -> None:
    for method in ("place_order", "replace_order", "cancel_order", "submit_order"):
        assert not hasattr(WebullClient, method), (
            f"WebullClient must expose no order-mutating method; found {method}"
        )


def test_decision_sink_exposes_no_order_surface() -> None:
    # The write the sink offers observe is an append-only insert -- not an order.
    for method in ("place_order", "cancel_order", "submit_order"):
        assert not hasattr(Database, method)
    assert hasattr(Database, "insert_decision")
