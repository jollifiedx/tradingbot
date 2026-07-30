"""OBSERVE MODE: the bot thinks out loud, and records it -- it places NO order.

This is the safe precursor to the order path. The worker evaluates a fixed
watchlist against real daily bars and APPENDS the resulting decisions to the
`decisions` audit table, so the owner can watch the strategy's calls in the
dashboard decision log BEFORE any real execution is wired. Nothing here can
trade, and nothing here is *structurally able* to trade:

- the market-data source is typed as :class:`BarSource` -- one read-only method,
  ``get_historical_bars``. There is no ``place_order`` on it to call (the real
  :class:`~app.core.webull.WebullClient` is a read-only wrapper besides).
- the decision sink is typed as :class:`DecisionSink` -- one method,
  ``insert_decision``, which is an INSERT into an append-only table. It cannot
  update or delete, and it is not an order.

Both narrowings mirror the worker's :class:`~app.worker.scheduler.SettingsStore`
protocol: making the *incapability* a type, not a promise (invariant #1 -- only
the deterministic rules engine ever trades, and only through the future audited
order path; invariant #5 -- `decisions` is append-only).

What this deliberately does NOT do (all deferred to the order path)
-------------------------------------------------------------------
- It never computes a real position size or share count.
- It never reads real position state: it evaluates every symbol from
  :meth:`PositionState.flat`, because the bot holds nothing and there is no
  trades ledger yet. Real position state arrives WITH the order path; until
  then, "flat" is the honest input. (A consequence: the recorded exit rules --
  stop-loss, trend-break -- can never fire in observe mode, because they only
  apply to an open position. That is correct: observe mode records ENTRY intent,
  not exit management.)
- It never wires the safety gate to execution -- there is no execution.

The watchlist is MANUAL (owner decision, docs/decisions.md: the analyst/research
layer is deferred). It is a small fixed set of liquid US names, sourced from no
paid service.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from app.core.webull import BarTimespan, HistoricalBarsRequest, MarketCategory
from app.worker.strategy.backtest import bars_from_ohlcv
from app.worker.strategy.base import MarketData, PositionState
from app.worker.strategy.swing import SwingStrategy

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from app.core.models import Decision
    from app.core.webull import HistoricalBars
    from app.worker.strategy.base import Bar, Strategy

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    """One symbol to observe, with the market category its bars are fetched under.

    Category matters: SPY is an ETF and AAPL/MSFT are common stock, and Webull's
    history endpoint is category-keyed. Pairing each symbol with its category
    keeps that explicit rather than defaulting every name to ``US_STOCK``.
    """

    symbol: str
    category: MarketCategory


# The MANUAL watchlist (owner decision -- no research layer yet). A handful of
# liquid US names: two large-cap stocks plus the SPY ETF (also the mission's
# benchmark). Edit this list to change what OBSERVE MODE evaluates; it is the one
# place the set is defined.
WATCHLIST: tuple[WatchlistEntry, ...] = (
    WatchlistEntry("AAPL", MarketCategory.US_STOCK),
    WatchlistEntry("MSFT", MarketCategory.US_STOCK),
    WatchlistEntry("SPY", MarketCategory.US_ETF),
)

# Daily bars (swing_trend_v1 reasons over daily bars). The count is chosen to
# exceed the strategy's longest lookback (the 200-day trend SMA) with margin, so
# a full session of history is available rather than a truncated window that can
# only ever return NO_TRADE. Well under HistoricalBarsRequest's 1200 ceiling.
_OBSERVE_TIMESPAN = BarTimespan.DAY
_OBSERVE_BAR_COUNT = 250


class BarSource(Protocol):
    """The read-only market-data surface OBSERVE MODE needs, and nothing more.

    :class:`~app.core.webull.WebullClient` satisfies this structurally. There is
    deliberately no order method here to call.
    """

    def get_historical_bars(self, request: HistoricalBarsRequest) -> HistoricalBars: ...


class DecisionSink(Protocol):
    """The append-only write surface OBSERVE MODE needs, and nothing more.

    :class:`~app.core.db.Database` satisfies this structurally. ``insert_decision``
    is an INSERT into an append-only table -- not an order, and not a mutation.
    """

    async def insert_decision(
        self,
        *,
        symbol: str,
        action: str,
        conviction: Decimal | None,
        rules_fired: list[Any] | dict[str, Any],
        market_data_as_of: datetime | None,
    ) -> Decision: ...


def _to_strategy_bars(bars: HistoricalBars) -> tuple[Bar, ...]:
    """Convert broker :class:`OHLCVBar`s into strategy :class:`Bar`s at the boundary.

    Reuses :func:`bars_from_ohlcv` (the backtest harness's adapter -- the one
    place a broker bar becomes a strategy bar), so the conversion is not
    reinvented. A malformed bar (non-finite/non-positive price) is rejected there
    at construction and raises, which the per-symbol guard in
    :func:`observe_watchlist` turns into a SKIP -- an unusable series is never
    guessed at.
    """
    rows: Sequence[tuple[object, ...]] = [
        (b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in bars.bars
    ]
    return bars_from_ohlcv(rows)


def _fetch_bars(client: BarSource, entry: WatchlistEntry, count: int) -> HistoricalBars:
    """One blocking SDK read for a symbol's daily bars (run in a worker thread)."""
    return client.get_historical_bars(
        HistoricalBarsRequest(
            symbol=entry.symbol,
            timespan=_OBSERVE_TIMESPAN,
            count=count,
            category=entry.category,
        )
    )


async def observe_watchlist(
    *,
    client: BarSource,
    db: DecisionSink,
    strategy: Strategy | None = None,
    watchlist: Sequence[WatchlistEntry] = WATCHLIST,
    count: int = _OBSERVE_BAR_COUNT,
) -> list[Decision]:
    """Evaluate each watchlist symbol on real daily bars and RECORD the decision.

    For every symbol: fetch its daily bars, convert them to strategy bars, run
    :meth:`SwingStrategy.evaluate` from a FLAT position (the bot holds nothing --
    see the module docstring), and append the decision to `decisions`. Places NO
    order (it has no order method to call).

    Isolation is per symbol: one symbol failing -- a bad API response, a
    malformed bar, a DB error -- is logged and skipped, and the rest still run. A
    symbol whose bars are empty/unusable is SKIPPED, never recorded on a guess.

    Returns the decisions that were actually recorded (for tests/observability);
    the audit table is the durable record. The blocking SDK reads run in a worker
    thread so the event loop is never stalled.
    """
    strategy = strategy if strategy is not None else SwingStrategy()
    recorded: list[Decision] = []
    for entry in watchlist:
        try:
            bars = await asyncio.to_thread(_fetch_bars, client, entry, count)
            strat_bars = _to_strategy_bars(bars)
            if not strat_bars:
                log.warning(
                    "observe.symbol_skipped",
                    symbol=entry.symbol,
                    reason="no usable bars returned",
                )
                continue
            market_data = MarketData(symbol=entry.symbol, bars=strat_bars)
            # OBSERVE MODE holds nothing; real position state arrives with the
            # order path (there is no trades ledger to read yet).
            decision = strategy.evaluate(
                market_data=market_data, position=PositionState.flat(entry.symbol)
            )
            # Exactly StrategyDecision.as_decision_fields(), passed with precise
            # types (llm_rationale/thesis_id/settings_snapshot stay NULL -- see
            # Database.insert_decision).
            stored = await db.insert_decision(
                symbol=decision.symbol,
                action=decision.action.value,
                conviction=decision.conviction,
                rules_fired=[rule.as_dict() for rule in decision.rules],
                market_data_as_of=decision.as_of,
            )
            recorded.append(stored)
            log.info(
                "observe.decision_recorded",
                symbol=entry.symbol,
                action=decision.action.value,
                conviction=str(decision.conviction),
                bars=len(strat_bars),
                strategy=strategy.name,
            )
        except Exception as exc:
            # Per-symbol fail-closed: never abort the rest of the watchlist, and
            # never record a guess for a symbol we could not evaluate cleanly.
            log.error(
                "observe.symbol_failed",
                symbol=entry.symbol,
                error_type=type(exc).__name__,
            )
            continue
    log.info(
        "observe.run_complete",
        symbols=len(watchlist),
        recorded=len(recorded),
        strategy=strategy.name,
    )
    return recorded
