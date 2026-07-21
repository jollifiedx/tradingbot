"""Exhaustive tests for the pure pre-order safety gate (safety-tests skill / E1).

Every check gets three cases at minimum: a passing case, the exact boundary
(at the cap / threshold), and the just-over failure -- asserting the SAFE
outcome (denied with the correct :class:`GateReason`), never merely "no
exception". The documented priority order is pinned by tests that make several
checks fail at once and assert which reason wins. The fail-closed contract
(``None`` required input -> deny with the mapped reason) is covered per input.

No mocking, no I/O: the gate is pure, so every branch is exercised directly.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.models import BotSettings
from app.worker.safety_gate import GateDecision, GateReason, evaluate_order_safety

# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _settings(
    *,
    frozen: bool = False,
    buy_power_cap: str = "10000.00",
    max_daily_loss: str = "500.00",
    max_per_trade_cap: str = "2000.00",
    staleness_threshold_seconds: int = 5,
) -> BotSettings:
    """A permissive-but-realistic settings row; individual tests tighten one knob."""
    return BotSettings(
        frozen=frozen,
        buy_power_cap=Decimal(buy_power_cap),
        max_daily_loss=Decimal(max_daily_loss),
        max_per_trade_cap=Decimal(max_per_trade_cap),
        staleness_threshold_seconds=staleness_threshold_seconds,
        updated_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _clean_kwargs(**overrides: object) -> dict[str, object]:
    """A fully-clean call that returns OK; override one field to test a check.

    Clean baseline: per-trade 1000 <= 2000; buy-power 1000+1000=2000 <= 10000;
    loss 0 < 500; 1s <= 5s threshold; reconciled; not frozen.
    """
    kwargs: dict[str, object] = {
        "settings": _settings(),
        "order_notional": Decimal("1000.00"),
        "deployed_capital": Decimal("1000.00"),
        "loss_so_far": Decimal("0.00"),
        "seconds_since_tick": 1.0,
        "reconciled": True,
    }
    kwargs.update(overrides)
    return kwargs


def _evaluate(**overrides: object) -> GateDecision:
    return evaluate_order_safety(**_clean_kwargs(**overrides))  # type: ignore[arg-type]


def _assert_denied(decision: GateDecision, reason: GateReason) -> None:
    assert decision.allowed is False
    assert decision.reason is reason


def _assert_allowed(decision: GateDecision) -> None:
    assert decision.allowed is True
    assert decision.reason is GateReason.OK


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def test_fully_clean_order_is_allowed() -> None:
    _assert_allowed(_evaluate())


# --------------------------------------------------------------------------- #
# 1. Settings unreadable (Invariant #2, fail closed)
# --------------------------------------------------------------------------- #


def test_settings_none_is_unreadable() -> None:
    _assert_denied(_evaluate(settings=None), GateReason.SETTINGS_UNREADABLE)


def test_settings_unreadable_outranks_every_other_failure() -> None:
    # settings=None while every other input is also unsafe -> still the top reason.
    decision = evaluate_order_safety(
        settings=None,
        order_notional=Decimal("999999.00"),
        deployed_capital=Decimal("999999.00"),
        loss_so_far=Decimal("999999.00"),
        seconds_since_tick=99999.0,
        reconciled=False,
    )
    _assert_denied(decision, GateReason.SETTINGS_UNREADABLE)


# --------------------------------------------------------------------------- #
# 2. Frozen (owner kill switch)
# --------------------------------------------------------------------------- #


def test_frozen_denies() -> None:
    _assert_denied(_evaluate(settings=_settings(frozen=True)), GateReason.FROZEN)


def test_frozen_short_circuits_even_when_everything_else_is_bad() -> None:
    # Priority: with a readable-but-frozen row, FROZEN wins over unreconciled,
    # stale, daily-loss and both caps all failing simultaneously.
    decision = evaluate_order_safety(
        settings=_settings(frozen=True),
        order_notional=Decimal("999999.00"),  # over per-trade cap
        deployed_capital=Decimal("999999.00"),  # over buy-power cap
        loss_so_far=Decimal("999999.00"),  # over daily loss
        seconds_since_tick=99999.0,  # stale
        reconciled=False,  # unreconciled
    )
    _assert_denied(decision, GateReason.FROZEN)


# --------------------------------------------------------------------------- #
# 3. Reconciliation (Invariant #6)
# --------------------------------------------------------------------------- #


def test_unreconciled_false_denies() -> None:
    _assert_denied(_evaluate(reconciled=False), GateReason.UNRECONCILED)


def test_unreconciled_none_denies_fail_closed() -> None:
    _assert_denied(_evaluate(reconciled=None), GateReason.UNRECONCILED)


def test_unreconciled_outranks_stale_loss_and_caps() -> None:
    decision = evaluate_order_safety(
        settings=_settings(),
        order_notional=Decimal("999999.00"),
        deployed_capital=Decimal("999999.00"),
        loss_so_far=Decimal("999999.00"),
        seconds_since_tick=99999.0,
        reconciled=False,
    )
    _assert_denied(decision, GateReason.UNRECONCILED)


# --------------------------------------------------------------------------- #
# 4. Staleness (Invariant #3)  -- boundary to the second
# --------------------------------------------------------------------------- #


def test_stale_data_under_threshold_allowed() -> None:
    _assert_allowed(_evaluate(seconds_since_tick=4.0))


def test_stale_data_exactly_at_threshold_is_fresh_allowed() -> None:
    # 5s since tick, threshold 5s: at the threshold is fresh.
    _assert_allowed(_evaluate(seconds_since_tick=5.0))


def test_stale_data_one_second_over_is_stale_denied() -> None:
    _assert_denied(_evaluate(seconds_since_tick=6.0), GateReason.STALE_DATA)


def test_stale_data_fractionally_over_is_stale_denied() -> None:
    _assert_denied(_evaluate(seconds_since_tick=5.001), GateReason.STALE_DATA)


def test_stale_data_none_denies_fail_closed() -> None:
    _assert_denied(_evaluate(seconds_since_tick=None), GateReason.STALE_DATA)


def test_stale_outranks_daily_loss_and_caps() -> None:
    decision = _evaluate(
        seconds_since_tick=99999.0,
        loss_so_far=Decimal("999999.00"),
        order_notional=Decimal("999999.00"),
        deployed_capital=Decimal("999999.00"),
    )
    _assert_denied(decision, GateReason.STALE_DATA)


# --------------------------------------------------------------------------- #
# 5. Daily-loss halt  -- at-limit is a breach (>=)
# --------------------------------------------------------------------------- #


def test_daily_loss_just_under_limit_allowed() -> None:
    _assert_allowed(_evaluate(loss_so_far=Decimal("499.99")))


def test_daily_loss_exactly_at_limit_is_breach_denied() -> None:
    # Documented: exactly at the limit halts (>=), never trade through it.
    _assert_denied(_evaluate(loss_so_far=Decimal("500.00")), GateReason.DAILY_LOSS)


def test_daily_loss_one_cent_over_denied() -> None:
    _assert_denied(_evaluate(loss_so_far=Decimal("500.01")), GateReason.DAILY_LOSS)


def test_daily_loss_profit_is_allowed() -> None:
    # A profit (negative loss magnitude) is comfortably under the cap.
    _assert_allowed(_evaluate(loss_so_far=Decimal("-250.00")))


def test_daily_loss_none_denies_fail_closed() -> None:
    _assert_denied(_evaluate(loss_so_far=None), GateReason.DAILY_LOSS)


def test_daily_loss_outranks_caps() -> None:
    decision = _evaluate(
        loss_so_far=Decimal("999999.00"),
        order_notional=Decimal("999999.00"),
        deployed_capital=Decimal("999999.00"),
    )
    _assert_denied(decision, GateReason.DAILY_LOSS)


# --------------------------------------------------------------------------- #
# 6. Per-trade cap  -- at cap allowed, one cent over denied
# --------------------------------------------------------------------------- #


def test_per_trade_cap_under_allowed() -> None:
    _assert_allowed(_evaluate(order_notional=Decimal("1999.99")))


def test_per_trade_cap_exactly_at_cap_allowed() -> None:
    # order 2000 == max_per_trade_cap 2000; deployed 1000 keeps buy-power clear.
    _assert_allowed(_evaluate(order_notional=Decimal("2000.00")))


def test_per_trade_cap_one_cent_over_denied() -> None:
    _assert_denied(
        _evaluate(order_notional=Decimal("2000.01")), GateReason.PER_TRADE_CAP
    )


def test_per_trade_cap_none_denies_fail_closed() -> None:
    _assert_denied(_evaluate(order_notional=None), GateReason.PER_TRADE_CAP)


def test_per_trade_cap_outranks_buy_power_cap() -> None:
    # Order is over the per-trade cap AND would blow the buy-power cap;
    # per-trade is reported first.
    decision = _evaluate(
        order_notional=Decimal("50000.00"),  # > 2000 per-trade cap
        deployed_capital=Decimal("9000.00"),  # 9000+50000 >> 10000 buy-power cap
    )
    _assert_denied(decision, GateReason.PER_TRADE_CAP)


# --------------------------------------------------------------------------- #
# 7. Buy-power cap  -- deployed + notional, at cap allowed, one cent over denied
# --------------------------------------------------------------------------- #


def test_buy_power_cap_under_allowed() -> None:
    _assert_allowed(
        _evaluate(deployed_capital=Decimal("7000.00"), order_notional=Decimal("2000.00"))
    )  # 9000 <= 10000


def test_buy_power_cap_exactly_at_cap_allowed() -> None:
    # deployed 8000 + order 2000 == buy-power cap 10000 (order == per-trade cap OK).
    _assert_allowed(
        _evaluate(deployed_capital=Decimal("8000.00"), order_notional=Decimal("2000.00"))
    )


def test_buy_power_cap_one_cent_over_denied() -> None:
    _assert_denied(
        _evaluate(
            deployed_capital=Decimal("8000.01"), order_notional=Decimal("2000.00")
        ),
        GateReason.BUY_POWER_CAP,
    )


def test_buy_power_cap_none_deployed_denies_fail_closed() -> None:
    _assert_denied(_evaluate(deployed_capital=None), GateReason.BUY_POWER_CAP)


# --------------------------------------------------------------------------- #
# GateDecision value semantics
# --------------------------------------------------------------------------- #


def test_gate_decision_is_immutable() -> None:
    decision = GateDecision.allow()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_allow_and_deny_constructors() -> None:
    _assert_allowed(GateDecision.allow())
    _assert_denied(GateDecision.deny(GateReason.FROZEN), GateReason.FROZEN)


# --------------------------------------------------------------------------- #
# Non-finite inputs (B1): NaN / +Inf / -Inf must DENY at the input's rung and
# NEVER raise. A NaN clock must not fail open; a Decimal('NaN') must not raise
# on comparison. Fold in alongside the None handling.
# --------------------------------------------------------------------------- #

_NON_FINITE_FLOATS = (
    float("nan"),
    float("inf"),
    float("-inf"),
)
_NON_FINITE_DECIMALS = (
    Decimal("NaN"),
    Decimal("Infinity"),
    Decimal("-Infinity"),
)


@pytest.mark.parametrize("bad", _NON_FINITE_FLOATS)
def test_non_finite_seconds_since_tick_denies_stale(bad: float) -> None:
    # NaN in particular fails OPEN without this guard (`nan > threshold` is False).
    _assert_denied(_evaluate(seconds_since_tick=bad), GateReason.STALE_DATA)


@pytest.mark.parametrize("bad", _NON_FINITE_DECIMALS)
def test_non_finite_loss_so_far_denies_daily_loss(bad: Decimal) -> None:
    # Decimal('NaN') would RAISE on `>=` without this guard.
    _assert_denied(_evaluate(loss_so_far=bad), GateReason.DAILY_LOSS)


@pytest.mark.parametrize("bad", _NON_FINITE_DECIMALS)
def test_non_finite_order_notional_denies_per_trade_cap(bad: Decimal) -> None:
    _assert_denied(_evaluate(order_notional=bad), GateReason.PER_TRADE_CAP)


@pytest.mark.parametrize("bad", _NON_FINITE_DECIMALS)
def test_non_finite_deployed_capital_denies_buy_power_cap(bad: Decimal) -> None:
    _assert_denied(_evaluate(deployed_capital=bad), GateReason.BUY_POWER_CAP)


def test_non_finite_inputs_never_raise_and_always_deny() -> None:
    # Feed every non-finite value to every numeric input; the gate must return a
    # denial (allowed=False), never raise decimal.InvalidOperation or anything else.
    for value in _NON_FINITE_FLOATS:
        decision = _evaluate(seconds_since_tick=value)
        assert decision.allowed is False
    for value in _NON_FINITE_DECIMALS:
        for field in ("loss_so_far", "order_notional", "deployed_capital"):
            decision = _evaluate(**{field: value})
            assert decision.allowed is False


def test_denied_reason_is_never_ok() -> None:
    # Any non-clean input must produce a non-OK reason and allowed=False.
    for overrides in (
        {"settings": None},
        {"settings": _settings(frozen=True)},
        {"reconciled": False},
        {"seconds_since_tick": 999.0},
        {"loss_so_far": Decimal("999999.00")},
        {"order_notional": Decimal("999999.00")},
        {"deployed_capital": Decimal("999999.00")},
    ):
        decision = _evaluate(**overrides)
        assert decision.allowed is False
        assert decision.reason is not GateReason.OK
