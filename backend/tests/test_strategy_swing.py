"""Exhaustive tests for the swing strategy (app.worker.strategy.swing).

The strategy is a pure, total signal generator: every input -- including empty
bars, too little history, a flat market -- must yield a StrategyDecision and
never raise on market data. Rule fixtures are built from deterministic price
series whose resulting indicator relationships were verified empirically; each
test asserts the exact action and which rules fired, not merely "no exception".

Money/risk note: the one true risk parameter (stop_loss_pct) is exercised on
price alone, so the stop is proven to protect capital even with no indicator
history at all.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.worker.strategy.base import (
    Bar,
    MarketData,
    PositionState,
    StrategyAction,
    StrategyDecision,
)
from app.worker.strategy.swing import DEFAULT_SWING_CONFIG, SwingConfig, SwingStrategy

_T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _bars(prices: Sequence[float]) -> tuple[Bar, ...]:
    """Build daily bars from a close-price series (o=h=l=c for simplicity)."""
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
    """A drift-plus-oscillation uptrend: rising overall, pullbacks keep RSI < 75."""
    return _bars([100 + 0.5 * i + 5 * math.sin(i / 3.0) for i in range(260)])


def _rise_then_fall() -> tuple[Bar, ...]:
    """A long rise then a recent decline -> fast SMA crosses below slow SMA."""
    rise = [100 + 0.6 * i for i in range(220)]
    fall = [rise[-1] - 0.8 * i for i in range(1, 36)]
    return _bars(rise + fall)


# --------------------------------------------------------------------------
# Value-type guards (long-only; UTC-aware bars).
# --------------------------------------------------------------------------


def test_bar_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(
            timestamp=datetime(2025, 1, 1),  # noqa: DTZ001 -- the case under test
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
        )


def test_position_state_is_long_only() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        PositionState(symbol="X", quantity=Decimal(-1))


def test_open_position_requires_entry_price() -> None:
    with pytest.raises(ValueError, match="entry_price"):
        PositionState(symbol="X", quantity=Decimal(5))


def test_flat_position_must_not_carry_entry_price() -> None:
    with pytest.raises(ValueError, match="must not carry an entry_price"):
        PositionState(symbol="X", quantity=Decimal(0), entry_price=Decimal(10))


# --------------------------------------------------------------------------
# Never-raise / idle-default behaviour.
# --------------------------------------------------------------------------


def test_no_bars_flat_is_no_trade() -> None:
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", ()), position=PositionState.flat("X")
    )
    assert decision.action is StrategyAction.NO_TRADE
    assert decision.as_of is None
    assert decision.conviction == Decimal(0)


def test_no_bars_holding_is_hold() -> None:
    pos = PositionState(symbol="X", quantity=Decimal(5), entry_price=Decimal(100))
    decision = SwingStrategy().evaluate(market_data=MarketData("X", ()), position=pos)
    assert decision.action is StrategyAction.HOLD


def test_insufficient_history_is_no_trade() -> None:
    # Fewer bars than the trend SMA needs -> indicators not ready -> NO_TRADE.
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([100 + i for i in range(50)])),
        position=PositionState.flat("X"),
    )
    assert decision.action is StrategyAction.NO_TRADE


def test_flat_market_is_no_trade() -> None:
    # A constant series: close is never above the trend SMA -> no entry.
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([100.0] * 260)),
        position=PositionState.flat("X"),
    )
    assert decision.action is StrategyAction.NO_TRADE


# --------------------------------------------------------------------------
# Entry.
# --------------------------------------------------------------------------


def test_confirmed_uptrend_buys() -> None:
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _uptrend()), position=PositionState.flat("X")
    )
    assert decision.action is StrategyAction.BUY
    assert set(decision.fired_rules) == {"trend_up", "momentum_up", "not_overbought"}
    assert Decimal(0) <= decision.conviction <= Decimal(1)
    assert decision.as_of is not None


def test_downtrend_does_not_buy() -> None:
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([300 - 0.5 * i for i in range(260)])),
        position=PositionState.flat("X"),
    )
    assert decision.action is StrategyAction.NO_TRADE
    # every rule is still recorded (audit trail), even the ones that failed
    assert len(decision.rules) == 3


# --------------------------------------------------------------------------
# Exit -- the risk-critical path.
# --------------------------------------------------------------------------


def test_stop_loss_fires_on_price_alone() -> None:
    # entry 100, stop level 92 (8%); latest close 91 -> SELL, maximal conviction.
    pos = PositionState(symbol="X", quantity=Decimal(5), entry_price=Decimal(100))
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([100.0] * 259 + [91.0])), position=pos
    )
    assert decision.action is StrategyAction.SELL
    assert "stop_loss" in decision.fired_rules
    assert decision.conviction == Decimal(1)


def test_stop_loss_protects_with_no_indicator_history() -> None:
    # Only three bars -> SMAs/RSI all unavailable, yet the price-only stop still
    # exits. This is the "capital protection works even when indicators cannot".
    pos = PositionState(symbol="X", quantity=Decimal(5), entry_price=Decimal(100))
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([100.0, 99.0, 90.0])), position=pos
    )
    assert decision.action is StrategyAction.SELL
    assert "stop_loss" in decision.fired_rules


def test_stop_not_hit_and_trend_intact_holds() -> None:
    # Rising series, entry just below the latest close -> stop far away, no break.
    prices = [100 + 0.6 * i for i in range(260)]
    pos = PositionState(
        symbol="X", quantity=Decimal(5), entry_price=Decimal(str(prices[-1] - 1))
    )
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars(prices)), position=pos
    )
    assert decision.action is StrategyAction.HOLD


def test_trend_break_exits_without_a_stop() -> None:
    prices = _rise_then_fall()
    last_close = prices[-1].close
    # entry above the latest close so the recent dip does NOT hit the 8% stop --
    # isolating the trend-break exit from the stop-loss exit.
    pos = PositionState(
        symbol="X", quantity=Decimal(5), entry_price=last_close * Decimal("1.03")
    )
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", prices), position=pos
    )
    assert decision.action is StrategyAction.SELL
    assert "trend_break" in decision.fired_rules
    assert "stop_loss" not in decision.fired_rules


# --------------------------------------------------------------------------
# Purity, mapping, config.
# --------------------------------------------------------------------------


def test_evaluate_is_pure_same_inputs_same_output() -> None:
    strategy = SwingStrategy()
    md = MarketData("X", _uptrend())
    flat = PositionState.flat("X")
    first = strategy.evaluate(market_data=md, position=flat)
    second = strategy.evaluate(market_data=md, position=flat)
    assert first == second


def test_decision_maps_to_decision_fields_without_llm_rationale() -> None:
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _uptrend()), position=PositionState.flat("X")
    )
    fields = decision.as_decision_fields()
    assert set(fields) == {
        "symbol",
        "action",
        "conviction",
        "rules_fired",
        "market_data_as_of",
    }
    # the strategy's rationale is deterministic, NOT an LLM rationale
    assert "llm_rationale" not in fields
    # rules_fired carries the FULL rule set (fired and not), for the audit trail
    assert isinstance(fields["rules_fired"], list)
    assert all("fired" in r for r in fields["rules_fired"])


def test_returned_decision_is_immutable() -> None:
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", ()), position=PositionState.flat("X")
    )
    assert isinstance(decision, StrategyDecision)
    with pytest.raises((AttributeError, TypeError)):
        decision.action = StrategyAction.BUY  # type: ignore[misc]


def test_min_bars_is_driven_by_the_longest_lookback() -> None:
    strategy = SwingStrategy()
    cfg = strategy.config
    assert strategy.min_bars == max(cfg.trend_ma, cfg.rsi_period + 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fast_ma": 50, "slow_ma": 20},  # fast must be < slow
        {"slow_ma": 200, "trend_ma": 100},  # slow must be < trend
        {"rsi_period": 0},
        {"rsi_ceiling": 0},
        {"rsi_ceiling": 101},
        {"stop_loss_pct": Decimal(0)},
        {"stop_loss_pct": Decimal(1)},
        {"conviction_full_spread": 0.0},
    ],
)
def test_config_rejects_nonsensical_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SwingConfig(**kwargs)  # type: ignore[arg-type]


def test_default_config_is_valid_and_frozen() -> None:
    assert DEFAULT_SWING_CONFIG.stop_loss_pct == Decimal("0.08")
    with pytest.raises((AttributeError, TypeError)):
        DEFAULT_SWING_CONFIG.stop_loss_pct = Decimal("0.05")  # type: ignore[misc]


# --------------------------------------------------------------------------
# Malformed-tick rejection at the Bar boundary (architect D1) + the exact
# stop boundary (architect N1).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_bar_rejects_non_finite_or_non_positive_price(bad: str) -> None:
    # A malformed tick must be rejected HERE, at construction, so the market-data
    # adapter can fail closed -- not flow in and make the stop comparison raise.
    with pytest.raises(ValueError, match="finite, positive"):
        Bar(
            timestamp=_T0,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(bad),
            volume=Decimal(1000),
        )


def test_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="volume"):
        Bar(
            timestamp=_T0,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(-1),
        )


def test_stop_fires_at_the_exact_boundary() -> None:
    # entry 100, stop level exactly 92 (8%); close == 92 -> the inclusive stop
    # (<=) MUST fire. The one control that loses money when wrong gets its exact
    # equality boundary pinned, not just a strictly-below case.
    pos = PositionState(symbol="X", quantity=Decimal(5), entry_price=Decimal(100))
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([100.0] * 259 + [92.0])), position=pos
    )
    assert decision.action is StrategyAction.SELL
    assert "stop_loss" in decision.fired_rules


def test_flat_position_conviction_clamps_to_zero() -> None:
    # A no-entry decision expresses zero confidence (lower clamp), end to end.
    decision = SwingStrategy().evaluate(
        market_data=MarketData("X", _bars([300 - 0.5 * i for i in range(260)])),
        position=PositionState.flat("X"),
    )
    assert decision.conviction == Decimal(0)
