"""Tests for the backtest harness (app.worker.strategy.backtest).

Deterministic, fixture-only -- no live Webull calls. The harness's contract is
narrow and this suite pins it: it walks bars without look-ahead, always applies
costs, always benchmarks against SPY buy-and-hold, and always surfaces the
"a backtest is not validation" disclaimer so nobody mistakes a green number for
proof the strategy is live-ready.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.worker.strategy.backtest import (
    BACKTEST_DISCLAIMER,
    DEFAULT_COST_MODEL,
    BacktestReport,
    CostModel,
    bars_from_ohlcv,
    generate_signals,
    run_backtest,
)
from app.worker.strategy.base import Bar, StrategyAction
from app.worker.strategy.swing import SwingStrategy

_T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _bars(prices: Sequence[float]) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            timestamp=_T0 + timedelta(days=i),
            open=Decimal(str(p)),
            high=Decimal(str(p)),
            low=Decimal(str(p)),
            close=Decimal(str(p)),
            volume=Decimal(1000),
        )
        for i, p in enumerate(prices)
    )


def _uptrend() -> tuple[Bar, ...]:
    return _bars([100 + 0.5 * i + 5 * math.sin(i / 3.0) for i in range(260)])


def _spy() -> tuple[Bar, ...]:
    return _bars([400 + 0.2 * i for i in range(260)])


# --------------------------------------------------------------------------
# CostModel.
# --------------------------------------------------------------------------


def test_cost_model_rejects_negative_fraction() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CostModel(commission_fraction=Decimal("-0.001"))


def test_round_trip_is_twice_the_per_side_cost() -> None:
    cm = CostModel(
        commission_fraction=Decimal("0.001"),
        spread_fraction=Decimal("0.001"),
        slippage_fraction=Decimal("0.002"),
    )
    # (0.001 + 0.001 + 0.002) per side, doubled
    assert cm.round_trip_fraction == Decimal("0.008")
    assert cm.slippage_component == Decimal("0.003")


# --------------------------------------------------------------------------
# generate_signals -- the no-look-ahead walk.
# --------------------------------------------------------------------------


def test_signals_are_aligned_and_causal() -> None:
    strategy = SwingStrategy()
    bars = _uptrend()
    entries, exits = generate_signals(strategy, bars)
    assert len(entries) == len(bars)
    assert len(exits) == len(bars)
    # a confirmed uptrend opens at least one position over the window
    assert any(entries)
    # no exit is ever recorded on a bar before the first entry (causality)
    first_entry = entries.index(True)
    assert not any(exits[:first_entry])


def test_signals_never_exit_while_flat() -> None:
    # A flat market never enters, so it must never exit either.
    strategy = SwingStrategy()
    entries, exits = generate_signals(strategy, _bars([100.0] * 260))
    assert not any(entries)
    assert not any(exits)


# --------------------------------------------------------------------------
# run_backtest.
# --------------------------------------------------------------------------


def test_backtest_requires_two_bars_each_side() -> None:
    strategy = SwingStrategy()
    with pytest.raises(ValueError, match="two bars"):
        run_backtest(strategy=strategy, bars=_bars([100.0]), spy_bars=_spy())
    with pytest.raises(ValueError, match="two SPY bars"):
        run_backtest(strategy=strategy, bars=_uptrend(), spy_bars=_bars([400.0]))


def test_backtest_produces_a_report_with_costs_and_spy_benchmark() -> None:
    report = run_backtest(strategy=SwingStrategy(), bars=_uptrend(), spy_bars=_spy())
    assert isinstance(report, BacktestReport)
    assert report.strategy_name == "swing_trend_v1"
    assert report.bars_count == 260
    # the SPY benchmark is present and the verdict is a real bool
    assert isinstance(report.spy_return, Decimal)
    assert isinstance(report.beats_spy_after_costs, bool)
    # costs are the ones we passed (defaults here) -- always applied, never zeroed
    assert report.cost_model is DEFAULT_COST_MODEL
    assert report.beats_spy_after_costs == (report.total_return > report.spy_return)


def test_backtest_is_deterministic() -> None:
    first = run_backtest(strategy=SwingStrategy(), bars=_uptrend(), spy_bars=_spy())
    second = run_backtest(strategy=SwingStrategy(), bars=_uptrend(), spy_bars=_spy())
    assert first == second


def test_costs_reduce_the_reported_return() -> None:
    # Same bars, only costs differ: heavier costs must not produce a HIGHER return.
    free = CostModel(
        commission_fraction=Decimal(0),
        spread_fraction=Decimal(0),
        slippage_fraction=Decimal(0),
    )
    heavy = CostModel(
        commission_fraction=Decimal("0.005"),
        spread_fraction=Decimal("0.005"),
        slippage_fraction=Decimal("0.005"),
    )
    bars, spy = _uptrend(), _spy()
    r_free = run_backtest(strategy=SwingStrategy(), bars=bars, spy_bars=spy, cost_model=free)
    r_heavy = run_backtest(strategy=SwingStrategy(), bars=bars, spy_bars=spy, cost_model=heavy)
    assert r_heavy.total_return <= r_free.total_return


def test_report_summary_leads_with_the_not_validation_disclaimer() -> None:
    report = run_backtest(strategy=SwingStrategy(), bars=_uptrend(), spy_bars=_spy())
    summary = report.summary()
    assert report.disclaimer == BACKTEST_DISCLAIMER
    assert "NOT validation" in summary
    assert "FORWARD paper" in summary
    # the disclaimer is first, so a skimmer cannot miss it
    assert summary.lstrip().startswith("!!")


def test_flat_market_makes_no_trades() -> None:
    report = run_backtest(
        strategy=SwingStrategy(), bars=_bars([100.0] * 260), spy_bars=_spy()
    )
    assert report.trades_count == 0
    assert report.win_rate is None
    assert report.profit_factor is None


# --------------------------------------------------------------------------
# bars_from_ohlcv adapter.
# --------------------------------------------------------------------------


def test_bars_from_ohlcv_coerces_to_decimal() -> None:
    rows = [(_T0, 10.5, 11.0, 10.0, 10.75, 1234)]
    bars = bars_from_ohlcv(rows)
    assert bars[0].close == Decimal("10.75")
    assert isinstance(bars[0].close, Decimal)


def test_bars_from_ohlcv_rejects_non_datetime_timestamp() -> None:
    with pytest.raises(TypeError, match="datetime"):
        bars_from_ohlcv([("2025-01-01", 1, 1, 1, 1, 1)])


def test_generate_signals_open_close_are_consistent() -> None:
    # Every exit must be preceded by an entry (a round trip), never a naked sell.
    strategy = SwingStrategy()
    entries, exits = generate_signals(strategy, _uptrend())
    open_count = 0
    for entered, exited in zip(entries, exits, strict=True):
        if entered:
            open_count += 1
        if exited:
            assert open_count > 0, "an exit fired with no open position"
            open_count -= 1
    # StrategyAction is exercised end-to-end through the walk
    assert StrategyAction.BUY.value == "buy"
