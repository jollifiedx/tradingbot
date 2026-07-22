"""Tests for the halt latch.

The load-bearing test is `test_drift_then_clean_does_not_reenable_trading`,
which the architect required explicitly: a drift halt must survive a later
clean reconciliation. Everything else pins the owner's 2026-07-21 rulings and
the fail-closed edges.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from app.worker.latch import LatchDecision, LatchReason, decide_posture
from app.worker.reconciliation import (
    HaltCategory,
    Mismatch,
    ReconciliationResult,
    ReconciliationStatus,
)


def _clean() -> ReconciliationResult:
    return ReconciliationResult.clean()


def _drift() -> ReconciliationResult:
    """A real disagreement: the broker holds something our books don't expect."""
    return ReconciliationResult(
        status=ReconciliationStatus.UNEXPECTED_BROKER_POSITION,
        positions_reconciled=False,
        mismatches=(
            Mismatch(
                kind=ReconciliationStatus.UNEXPECTED_BROKER_POSITION,
                symbol="TSLA",
                expected=None,
                actual=Decimal("5"),
                detail="broker holds 5 TSLA the books do not expect",
            ),
        ),
    )


def _transient() -> ReconciliationResult:
    """We could not look -- broker unreadable."""
    return ReconciliationResult(
        status=ReconciliationStatus.BROKER_UNREADABLE,
        positions_reconciled=False,
        mismatches=(
            Mismatch(
                kind=ReconciliationStatus.BROKER_UNREADABLE,
                symbol=None,
                expected=None,
                actual=None,
                detail="could not read the broker account",
            ),
        ),
    )


def _not_verified() -> ReconciliationResult:
    """Positions matched, but the cash leg was never compared."""
    return ReconciliationResult(
        status=ReconciliationStatus.CASH_NOT_VERIFIED,
        positions_reconciled=True,
        cash_checked=False,
    )


def test_fixtures_have_the_categories_the_rules_key_on() -> None:
    assert _clean().category is HaltCategory.NONE
    assert _drift().category is HaltCategory.DRIFT
    assert _transient().category is HaltCategory.TRANSIENT
    assert _not_verified().category is HaltCategory.NOT_VERIFIED


# --------------------------------------------------------------------------
# The rulings.
# --------------------------------------------------------------------------


def test_clean_and_unfrozen_permits_trading() -> None:
    decision = decide_posture(result=_clean(), currently_frozen=False)
    assert decision.may_trade is True
    assert decision.engage_freeze is False
    assert decision.reason is LatchReason.CLEAR


def test_drift_halts_and_engages_the_freeze() -> None:
    decision = decide_posture(result=_drift(), currently_frozen=False)
    assert decision.may_trade is False
    assert decision.engage_freeze is True
    assert decision.reason is LatchReason.DRIFT_HALT


def test_drift_then_clean_does_not_reenable_trading() -> None:
    """The architect's required test: a drift halt is sticky.

    Run 1 sees drift and engages the freeze. Run 2 comes back perfectly clean.
    Because the freeze flag is now set, run 2 must STILL refuse to trade -- a
    clean check may never silently undo a real disagreement.
    """
    first = decide_posture(result=_drift(), currently_frozen=False)
    assert first.engage_freeze is True

    # The caller persisted frozen=true; the next run reconciles clean.
    second = decide_posture(result=_clean(), currently_frozen=True)
    assert second.may_trade is False
    assert second.reason is LatchReason.FROZEN


def test_drift_survives_a_restart() -> None:
    """A fresh process reads frozen=true from the DB and stays halted."""
    after_restart = decide_posture(result=_clean(), currently_frozen=True)
    assert after_restart.may_trade is False
    assert after_restart.engage_freeze is False
    assert after_restart.reason is LatchReason.FROZEN


def test_drift_is_still_latched_when_settings_are_unreadable() -> None:
    """Architect B1: an observed drift must never be silently discarded.

    If the settings row can't be read we halt for THAT reason -- but the drift
    we just saw still has to be persisted, or the next run reads clean and
    trading resumes with nobody ever acknowledging the disagreement.
    """
    decision = decide_posture(result=_drift(), currently_frozen=None)
    assert decision.may_trade is False
    assert decision.engage_freeze is True, "drift observed must still latch"
    assert decision.reason is LatchReason.SETTINGS_UNREADABLE


def test_drift_is_still_latched_when_already_frozen() -> None:
    """Architect B1: drift seen during an owner freeze must still be recorded.

    Otherwise: owner freezes at 08:50 for their own reasons, the worker sees
    drift at 08:55 and writes nothing, owner unfreezes at 09:00 believing they
    are clearing only their own freeze -- and the drift is gone.
    """
    decision = decide_posture(result=_drift(), currently_frozen=True)
    assert decision.may_trade is False
    assert decision.engage_freeze is True, "drift observed must still latch"
    assert decision.reason is LatchReason.FROZEN


@pytest.mark.parametrize("falsy", [0, "", [], 0.0, Decimal("0")])
def test_non_bool_falsy_frozen_flag_halts(falsy: object) -> None:
    """Architect D1: identity, not truthiness.

    `BotSettings.frozen` is a real bool today, but a caller that hand-rolls the
    value from a cached dict or raw row is one careless line away. Anything that
    is not exactly False must halt.
    """
    decision = decide_posture(result=_clean(), currently_frozen=falsy)  # type: ignore[arg-type]
    assert decision.may_trade is False
    assert decision.reason is LatchReason.FROZEN


def test_transient_then_clean_does_clear() -> None:
    """A blip halts without freezing, so a later clean run resumes."""
    blip = decide_posture(result=_transient(), currently_frozen=False)
    assert blip.may_trade is False
    assert blip.engage_freeze is False, "a transient failure must not latch"
    assert blip.reason is LatchReason.TRANSIENT_HALT

    recovered = decide_posture(result=_clean(), currently_frozen=False)
    assert recovered.may_trade is True


def test_not_verified_halts_without_freezing_and_is_not_owner_clearable() -> None:
    decision = decide_posture(result=_not_verified(), currently_frozen=False)
    assert decision.may_trade is False
    assert decision.engage_freeze is False
    assert decision.reason is LatchReason.CASH_NOT_VERIFIED
    # Not an owner-clearable halt: reconciliation itself says so.
    assert _not_verified().owner_must_clear is False


def test_owner_freeze_beats_a_clean_run() -> None:
    """The kill switch is absolute: clean books do not override the owner."""
    decision = decide_posture(result=_clean(), currently_frozen=True)
    assert decision.may_trade is False
    assert decision.reason is LatchReason.FROZEN


# --------------------------------------------------------------------------
# Fail-closed edges.
# --------------------------------------------------------------------------


def test_unreadable_settings_halts() -> None:
    decision = decide_posture(result=_clean(), currently_frozen=None)
    assert decision.may_trade is False
    assert decision.reason is LatchReason.SETTINGS_UNREADABLE


def test_missing_reconciliation_halts() -> None:
    """A missed or errored run is not permission."""
    decision = decide_posture(result=None, currently_frozen=False)
    assert decision.may_trade is False
    assert decision.reason is LatchReason.NO_RECONCILIATION


def test_unreadable_settings_beats_a_missing_result() -> None:
    decision = decide_posture(result=None, currently_frozen=None)
    assert decision.reason is LatchReason.SETTINGS_UNREADABLE


@pytest.mark.parametrize(
    "result",
    [None, _clean(), _drift(), _transient(), _not_verified()],
)
@pytest.mark.parametrize("frozen", [True, False, None])
def test_never_raises_and_never_unfreezes(
    result: ReconciliationResult | None, frozen: bool | None
) -> None:
    """Across every combination: a decision comes back, and it never unfreezes.

    `LatchDecision` has no field that clears the freeze -- this asserts the
    surface stays that way, so no future edit can add a worker-side unfreeze.
    """
    decision = decide_posture(result=result, currently_frozen=frozen)
    assert isinstance(decision, LatchDecision)


def test_decision_surface_cannot_grow_a_release_field() -> None:
    """A real tripwire, not a hardcoded name check (architect N2).

    Pinning the exact field set means ANY future field -- `clear_latch`,
    `resume`, `frozen_override` -- breaks this test and forces a deliberate
    conversation. The worker must only ever be able to ENGAGE a safety
    mechanism, never release one.
    """
    assert {f.name for f in dataclasses.fields(LatchDecision)} == {
        "may_trade",
        "engage_freeze",
        "reason",
    }


def test_defensive_fallthrough_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unreachable-by-construction branch still fails closed (architect N3).

    A stand-in whose category is NONE but which is not reconciled cannot be
    built from the real class; duck-type it to prove the fall-through halts
    rather than dropping into the permit branch.
    """

    class _Impossible:
        category = HaltCategory.NONE
        reconciled = False

    decision = decide_posture(result=_Impossible(), currently_frozen=False)  # type: ignore[arg-type]
    assert decision.may_trade is False
    assert decision.reason is LatchReason.TRANSIENT_HALT


@pytest.mark.parametrize("frozen", [True, None])
def test_trading_is_impossible_whenever_frozen_is_not_exactly_false(
    frozen: bool | None,
) -> None:
    for result in (_clean(), _drift(), _transient(), _not_verified(), None):
        assert decide_posture(result=result, currently_frozen=frozen).may_trade is False


def test_only_drift_ever_engages_the_freeze() -> None:
    """Drift latches; nothing else does -- for EVERY frozen state.

    Architect D-3: this originally only exercised ``currently_frozen=False``,
    which left a mutant alive -- making the SETTINGS_UNREADABLE branch latch
    unconditionally kept the whole suite green. That behaviour matters in both
    directions: a transient DB blip must NOT latch a permanent, owner-clearable
    freeze, or Esther ends up unfreezing reflexively after every hiccup, which
    destroys the signal a real drift halt is supposed to carry.
    """
    for frozen in (False, True, None):
        for result in (_clean(), _transient(), _not_verified(), None):
            decision = decide_posture(result=result, currently_frozen=frozen)
            assert decision.engage_freeze is False, (
                f"only drift may latch (result={result}, frozen={frozen})"
            )
        assert (
            decide_posture(result=_drift(), currently_frozen=frozen).engage_freeze
            is True
        ), f"drift must latch even when frozen={frozen}"


def test_decision_rejects_incoherent_construction() -> None:
    with pytest.raises(ValueError, match="permit trading and freeze"):
        LatchDecision(may_trade=True, engage_freeze=True, reason=LatchReason.CLEAR)
    with pytest.raises(ValueError, match="exactly when the reason is CLEAR"):
        LatchDecision(may_trade=True, engage_freeze=False, reason=LatchReason.FROZEN)
    # The converse (architect N1): a CLEAR reason that does NOT permit trading
    # is equally incoherent, and would fail open for a caller that gates on
    # `reason` rather than `may_trade`.
    with pytest.raises(ValueError, match="exactly when the reason is CLEAR"):
        LatchDecision(may_trade=False, engage_freeze=False, reason=LatchReason.CLEAR)
    # Legal and load-bearing: drift latched while halting for another reason.
    assert LatchDecision(
        may_trade=False, engage_freeze=True, reason=LatchReason.FROZEN
    ).engage_freeze is True
