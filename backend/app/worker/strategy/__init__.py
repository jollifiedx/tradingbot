"""Rules-engine strategy modules (pluggable; swing now, intraday later).

Owner ruling 2026-07-21 (docs/decisions.md): the rules engine runs multiple
STRATEGY modules inside ONE worker -- the swing module is built first, an
intraday module is added later as a second module. The safety gate / order path
/ worker stay strategy-agnostic; nothing here is wired to them yet.

Import the interface and value types (``base``) freely -- it is standard-library
only. ``SwingStrategy`` and the backtest harness pull pandas / pandas-ta /
vectorbt, so import those from their submodules when you actually need them.
"""

from app.worker.strategy.base import (
    Bar,
    MarketData,
    PositionState,
    RuleResult,
    Strategy,
    StrategyAction,
    StrategyDecision,
    quantize_conviction,
)

__all__ = [
    "Bar",
    "MarketData",
    "PositionState",
    "RuleResult",
    "Strategy",
    "StrategyAction",
    "StrategyDecision",
    "quantize_conviction",
]
