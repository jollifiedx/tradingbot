"""Tests for the pure strategy interface and its value types (app.worker.strategy.base).

Covers the drift guard against the DB's ``DecisionAction`` vocabulary, the
long-only ``PositionState`` validation (a mis-built state is a programmer error
that raises), the ``Bar`` UTC-aware guard, conviction quantization/clamping, and
the ``decisions``-row mapping. No I/O; these are plain value objects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.models import DecisionAction
from app.worker.strategy.base import (
    Bar,
    MarketData,
    PositionState,
    RuleResult,
    StrategyAction,
    StrategyDecision,
    quantize_conviction,
)


def _bar(close: str = "100", ts: datetime | None = None) -> Bar:
    ts = ts or datetime(2024, 1, 1, tzinfo=UTC)
    return Bar(
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


# --------------------------------------------------------------------------- #
# Drift guard: StrategyAction must stay 1:1 with the DB's DecisionAction
# --------------------------------------------------------------------------- #


def test_strategy_action_matches_decision_action_values() -> None:
    """The strategy vocabulary must map onto ``decisions.action`` with no drift."""
    assert {a.value for a in StrategyAction} == {a.value for a in DecisionAction}
    # And member-for-member, so a rename on one side is caught too.
    for member in StrategyAction:
        assert DecisionAction(member.value).value == member.value


# --------------------------------------------------------------------------- #
# Bar: UTC-aware guard
# --------------------------------------------------------------------------- #


def test_bar_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(
            timestamp=datetime(2024, 1, 1),  # noqa: DTZ001 -- deliberately naive
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
        )


def test_bar_accepts_utc_aware_timestamp() -> None:
    bar = _bar()
    assert bar.close == Decimal("100")


# --------------------------------------------------------------------------- #
# MarketData.latest
# --------------------------------------------------------------------------- #


def test_market_data_latest_none_when_empty() -> None:
    assert MarketData("X", ()).latest is None


def test_market_data_latest_is_newest() -> None:
    b1 = _bar("10", datetime(2024, 1, 1, tzinfo=UTC))
    b2 = _bar("11", datetime(2024, 1, 2, tzinfo=UTC))
    assert MarketData("X", (b1, b2)).latest is b2


# --------------------------------------------------------------------------- #
# PositionState: long-only validation (raises on programmer error)
# --------------------------------------------------------------------------- #


def test_flat_position_is_not_open() -> None:
    pos = PositionState.flat("X")
    assert pos.is_open is False
    assert pos.quantity == Decimal(0)
    assert pos.entry_price is None


def test_open_position_is_open() -> None:
    pos = PositionState("X", Decimal("3"), entry_price=Decimal("50"))
    assert pos.is_open is True


def test_negative_quantity_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        PositionState("X", Decimal("-1"))


def test_non_finite_quantity_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        PositionState("X", Decimal("NaN"))


def test_open_position_without_entry_price_rejected() -> None:
    with pytest.raises(ValueError, match="entry_price"):
        PositionState("X", Decimal("1"))


def test_open_position_with_non_positive_entry_price_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        PositionState("X", Decimal("1"), entry_price=Decimal("0"))


def test_flat_position_with_entry_price_rejected() -> None:
    with pytest.raises(ValueError, match="flat position"):
        PositionState("X", Decimal("0"), entry_price=Decimal("10"))


# --------------------------------------------------------------------------- #
# quantize_conviction: clamp to [0,1], round to 3 places
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("0.5"), Decimal("0.500")),
        (Decimal("1.5"), Decimal("1.000")),  # clamped up
        (Decimal("-0.3"), Decimal("0.000")),  # clamped down
        (Decimal("0.12349"), Decimal("0.123")),
        (Decimal("0.12350"), Decimal("0.124")),  # half-up
    ],
)
def test_quantize_conviction(raw: Decimal, expected: Decimal) -> None:
    assert quantize_conviction(raw) == expected


# --------------------------------------------------------------------------- #
# StrategyDecision: mapping onto a decisions row, fired_rules
# --------------------------------------------------------------------------- #


def test_fired_rules_lists_only_fired_in_order() -> None:
    decision = StrategyDecision(
        symbol="X",
        action=StrategyAction.BUY,
        conviction=Decimal("0.500"),
        rationale="ok",
        rules=(
            RuleResult("a", True, ""),
            RuleResult("b", False, ""),
            RuleResult("c", True, ""),
        ),
        as_of=datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert decision.fired_rules == ("a", "c")


def test_as_decision_fields_shape() -> None:
    """Maps onto decisions columns; carries the FULL rule set, omits llm_rationale."""
    as_of = datetime(2024, 1, 2, tzinfo=UTC)
    decision = StrategyDecision(
        symbol="AAPL",
        action=StrategyAction.SELL,
        conviction=Decimal("0.750"),
        rationale="deterministic reason (NOT an llm rationale)",
        rules=(RuleResult("stop_loss", True, "hit"), RuleResult("trend_break", False, "no")),
        as_of=as_of,
    )
    fields = decision.as_decision_fields()
    assert fields == {
        "symbol": "AAPL",
        "action": "sell",
        "conviction": Decimal("0.750"),
        "rules_fired": [
            {"name": "stop_loss", "fired": True, "detail": "hit"},
            {"name": "trend_break", "fired": False, "detail": "no"},
        ],
        "market_data_as_of": as_of,
    }
    # The deterministic rationale must NOT be smuggled into the LLM's column.
    assert "llm_rationale" not in fields
