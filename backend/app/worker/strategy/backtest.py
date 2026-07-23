"""Backtest / validation harness for a :class:`Strategy`, built on vectorbt.

READ THIS FIRST -- what a backtest here IS and IS NOT
====================================================
A green backtest is **NOT** evidence the strategy works, and this harness must
never be presented as validation-for-live. Two reasons, both from
viability-analysis.md and binding on this project:

1. **Forward-only mission.** The success criterion is beating SPY buy-and-hold in
   *forward paper trading* over a meaningful sample, BEFORE any real money. A
   historical backtest may only inform the *deterministic* rules (parameter
   sanity, does the code do what we think). It never promotes a strategy toward
   live.
2. **Look-ahead bias.** Any LLM-influenced version of this strategy is judged
   EXCLUSIVELY on forward paper results: an LLM's training data contains the
   historical answers, so a backtest over any past window is contaminated. This
   swing module is purely deterministic today, so a backtest is a legitimate
   engineering check of the rules -- but the moment an LLM touches the signal,
   backtest numbers become marketing, not evidence.

What the harness DOES do (honestly)
-----------------------------------
- Runs the strategy the SAME way the live worker will: it walks the bars one at a
  time and calls :meth:`Strategy.evaluate` with only the history up to that bar
  plus the position state it has built so far. There is no separate vectorized
  reimplementation of the rules that could silently drift from live behaviour,
  and the walk cannot see the future. (Cost: it recomputes indicators each step,
  O(n^2); fine for a few hundred daily bars, and the strategy is designed to be
  called once per day live anyway -- correctness over cleverness.)
- Feeds the resulting entry/exit signals to vectorbt for P&L accounting **with
  explicit costs applied** (commission + spread + slippage; see :class:`CostModel`).
- Reports return, max drawdown, win rate, profit factor, trade count, AND a
  side-by-side SPY buy-and-hold comparison over the same window, AND the verdict
  ``beats_spy_after_costs`` -- a strategy that loses to SPY after costs is a
  FAILING strategy whatever its win rate (viability-analysis.md).

Fill assumption: a decision is made at a bar's close (using data through that
close) and filled at that same close. Costs then push the fill against us.

Money is :class:`~decimal.Decimal` at this module's boundaries (costs, the
reported ratios); the vectorbt/pandas core is float, as numerics libraries are.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import vectorbt as vbt

from app.worker.strategy.base import (
    Bar,
    MarketData,
    PositionState,
    Strategy,
    StrategyAction,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# The standing disclaimer, surfaced in every report so a reader cannot mistake a
# backtest for validation. Kept as a constant so it is impossible to omit.
BACKTEST_DISCLAIMER = (
    "A backtest is NOT validation. The mission requires beating SPY buy-and-hold "
    "in FORWARD paper trading over a meaningful sample before any real money. "
    "Historical results may inform deterministic rules only; any LLM-influenced "
    "version is judged forward-only (look-ahead bias). Do not promote to live on "
    "the strength of these numbers."
)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Explicit, DRAFT trading-cost assumptions. Every strategy report applies these.

    All three are fractions of notional, charged per side (entry and exit each pay
    them). They are ASSUMPTIONS to review, not committed values -- but they must
    always be applied: costs quietly turn a marginally-positive strategy into a
    loser (viability-analysis.md), so a costless backtest is a lie.

    - ``commission_fraction`` -- broker commission. Webull US-stock commission is
      $0, but small regulatory fees (SEC/TAF) exist; default is a token amount.
    - ``spread_fraction`` -- half the bid/ask spread, paid crossing the spread.
    - ``slippage_fraction`` -- adverse fill vs the close (market impact / delay).

    vectorbt is given ``commission_fraction`` as its ``fees`` and
    ``spread_fraction + slippage_fraction`` as its ``slippage``; the three are
    reported separately so the assumption set is legible.
    """

    commission_fraction: Decimal = Decimal("0.0005")
    spread_fraction: Decimal = Decimal("0.0005")
    slippage_fraction: Decimal = Decimal("0.0010")

    def __post_init__(self) -> None:
        for name in ("commission_fraction", "spread_fraction", "slippage_fraction"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative fraction")

    @property
    def slippage_component(self) -> Decimal:
        """What is handed to vectorbt as ``slippage`` (spread + slippage)."""
        return self.spread_fraction + self.slippage_fraction

    @property
    def round_trip_fraction(self) -> Decimal:
        """Total cost of one entry + one exit, as a fraction of notional.

        Used to charge the SPY buy-and-hold benchmark its own (single) entry cost
        for a fair comparison; see :func:`run_backtest`.
        """
        per_side = self.commission_fraction + self.slippage_component
        return per_side * 2


DEFAULT_COST_MODEL = CostModel()
"""DRAFT default costs. Conservative-ish; owner/analyst should sanity-check."""


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Immutable result of one backtest run. Ratios are Decimals (money discipline).

    ``beats_spy_after_costs`` is the one honest headline: strategy net return vs
    SPY buy-and-hold net return over the identical window. Everything else is
    diagnostic. A ``False`` here means FAILING, regardless of win rate.
    """

    symbol: str
    strategy_name: str
    bars_count: int
    trades_count: int
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal | None
    profit_factor: Decimal | None
    spy_return: Decimal
    beats_spy_after_costs: bool
    cost_model: CostModel
    initial_cash: Decimal
    disclaimer: str = BACKTEST_DISCLAIMER

    def summary(self) -> str:
        """A plain-text report, disclaimer first so it is never read as validation."""
        wr = "n/a" if self.win_rate is None else f"{self.win_rate:.2%}"
        pf = "n/a" if self.profit_factor is None else f"{self.profit_factor:.3f}"
        verdict = "BEATS" if self.beats_spy_after_costs else "LOSES TO"
        return (
            f"!! {self.disclaimer}\n"
            f"Backtest: {self.strategy_name} on {self.symbol} "
            f"({self.bars_count} bars)\n"
            f"  net total return : {self.total_return:.2%} (after costs)\n"
            f"  SPY buy&hold     : {self.spy_return:.2%} (after entry cost)\n"
            f"  VERDICT          : strategy {verdict} SPY after costs\n"
            f"  max drawdown     : {self.max_drawdown:.2%}\n"
            f"  trades           : {self.trades_count}\n"
            f"  win rate         : {wr}\n"
            f"  profit factor    : {pf}\n"
            f"  costs applied    : commission {self.cost_model.commission_fraction} "
            f"+ spread {self.cost_model.spread_fraction} "
            f"+ slippage {self.cost_model.slippage_fraction} (per side)\n"
        )


def generate_signals(
    strategy: Strategy, bars: Sequence[Bar]
) -> tuple[list[bool], list[bool]]:
    """Walk the bars, calling ``strategy.evaluate`` exactly as the live worker will.

    Returns ``(entries, exits)`` boolean lists aligned to ``bars``. At each bar the
    strategy sees ONLY the history up to and including that bar (no look-ahead) and
    the position state built from prior signals; a ``BUY`` while flat opens at that
    bar's close, a ``SELL`` while holding closes. ``HOLD``/``NO_TRADE`` do nothing.
    Pure: no I/O, deterministic for deterministic bars.
    """
    entries = [False] * len(bars)
    exits = [False] * len(bars)
    position = PositionState.flat(strategy_symbol(bars))

    for i in range(len(bars)):
        window = MarketData(symbol=position.symbol, bars=tuple(bars[: i + 1]))
        decision = strategy.evaluate(market_data=window, position=position)
        if not position.is_open and decision.action is StrategyAction.BUY:
            entries[i] = True
            position = PositionState(
                symbol=position.symbol,
                quantity=Decimal(1),  # unit position; sizing is not the strategy's job
                entry_price=bars[i].close,
            )
        elif position.is_open and decision.action is StrategyAction.SELL:
            exits[i] = True
            position = PositionState.flat(position.symbol)
    return entries, exits


def strategy_symbol(bars: Sequence[Bar]) -> str:
    """Placeholder symbol for the walk (the pure rules do not key on it).

    The strategy's rules never depend on the symbol string, only on the bars, so
    the backtest uses a fixed label. Kept as a named helper so the intent is
    explicit rather than a bare literal in the loop.
    """
    return "BACKTEST"


def bars_from_ohlcv(rows: Sequence[tuple[object, ...]]) -> tuple[Bar, ...]:
    """Adapter: build strategy :class:`Bar`s from broker/raw OHLCV rows.

    Each row is ``(timestamp, open, high, low, close, volume)``; prices/volume are
    coerced to :class:`Decimal` via ``str`` so a float never sneaks in with binary
    rounding. This is the one place a broker ``OHLCVBar`` (or a fixture) becomes a
    strategy ``Bar``; keeping it here leaves the strategy free of any broker type.
    """
    from datetime import datetime

    out: list[Bar] = []
    for ts, o, h, low, c, v in rows:
        if not isinstance(ts, datetime):
            raise TypeError("bar timestamp must be a datetime")
        out.append(
            Bar(
                timestamp=ts,
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(c)),
                volume=Decimal(str(v)),
            )
        )
    return tuple(out)


def _to_decimal(value: float) -> Decimal:
    """Finite float -> Decimal via str (no binary-float artefacts); NaN/Inf -> 0."""
    return Decimal(str(value)) if math.isfinite(value) else Decimal(0)


def run_backtest(
    *,
    strategy: Strategy,
    bars: Sequence[Bar],
    spy_bars: Sequence[Bar],
    cost_model: CostModel = DEFAULT_COST_MODEL,
    initial_cash: Decimal = Decimal("10000.00"),
) -> BacktestReport:
    """Run ``strategy`` over ``bars`` with costs, benchmarked to SPY buy-and-hold.

    Deterministic: same inputs -> same report. Requires at least two bars on each
    side (a return needs a start and an end). Fill assumption: decide at a close,
    fill at that close, costs push the fill against us.

    Parameters
    ----------
    strategy: the module under test.
    bars: the traded symbol's daily bars, oldest -> newest.
    spy_bars: SPY daily bars over (ideally) the same window, for the benchmark.
    cost_model: the cost assumptions to apply (always applied; see :class:`CostModel`).
    initial_cash: starting equity for the P&L accounting.
    """
    if len(bars) < 2:
        raise ValueError("need at least two bars to measure a return")
    if len(spy_bars) < 2:
        raise ValueError("need at least two SPY bars for the benchmark")

    close = pd.Series(
        [float(b.close) for b in bars],
        index=pd.DatetimeIndex([b.timestamp for b in bars]),
        dtype="float64",
    )
    entries, exits = generate_signals(strategy, bars)

    portfolio = vbt.Portfolio.from_signals(
        close,
        entries=pd.Series(entries, index=close.index),
        exits=pd.Series(exits, index=close.index),
        init_cash=float(initial_cash),
        fees=float(cost_model.commission_fraction),
        slippage=float(cost_model.slippage_component),
        freq="1D",
    )

    trades = portfolio.trades
    trades_count = int(trades.count())
    total_return = _to_decimal(float(portfolio.total_return()))
    max_drawdown = _to_decimal(float(portfolio.max_drawdown()))

    win_rate: Decimal | None = None
    profit_factor: Decimal | None = None
    if trades_count > 0:
        win_rate = _to_decimal(float(trades.win_rate()))
        pf_raw = float(trades.profit_factor())
        # profit_factor is +inf when there are no losing trades: report None
        # rather than a bogus Decimal, so a reader is not misled by "infinite".
        profit_factor = _to_decimal(pf_raw) if math.isfinite(pf_raw) else None

    # SPY buy-and-hold over its window, charged a single entry cost for fairness
    # (a hold pays to get in once and never crosses the spread again).
    spy_gross = Decimal(str(spy_bars[-1].close)) / Decimal(str(spy_bars[0].close)) - 1
    entry_cost = cost_model.round_trip_fraction / 2
    spy_return = (Decimal(1) + spy_gross) * (Decimal(1) - entry_cost) - 1

    return BacktestReport(
        symbol=strategy_symbol(bars),
        strategy_name=strategy.name,
        bars_count=len(bars),
        trades_count=trades_count,
        total_return=total_return,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        profit_factor=profit_factor,
        spy_return=spy_return,
        beats_spy_after_costs=total_return > spy_return,
        cost_model=cost_model,
        initial_cash=initial_cash,
    )


__all__ = [
    "BACKTEST_DISCLAIMER",
    "DEFAULT_COST_MODEL",
    "BacktestReport",
    "CostModel",
    "bars_from_ohlcv",
    "generate_signals",
    "run_backtest",
]
