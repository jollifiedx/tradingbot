"""First strategy module: a simple, conservative daily-bar SWING trend follower.

Owner ruling 2026-07-21 (docs/decisions.md): build the SWING layer first --
daily bars, holding days to weeks -- prove it vs SPY in forward paper, then add
the intraday layer later as a second module. This is that swing module and
nothing more.

The philosophy, in one line
---------------------------
Only hold in a confirmed uptrend; ride the trend; cut losers with a hard stop
and exit when the trend rolls over. Trend-following, not prediction. It trades
rarely (a handful of times a year per symbol), which is the whole point: low
frequency = low cost drag, the single biggest killer of retail edges
(viability-analysis.md, RISK/cost section).

The ruleset (kept deliberately small -- see the curve-fitting note below)
-------------------------------------------------------------------------
ENTRY (flat -> BUY), ALL must hold:
  R1 trend_up       -- close is above the long-term SMA (only long in uptrends).
  R2 momentum_up    -- fast SMA is above slow SMA (the trend is up *now*).
  R3 not_overbought -- RSI is below the ceiling (don't chase a blow-off top).
EXIT (holding -> SELL), ANY triggers:
  X1 stop_loss   -- close <= entry * (1 - stop_loss_pct). Capital protection;
                    evaluated on price alone, so it still works if indicators
                    are unavailable.
  X2 trend_break -- fast SMA falls below slow SMA (the up-move is over).
Otherwise HOLD (in position) or NO_TRADE (flat).

Because R2 (fast > slow) and X2 (fast < slow) are exact negations, the strategy
cannot re-enter on the same bar it exits on a trend break: right after such a
SELL it is flat with fast < slow, so R2 fails and the result is NO_TRADE. A
re-entry then requires the fast SMA to genuinely cross back above the slow one.

Known behaviour, documented not hidden: a *stop-loss* exit can fire while the
trend filter still reads "up" (a sharp dip the lagging SMAs haven't caught).
The bot may then re-enter within a day or two if the uptrend is intact. That is
a deliberate trade-off -- adding a cooldown would be one more tunable parameter,
and this strategy spends its parameter budget elsewhere. Flag for forward-paper
review: does whipsaw after stops hurt in practice?

Purity and money
----------------
:meth:`SwingStrategy.evaluate` is pure (no I/O, no clock, no randomness) and
total (never raises on market data). Indicators (SMA, RSI) are computed with
**pandas-ta** and are inherently floating-point -- indicator *comparisons* are
done in float. Every **price** comparison that defines real risk -- the
stop-loss level -- is done in :class:`~decimal.Decimal`. Any indicator that
comes back missing or non-finite is treated as "cannot confirm", failing safe:
a flat position stays flat (``NO_TRADE``), and an open position still honours
its price-only stop.

!! RISK PARAMETERS ARE DRAFT !!
-------------------------------
Every number below is a DRAFT starting point for Esther's review, NOT a
committed choice. Per CLAUDE.md, anything affecting stop distance / sizing / how
much capital a signal implies is owner-approval. The single true risk parameter
here is ``stop_loss_pct`` (it sets how far price can go against an open position
before the strategy says exit); the moving-average lengths and RSI ceiling are
signal-shape parameters that must not be tuned to fit history (that is how
backtests lie -- viability-analysis.md, RISK 4). This strategy SUGGESTS; it
never sizes a real order. None of these values should inform real sizing until
the owner has reviewed them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd
import pandas_ta as ta

from app.worker.strategy.base import (
    MarketData,
    PositionState,
    RuleResult,
    Strategy,
    StrategyAction,
    StrategyDecision,
    quantize_conviction,
)


# --------------------------------------------------------------------------- #
# DRAFT config block -- every number here is owner-review material (see header).
# One place, each commented, easy to edit. Do NOT scatter these constants.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SwingConfig:
    """Tunable knobs for :class:`SwingStrategy`. All values are DRAFT.

    Six parameters in total. That is at the upper edge of "a handful" -- the
    viability analysis warns that every added knob is another way to curve-fit
    history and lose forward. Do not grow this set without a concrete reason, and
    do not tune it against a backtest (a backtest may inform *deterministic*
    rules, but the mission is judged on forward paper -- viability-analysis.md).
    """

    # -- Signal shape (trend/momentum). Tunable, but curve-fit risk: leave alone
    #    unless there is a principled reason, never fit to a backtest curve. --
    fast_ma: int = 20
    """Fast SMA length in days -- the near-term trend."""

    slow_ma: int = 50
    """Slow SMA length in days -- fast>slow marks an up-regime, fast<slow a down."""

    trend_ma: int = 200
    """Long-term SMA length -- the regime filter; only go long above it."""

    rsi_period: int = 14
    """RSI lookback in days -- the momentum/overbought gauge."""

    rsi_ceiling: float = 75.0
    """Don't open above this RSI (avoid chasing an overbought blow-off top)."""

    # -- TRUE RISK PARAMETER (owner-approval before it informs any real sizing). --
    stop_loss_pct: Decimal = Decimal("0.08")
    """Hard stop distance below entry (0.08 = 8%). Sets how much a losing trade
    can cost before the strategy says exit -- this is the risk knob."""

    # -- Advisory-only reporting knob. Does NOT gate or size anything; it only
    #    scales the conviction number attached to a decision for later ranking. --
    conviction_full_spread: float = 0.10
    """The fast/slow SMA gap (as a fraction of the slow SMA) that maps to full
    conviction 1.0. Purely cosmetic: changing it never changes what is traded."""

    def __post_init__(self) -> None:
        # Nonsensical config is a programmer error, caught at construction -- this
        # is not market data, so raising is correct (mirrors ReconciliationResult).
        if not (0 < self.fast_ma < self.slow_ma < self.trend_ma):
            raise ValueError("require 0 < fast_ma < slow_ma < trend_ma")
        if self.rsi_period <= 0:
            raise ValueError("rsi_period must be positive")
        if not (0 < self.rsi_ceiling <= 100):
            raise ValueError("rsi_ceiling must be in (0, 100]")
        if not (Decimal(0) < self.stop_loss_pct < Decimal(1)):
            raise ValueError("stop_loss_pct must be a fraction in (0, 1)")
        if self.conviction_full_spread <= 0:
            raise ValueError("conviction_full_spread must be positive")


DEFAULT_SWING_CONFIG = SwingConfig()
"""The DRAFT defaults. Owner must review before these inform real sizing."""


@dataclass(frozen=True, slots=True)
class _Indicators:
    """The latest value of each indicator the ruleset needs. All float or None.

    ``None`` means "could not be computed" (too little history, or a non-finite
    result) -- the caller treats that as "cannot confirm", never as zero.
    """

    close: float
    sma_fast: float | None
    sma_slow: float | None
    sma_trend: float | None
    rsi: float | None

    @property
    def trend_ready(self) -> bool:
        """True iff every indicator needed for a FLAT entry decision is usable."""
        return None not in (self.sma_fast, self.sma_slow, self.sma_trend, self.rsi)


class SwingStrategy(Strategy):
    """Conservative daily-bar trend follower. Pure; long-only; signals only."""

    name = "swing_trend_v1"

    def __init__(self, config: SwingConfig = DEFAULT_SWING_CONFIG) -> None:
        self.config = config

    @property
    def min_bars(self) -> int:
        """Bars needed before the full ruleset can be evaluated.

        Driven by the longest lookback: the trend SMA needs ``trend_ma`` bars and
        RSI needs ``rsi_period + 1`` (it is a change-based indicator). Below this,
        a flat position gets ``NO_TRADE`` -- but an open position still gets its
        price-only stop, which needs no history at all.
        """
        return max(self.config.trend_ma, self.config.rsi_period + 1)

    # -- indicator computation (the only float/pandas-touching part) -----------
    def _compute_indicators(self, bars: MarketData) -> _Indicators:
        """Latest SMA/RSI values from the close series. Deterministic; never raises.

        pandas-ta returns ``None`` (or a NaN tail) when a series is too short for a
        given length; both collapse to ``None`` here so the ruleset sees a single
        "not available" signal. The close series is built from the bars' Decimal
        closes via ``float`` -- indicator space is float by nature; the risk-
        defining price comparison stays in Decimal in :meth:`evaluate`.
        """
        closes = pd.Series([float(b.close) for b in bars.bars], dtype="float64")

        def _tail(series: pd.Series[float] | None) -> float | None:
            if series is None or len(series) == 0:
                return None
            value = series.iloc[-1]
            fvalue = float(value)
            return fvalue if math.isfinite(fvalue) else None

        cfg = self.config
        return _Indicators(
            close=float(bars.bars[-1].close),
            sma_fast=_tail(ta.sma(closes, length=cfg.fast_ma)),
            sma_slow=_tail(ta.sma(closes, length=cfg.slow_ma)),
            sma_trend=_tail(ta.sma(closes, length=cfg.trend_ma)),
            rsi=_tail(ta.rsi(closes, length=cfg.rsi_period)),
        )

    # -- the pure decision ------------------------------------------------------
    def evaluate(
        self, *, market_data: MarketData, position: PositionState
    ) -> StrategyDecision:
        """See :meth:`Strategy.evaluate`. Pure, total, long-only."""
        latest = market_data.latest
        if latest is None:
            return self._decide(
                symbol=market_data.symbol,
                action=StrategyAction.HOLD if position.is_open else StrategyAction.NO_TRADE,
                conviction=Decimal(0),
                rationale="no market data available; standing pat",
                rules=(),
                as_of=None,
            )

        as_of = latest.timestamp
        indic = self._compute_indicators(market_data)

        if position.is_open:
            return self._evaluate_exit(
                market_data.symbol, position, indic, latest.close, as_of
            )
        return self._evaluate_entry(market_data.symbol, indic, as_of)

    def _evaluate_exit(
        self,
        symbol: str,
        position: PositionState,
        indic: _Indicators,
        close: Decimal,
        as_of: datetime,
    ) -> StrategyDecision:
        """Holding: SELL on stop-loss or trend break, else HOLD.

        The stop is evaluated FIRST and in Decimal, on price alone, so it protects
        capital even when indicators could not be computed. ``entry_price`` is
        guaranteed present for an open position (PositionState enforces it).

        ``close`` is the bar's NATIVE Decimal close (not the float indicator
        value): the one risk-defining comparison must never round-trip the money
        path through float.
        """
        cfg = self.config
        entry = position.entry_price
        assert entry is not None  # guaranteed by PositionState for an open position
        stop_level = entry * (Decimal(1) - cfg.stop_loss_pct)
        stop_hit = close <= stop_level

        # Trend break needs both SMAs; if either is unavailable we cannot confirm
        # a break -> the rule does not fire (fail safe: never invent an exit signal
        # from missing data; the stop remains the backstop).
        if indic.sma_fast is not None and indic.sma_slow is not None:
            trend_break = indic.sma_fast < indic.sma_slow
            trend_detail = (
                f"fast SMA {indic.sma_fast:.4f} "
                f"{'<' if trend_break else '>='} slow SMA {indic.sma_slow:.4f}"
            )
        else:
            trend_break = False
            trend_detail = "SMAs unavailable; cannot confirm a trend break"

        rules = (
            RuleResult(
                name="stop_loss",
                fired=stop_hit,
                detail=(
                    f"close {close} {'<=' if stop_hit else '>'} stop level "
                    f"{stop_level} (entry {entry} - {cfg.stop_loss_pct:%})"
                ),
            ),
            RuleResult(name="trend_break", fired=trend_break, detail=trend_detail),
        )

        if stop_hit or trend_break:
            reason = "stop-loss hit" if stop_hit else "trend broke (fast SMA below slow)"
            # Conviction in the exit: a hard stop is maximally decisive; a plain
            # trend break scales with how far the trend has rolled over.
            conviction = Decimal(1) if stop_hit else self._downside_strength(indic)
            return self._decide(
                symbol=symbol,
                action=StrategyAction.SELL,
                conviction=conviction,
                rationale=f"exit: {reason}",
                rules=rules,
                as_of=as_of,
            )

        return self._decide(
            symbol=symbol,
            action=StrategyAction.HOLD,
            conviction=self._upside_strength(indic),
            rationale="hold: uptrend intact and stop not hit",
            rules=rules,
            as_of=as_of,
        )

    def _evaluate_entry(
        self, symbol: str, indic: _Indicators, as_of: datetime
    ) -> StrategyDecision:
        """Flat: BUY iff every entry rule fires, else NO_TRADE.

        If any indicator is unavailable (too little history, or a non-finite
        value -- e.g. RSI of a perfectly flat series), we cannot confirm the setup
        and return NO_TRADE. This is what makes "insufficient history" and "flat
        market" both resolve to NO_TRADE.
        """
        if not indic.trend_ready:
            return self._decide(
                symbol=symbol,
                action=StrategyAction.NO_TRADE,
                conviction=Decimal(0),
                rationale=(
                    "no entry: insufficient history or indicators unavailable "
                    "(need a full trend/RSI window)"
                ),
                rules=(),
                as_of=as_of,
            )

        cfg = self.config
        # Narrowed to float by trend_ready; assert for the type checker.
        assert indic.sma_fast is not None
        assert indic.sma_slow is not None
        assert indic.sma_trend is not None
        assert indic.rsi is not None

        trend_up = indic.close > indic.sma_trend
        momentum_up = indic.sma_fast > indic.sma_slow
        not_overbought = indic.rsi < cfg.rsi_ceiling

        rules = (
            RuleResult(
                name="trend_up",
                fired=trend_up,
                detail=(
                    f"close {indic.close:.4f} "
                    f"{'>' if trend_up else '<='} trend SMA {indic.sma_trend:.4f}"
                ),
            ),
            RuleResult(
                name="momentum_up",
                fired=momentum_up,
                detail=(
                    f"fast SMA {indic.sma_fast:.4f} "
                    f"{'>' if momentum_up else '<='} slow SMA {indic.sma_slow:.4f}"
                ),
            ),
            RuleResult(
                name="not_overbought",
                fired=not_overbought,
                detail=(
                    f"RSI {indic.rsi:.2f} "
                    f"{'<' if not_overbought else '>='} ceiling {cfg.rsi_ceiling:.2f}"
                ),
            ),
        )

        if trend_up and momentum_up and not_overbought:
            return self._decide(
                symbol=symbol,
                action=StrategyAction.BUY,
                conviction=self._upside_strength(indic),
                rationale="entry: uptrend + momentum aligned, not overbought",
                rules=rules,
                as_of=as_of,
            )

        return self._decide(
            symbol=symbol,
            action=StrategyAction.NO_TRADE,
            conviction=Decimal(0),
            rationale="no entry: not all entry conditions met",
            rules=rules,
            as_of=as_of,
        )

    # -- conviction (advisory only; never gates or sizes) ----------------------
    def _upside_strength(self, indic: _Indicators) -> Decimal:
        """Trend-strength score in [0,1] from the fast-over-slow SMA gap.

        Advisory metadata only. Undefined inputs -> 0 (no confidence expressed).
        """
        return self._spread_strength(indic, positive=True)

    def _downside_strength(self, indic: _Indicators) -> Decimal:
        """Confidence in a trend-break exit, from the slow-over-fast SMA gap."""
        return self._spread_strength(indic, positive=False)

    def _spread_strength(self, indic: _Indicators, *, positive: bool) -> Decimal:
        if indic.sma_fast is None or indic.sma_slow is None or indic.sma_slow == 0:
            return Decimal(0)
        spread = (indic.sma_fast - indic.sma_slow) / indic.sma_slow
        directional = spread if positive else -spread
        score = directional / self.config.conviction_full_spread
        return quantize_conviction(Decimal(str(score)))

    def _decide(
        self,
        *,
        symbol: str,
        action: StrategyAction,
        conviction: Decimal,
        rationale: str,
        rules: tuple[RuleResult, ...],
        as_of: datetime | None,
    ) -> StrategyDecision:
        """Assemble the immutable decision, quantizing conviction to the DB scale."""
        return StrategyDecision(
            symbol=symbol,
            action=action,
            conviction=quantize_conviction(conviction),
            rationale=rationale,
            rules=rules,
            as_of=as_of,
        )
