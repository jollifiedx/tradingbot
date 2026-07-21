"""Exhaustive tests for reconciliation (CLAUDE.md invariant #6, `reconcile` skill).

The pure :func:`compare_positions` carries the safety weight, so it gets the bulk
of the coverage: every drift scenario from the `reconcile` skill (manual
out-of-band trade, missed fill during downtime, crash between order-persist and
submission, partial fill on one side only, duplicate submission), every
fail-closed input (``None`` / ``NaN`` / ``Inf`` on each numeric), and the exact
cash boundary to the cent.

Every test asserts the SAFE outcome -- ``reconciled is False`` **and** the exact
:class:`ReconciliationStatus` -- never merely "nothing raised". The orchestrator
tests use a fake WebullClient and a fake Database (no network, no DB, no
credentials) and additionally assert that reconciliation never writes anything.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, WebullEnv
from app.core.webull import (
    AccountBalance,
    AccountSnapshot,
    AccountSnapshotRequest,
    Position,
)
from app.worker import reconciliation as reconciliation_module
from app.worker.reconciliation import (
    _STATUS_CATEGORY,
    DEFAULT_CASH_TOLERANCE,
    ExpectedPosition,
    HaltCategory,
    Mismatch,
    ReconciliationResult,
    ReconciliationStatus,
    _mask_account_id,
    compare_positions,
    reconcile,
)

# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

BROKER_CASH = Decimal("50000.00")


def _pos(symbol: str, quantity: str | Decimal) -> Position:
    return Position(
        instrument_id=f"inst-{symbol}",
        symbol=symbol,
        quantity=Decimal(quantity) if isinstance(quantity, str) else quantity,
        cost_price=Decimal("10.0000"),
        market_value=Decimal("100.0000"),
        unrealized_pnl=Decimal("0.00"),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _RawPosition:
    """Structural stand-in for a broker position (satisfies `BrokerPosition`).

    Needed because the wrapper's Pydantic `Position` refuses a non-finite
    quantity outright (pinned by
    :func:`test_broker_position_model_rejects_non_finite_quantity`), which is the
    FIRST line of defence. This stand-in exercises the SECOND line -- the pure
    comparison's own numeric guards -- so a future loosening of the model (e.g.
    `allow_inf_nan=True`) cannot silently turn NaN into a "clean" verdict.
    """

    symbol: str
    quantity: Decimal


def _expected(symbol: str, quantity: str | Decimal) -> ExpectedPosition:
    return ExpectedPosition(
        symbol=symbol,
        quantity=Decimal(quantity) if isinstance(quantity, str) else quantity,
    )


def _compare(**overrides: Any) -> ReconciliationResult:
    """A fully-verified clean call (flat both sides, cash compared and equal).

    NOTE the baseline passes `expected_cash`: since the owner ruling of
    2026-07-21 a run whose cash leg was never compared is NOT reconciled, so a
    "clean" baseline has to include a cash expectation. The cash-unchecked path
    has its own tests below.
    """
    kwargs: dict[str, Any] = {
        "broker_positions": (),
        "expected_positions": (),
        "broker_cash": BROKER_CASH,
        "expected_cash": BROKER_CASH,
        "cash_tolerance": DEFAULT_CASH_TOLERANCE,
    }
    kwargs.update(overrides)
    return compare_positions(**kwargs)


def _assert_halted(
    result: ReconciliationResult, status: ReconciliationStatus
) -> None:
    assert result.reconciled is False
    assert result.status is status
    assert result.mismatches, "a halt must always say what differed"


def _assert_clean(result: ReconciliationResult) -> None:
    assert result.reconciled is True
    assert result.status is ReconciliationStatus.CLEAN
    assert result.mismatches == ()
    # CLEAN means fully verified: positions compared AND cash compared.
    assert result.positions_reconciled is True
    assert result.cash_checked is True
    assert result.category is HaltCategory.NONE
    assert result.owner_must_clear is False


# --------------------------------------------------------------------------- #
# Clean outcomes
# --------------------------------------------------------------------------- #


def test_flat_on_both_sides_is_clean() -> None:
    # Broker holds nothing, DB intends nothing, cash agrees.
    _assert_clean(_compare())


def test_matching_positions_are_clean() -> None:
    result = _compare(
        broker_positions=(_pos("AAPL", "10"), _pos("MSFT", "3.5")),
        expected_positions=(_expected("MSFT", "3.5"), _expected("AAPL", "10")),
    )
    _assert_clean(result)  # order of the sequences is irrelevant


def test_quantity_scale_difference_is_clean() -> None:
    # Decimal("10") == Decimal("10.000000"): same number, different scale.
    result = _compare(
        broker_positions=(_pos("AAPL", "10.000000"),),
        expected_positions=(_expected("AAPL", "10"),),
    )
    _assert_clean(result)


def test_symbol_case_and_whitespace_do_not_cause_a_false_halt() -> None:
    result = _compare(
        broker_positions=(_pos("aapl ", "10"),),
        expected_positions=(_expected(" AAPL", "10"),),
    )
    _assert_clean(result)


def test_clean_with_a_matching_cash_expectation_reports_cash_checked() -> None:
    result = _compare(expected_cash=BROKER_CASH)
    _assert_clean(result)
    assert result.cash_checked is True


# --------------------------------------------------------------------------- #
# A PARTIAL verification is not a verification (owner ruling, 2026-07-21)
# --------------------------------------------------------------------------- #


def test_positions_match_but_cash_never_compared_is_not_reconciled() -> None:
    # Today's real state: no DB cash ledger, so cash cannot be compared.
    result = _compare(expected_cash=None)

    assert result.reconciled is False  # the bit the safety gate consumes
    assert result.status is ReconciliationStatus.CASH_NOT_VERIFIED
    # ...but the diagnostics survive: "positions matched, cash never compared".
    assert result.positions_reconciled is True
    assert result.cash_checked is False


def test_cash_not_verified_is_not_an_owner_clearable_drift_halt() -> None:
    result = _compare(expected_cash=None)
    assert result.category is HaltCategory.NOT_VERIFIED
    assert result.owner_must_clear is False  # resolves structurally, not by hand


def test_cash_not_verified_still_reports_a_failed_positions_verdict() -> None:
    # Positions drifted AND cash was never compared: drift wins the headline,
    # and positions_reconciled records that the positions leg itself failed.
    result = _compare(
        broker_positions=(_pos("TSLA", "5"),), expected_positions=(), expected_cash=None
    )
    _assert_halted(result, ReconciliationStatus.UNEXPECTED_BROKER_POSITION)
    assert result.positions_reconciled is False
    assert result.cash_checked is False
    assert result.category is HaltCategory.DRIFT


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"expected_cash": None},
        {"broker_positions": (_pos("AAPL", "1"),)},
        {"broker_positions": (_pos("AAPL", "1"),), "expected_cash": None},
        {"expected_positions": (_expected("AAPL", "1"),)},
        {"broker_cash": None},
        {"broker_cash": None, "expected_cash": None},
        {"cash_tolerance": None},
        {"broker_positions": None},
        {"expected_positions": None},
        {
            "broker_positions": (_pos("AAPL", "1"),),
            "expected_positions": (_expected("AAPL", "1"),),
            "expected_cash": None,
        },
    ],
)
def test_no_input_combination_yields_reconciled_without_cash_checked(
    overrides: dict[str, Any],
) -> None:
    # The structural guarantee, swept over every shape of input: "reconciled"
    # can never be True unless cash was actually compared.
    result = _compare(**overrides)
    assert not (result.reconciled and not result.cash_checked)
    assert result.reconciled is (result.status is ReconciliationStatus.CLEAN)


def test_clean_constructor_cannot_be_built_partially_verified() -> None:
    # Not merely discouraged -- impossible. The verdict is derived from status,
    # and an inconsistent hand-built instance is rejected outright.
    assert ReconciliationResult.clean().cash_checked is True
    with pytest.raises(ValueError, match="partial verification"):
        ReconciliationResult(
            status=ReconciliationStatus.CLEAN,
            positions_reconciled=True,
            cash_checked=False,
        )


def test_clean_cannot_be_built_with_mismatches() -> None:
    with pytest.raises(ValueError, match="partial verification"):
        ReconciliationResult(
            status=ReconciliationStatus.CLEAN,
            positions_reconciled=True,
            cash_checked=True,
            mismatches=(
                Mismatch(
                    kind=ReconciliationStatus.CASH_MISMATCH,
                    symbol=None,
                    expected=None,
                    actual=None,
                    detail="x",
                ),
            ),
        )


def test_a_halt_must_say_what_differed() -> None:
    with pytest.raises(ValueError, match="must say what differed"):
        ReconciliationResult(
            status=ReconciliationStatus.QUANTITY_MISMATCH,
            positions_reconciled=False,
        )


# --------------------------------------------------------------------------- #
# THE LATCH RULE: every status is classified, drift is sticky
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (ReconciliationStatus.CLEAN, HaltCategory.NONE),
        (ReconciliationStatus.ACCOUNT_NOT_PINNED, HaltCategory.TRANSIENT),
        (ReconciliationStatus.BROKER_UNREADABLE, HaltCategory.TRANSIENT),
        (ReconciliationStatus.DB_UNREADABLE, HaltCategory.TRANSIENT),
        (ReconciliationStatus.DUPLICATE_POSITION, HaltCategory.DRIFT),
        (ReconciliationStatus.UNEXPECTED_BROKER_POSITION, HaltCategory.DRIFT),
        (ReconciliationStatus.MISSING_BROKER_POSITION, HaltCategory.DRIFT),
        (ReconciliationStatus.QUANTITY_MISMATCH, HaltCategory.DRIFT),
        (ReconciliationStatus.CASH_MISMATCH, HaltCategory.DRIFT),
        (ReconciliationStatus.CASH_NOT_VERIFIED, HaltCategory.NOT_VERIFIED),
    ],
)
def test_each_status_maps_to_its_owner_decided_category(
    status: ReconciliationStatus, category: HaltCategory
) -> None:
    assert _STATUS_CATEGORY[status] is category
    # Only DRIFT is the owner's to clear; the other halts are not.
    assert (category is HaltCategory.DRIFT) is (
        _STATUS_CATEGORY[status] is HaltCategory.DRIFT
    )


def test_every_status_is_classified() -> None:
    # Tripwire: a new status cannot be added without deciding how the caller
    # must treat it (auto-clear vs sticky vs structural).
    assert set(_STATUS_CATEGORY) == set(ReconciliationStatus)


def test_drift_is_reported_even_when_a_transient_status_wins_the_headline() -> None:
    # Broker cash unreadable (transient) AND a real position drift. The headline
    # is the broader transient status, but the category must stay DRIFT --
    # otherwise a caller latching on category would un-halt after real drift.
    result = _compare(
        broker_positions=(_pos("TSLA", "5"),),
        expected_positions=(),
        broker_cash=None,
    )
    assert result.status is ReconciliationStatus.BROKER_UNREADABLE
    assert result.category is HaltCategory.DRIFT
    assert result.owner_must_clear is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"broker_positions": (_pos("TSLA", "5"),), "expected_positions": ()},
        {"broker_positions": (), "expected_positions": (_expected("NVDA", "7"),)},
        {
            "broker_positions": (_pos("AAPL", "12"),),
            "expected_positions": (_expected("AAPL", "10"),),
        },
        {"broker_positions": (_pos("AAPL", "1"), _pos("AAPL", "2"))},
        {"expected_cash": BROKER_CASH + Decimal("5.00")},
    ],
)
def test_real_drift_is_sticky_and_owner_clearable(overrides: dict[str, Any]) -> None:
    result = _compare(**overrides)
    assert result.reconciled is False
    assert result.category is HaltCategory.DRIFT
    assert result.owner_must_clear is True


@pytest.mark.parametrize(
    "overrides",
    [{"broker_positions": None}, {"expected_positions": None}, {"broker_cash": None}],
)
def test_transient_failures_are_not_owner_clearable(overrides: dict[str, Any]) -> None:
    result = _compare(**overrides)
    assert result.reconciled is False
    assert result.category is HaltCategory.TRANSIENT
    assert result.owner_must_clear is False  # may auto-clear on a later clean run


# --------------------------------------------------------------------------- #
# Position drift (the `reconcile` skill's scenarios)
# --------------------------------------------------------------------------- #


def test_broker_position_the_db_never_intended_halts() -> None:
    # Manual trade in the Webull app / leftover state / a fill we never recorded
    # / a crash between order-persist and submission that actually filled.
    result = _compare(broker_positions=(_pos("TSLA", "5"),), expected_positions=())
    _assert_halted(result, ReconciliationStatus.UNEXPECTED_BROKER_POSITION)
    (mismatch,) = result.mismatches
    assert mismatch.symbol == "TSLA"
    assert mismatch.expected is None  # DB side: nothing
    assert mismatch.actual == Decimal("5")  # broker side: 5 shares


def test_db_expects_a_position_the_broker_does_not_have_halts() -> None:
    # Position closed out of band, or an exit fill we never recorded.
    result = _compare(broker_positions=(), expected_positions=(_expected("NVDA", "7"),))
    _assert_halted(result, ReconciliationStatus.MISSING_BROKER_POSITION)
    (mismatch,) = result.mismatches
    assert mismatch.symbol == "NVDA"
    assert mismatch.expected == Decimal("7")
    assert mismatch.actual is None


def test_same_symbol_different_quantity_halts_with_both_values() -> None:
    # Partial fill recorded on one side only, or a duplicate submission that
    # filled twice.
    result = _compare(
        broker_positions=(_pos("AAPL", "12"),),
        expected_positions=(_expected("AAPL", "10"),),
    )
    _assert_halted(result, ReconciliationStatus.QUANTITY_MISMATCH)
    (mismatch,) = result.mismatches
    assert mismatch.symbol == "AAPL"
    assert (mismatch.expected, mismatch.actual) == (Decimal("10"), Decimal("12"))


def test_fractional_share_difference_halts_no_quantity_tolerance() -> None:
    result = _compare(
        broker_positions=(_pos("AAPL", "10.000001"),),
        expected_positions=(_expected("AAPL", "10"),),
    )
    _assert_halted(result, ReconciliationStatus.QUANTITY_MISMATCH)


def test_zero_quantity_broker_position_is_reported_not_ignored() -> None:
    # Documented choice: never silently drop a broker row. A spurious halt is
    # cheap; hiding broker state is not.
    result = _compare(broker_positions=(_pos("AAPL", "0"),), expected_positions=())
    _assert_halted(result, ReconciliationStatus.UNEXPECTED_BROKER_POSITION)


def test_every_difference_is_reported_not_just_the_first() -> None:
    result = _compare(
        broker_positions=(_pos("AAPL", "12"), _pos("TSLA", "5")),
        expected_positions=(_expected("AAPL", "10"), _expected("NVDA", "7")),
        expected_cash=BROKER_CASH + Decimal("100.00"),
    )
    # Headline is the highest-priority kind...
    _assert_halted(result, ReconciliationStatus.UNEXPECTED_BROKER_POSITION)
    # ...but the postmortem sees all four differences.
    kinds = {(m.kind, m.symbol) for m in result.mismatches}
    assert kinds == {
        (ReconciliationStatus.UNEXPECTED_BROKER_POSITION, "TSLA"),
        (ReconciliationStatus.MISSING_BROKER_POSITION, "NVDA"),
        (ReconciliationStatus.QUANTITY_MISMATCH, "AAPL"),
        (ReconciliationStatus.CASH_MISMATCH, None),
    }


@pytest.mark.parametrize(
    ("broker", "expected", "status"),
    [
        (
            (_pos("AAPL", "1"), _pos("aapl", "2")),
            (),
            ReconciliationStatus.DUPLICATE_POSITION,
        ),
        (
            (),
            (_expected("AAPL", "1"), _expected("AAPL", "2")),
            ReconciliationStatus.DUPLICATE_POSITION,
        ),
    ],
)
def test_duplicate_symbol_on_either_side_halts_rather_than_merging(
    broker: tuple[Position, ...],
    expected: tuple[ExpectedPosition, ...],
    status: ReconciliationStatus,
) -> None:
    result = _compare(broker_positions=broker, expected_positions=expected)
    _assert_halted(result, status)
    assert result.mismatches[0].symbol == "AAPL"


# --------------------------------------------------------------------------- #
# Cash comparison and its boundary
# --------------------------------------------------------------------------- #


def test_cash_exactly_equal_is_clean() -> None:
    _assert_clean(_compare(expected_cash=BROKER_CASH))


def test_cash_difference_exactly_at_tolerance_is_clean() -> None:
    result = _compare(expected_cash=BROKER_CASH - DEFAULT_CASH_TOLERANCE)
    _assert_clean(result)
    assert result.cash_checked is True


def test_cash_one_cent_beyond_tolerance_halts() -> None:
    off_by = DEFAULT_CASH_TOLERANCE + Decimal("0.01")
    result = _compare(expected_cash=BROKER_CASH - off_by)
    _assert_halted(result, ReconciliationStatus.CASH_MISMATCH)
    (mismatch,) = result.mismatches
    assert mismatch.expected == BROKER_CASH - off_by
    assert mismatch.actual == BROKER_CASH


def test_cash_mismatch_is_symmetric_in_sign() -> None:
    off_by = DEFAULT_CASH_TOLERANCE + Decimal("0.01")
    _assert_halted(
        _compare(expected_cash=BROKER_CASH + off_by),
        ReconciliationStatus.CASH_MISMATCH,
    )


@pytest.mark.parametrize(
    "tolerance",
    [None, Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01")],
)
def test_unusable_cash_tolerance_halts(tolerance: Decimal | None) -> None:
    # Even with cash that would otherwise match: we cannot state the rule.
    result = _compare(expected_cash=BROKER_CASH, cash_tolerance=tolerance)
    _assert_halted(result, ReconciliationStatus.CASH_MISMATCH)
    assert result.cash_checked is False


# --------------------------------------------------------------------------- #
# Fail-closed: missing / non-finite inputs (nothing raises, nothing passes)
# --------------------------------------------------------------------------- #


def test_broker_positions_none_is_broker_unreadable() -> None:
    _assert_halted(
        _compare(broker_positions=None), ReconciliationStatus.BROKER_UNREADABLE
    )


def test_expected_positions_none_is_db_unreadable() -> None:
    _assert_halted(
        _compare(expected_positions=None), ReconciliationStatus.DB_UNREADABLE
    )


def test_broker_unreadable_outranks_db_unreadable() -> None:
    _assert_halted(
        _compare(broker_positions=None, expected_positions=None),
        ReconciliationStatus.BROKER_UNREADABLE,
    )


def test_empty_positions_are_not_treated_as_unreadable() -> None:
    # () means "flat" and is a real answer; None means "unknown". Never conflate.
    _assert_clean(_compare(broker_positions=(), expected_positions=()))


@pytest.mark.parametrize(
    "cash", [None, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_unreadable_broker_cash_halts_as_transient_not_drift(
    cash: Decimal | None,
) -> None:
    # We could not READ the figure -- that is not evidence the books differ, so
    # it must not demand an owner acknowledgement like real drift does.
    result = _compare(broker_cash=cash)
    _assert_halted(result, ReconciliationStatus.BROKER_UNREADABLE)
    assert result.cash_checked is False
    assert result.category is HaltCategory.TRANSIENT


@pytest.mark.parametrize("cash", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_expected_cash_halts(cash: Decimal) -> None:
    result = _compare(expected_cash=cash)
    _assert_halted(result, ReconciliationStatus.CASH_MISMATCH)
    assert result.cash_checked is False


@pytest.mark.parametrize("quantity", ["NaN", "Infinity"])
def test_broker_position_model_rejects_non_finite_quantity(quantity: str) -> None:
    # First line of defence: a NaN/Inf quantity never even parses out of the
    # broker wrapper -- it raises there, which the orchestrator maps to
    # BROKER_UNREADABLE. Pinned here so the model's fail-closed posture cannot
    # be loosened without this test failing.
    with pytest.raises(ValidationError):
        _pos("AAPL", Decimal(quantity))


@pytest.mark.parametrize("quantity", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_broker_quantity_halts(quantity: Decimal) -> None:
    # Second line of defence (structural stand-in, see _RawPosition): NaN would
    # compare "not equal" by luck and Inf arithmetic can raise -- neither may be
    # trusted to produce the verdict.
    result = _compare(
        broker_positions=(_RawPosition("AAPL", quantity),),
        expected_positions=(_expected("AAPL", "10"),),
    )
    _assert_halted(result, ReconciliationStatus.QUANTITY_MISMATCH)


@pytest.mark.parametrize("quantity", [Decimal("NaN"), Decimal("-Infinity")])
def test_non_finite_expected_quantity_halts(quantity: Decimal) -> None:
    result = _compare(
        broker_positions=(_pos("AAPL", "10"),),
        expected_positions=(_expected("AAPL", quantity),),
    )
    _assert_halted(result, ReconciliationStatus.QUANTITY_MISMATCH)


def test_non_finite_quantities_on_both_sides_halt() -> None:
    nan = Decimal("NaN")
    result = _compare(
        broker_positions=(_RawPosition("AAPL", nan),),
        expected_positions=(_expected("AAPL", nan),),
    )
    # NaN != NaN, so "equal" is never inferred from garbage.
    _assert_halted(result, ReconciliationStatus.QUANTITY_MISMATCH)


def test_pure_compare_never_raises_on_hostile_input() -> None:
    # Belt and braces: whatever the shape, the answer is a result object.
    result = compare_positions(
        broker_positions=(_RawPosition("", Decimal("NaN")),),
        expected_positions=(_expected("", Decimal("Infinity")),),
        broker_cash=Decimal("NaN"),
        expected_cash=Decimal("-Infinity"),
        cash_tolerance=Decimal("NaN"),
    )
    assert result.reconciled is False
    assert result.status is not ReconciliationStatus.CLEAN


# --------------------------------------------------------------------------- #
# Result invariants
# --------------------------------------------------------------------------- #


def test_result_is_frozen() -> None:
    result = _compare()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = ReconciliationStatus.CASH_MISMATCH  # type: ignore[misc]


def test_reconciled_cannot_be_overwritten_it_is_derived() -> None:
    # No stored flag to flip: `reconciled` is computed from `status`. (The exact
    # exception type differs between a read-only property and a frozen field --
    # what matters is that the assignment is refused.)
    result = _compare()
    with pytest.raises((AttributeError, TypeError)):
        result.reconciled = False  # type: ignore[misc]


def test_mismatch_is_frozen_and_log_safe() -> None:
    result = _compare(broker_positions=(_pos("TSLA", "5"),))
    (mismatch,) = result.mismatches
    with pytest.raises(dataclasses.FrozenInstanceError):
        mismatch.symbol = "X"  # type: ignore[misc]
    fields = mismatch.as_log_fields()
    assert fields["actual"] == "5"  # Decimal stringified, never float
    assert fields["kind"] == ReconciliationStatus.UNEXPECTED_BROKER_POSITION.value


def test_reconciled_is_true_only_for_clean_status() -> None:
    assert ReconciliationResult.cash_not_verified().reconciled is False
    assert ReconciliationResult.clean().reconciled is True


def test_log_fields_expose_the_latch_information() -> None:
    fields = _compare(
        broker_positions=(_pos("TSLA", "5"),), expected_positions=()
    ).as_log_fields()
    assert fields["status"] == "unexpected_broker_position"
    assert fields["category"] == "drift"
    assert fields["owner_must_clear"] is True
    assert fields["positions_reconciled"] is False
    assert fields["reconciled"] is False


def test_mask_account_id_shows_only_last_four() -> None:
    assert _mask_account_id("1234567890ABCD") == "***ABCD"
    assert _mask_account_id("AB") == "***"


# --------------------------------------------------------------------------- #
# Orchestrator: fakes (no network, no DB, no credentials)
# --------------------------------------------------------------------------- #


class FakeClient:
    """Stand-in WebullClient: returns a canned snapshot or raises."""

    def __init__(
        self,
        *,
        positions: tuple[Position, ...] = (),
        total_cash: Decimal | None = BROKER_CASH,
        error: Exception | None = None,
        is_live: bool = False,
    ) -> None:
        self._positions = positions
        self._total_cash = total_cash
        self._error = error
        self._is_live = is_live
        self.requested_ids: list[str] = []

    @property
    def is_live(self) -> bool:
        return self._is_live

    def get_account_snapshot(self, request: AccountSnapshotRequest) -> AccountSnapshot:
        self.requested_ids.append(request.account_id)
        if self._error is not None:
            raise self._error
        return AccountSnapshot(
            balance=AccountBalance(
                account_id=request.account_id,
                currency="USD",
                net_liquidation=Decimal("100000.00"),
                total_cash=self._total_cash,
                buying_power=Decimal("100000.00"),
                settled_funds=Decimal("100000.00"),
            ),
            positions=self._positions,
            captured_at=datetime.now(UTC),
        )


class FakeDB:
    """Stand-in Database exposing ONLY the read reconciliation is allowed to do.

    Any attempt to write would fail with AttributeError -- which is the point:
    reconciliation must never mutate anything.
    """

    def __init__(
        self,
        intents: dict[str, Decimal] | None = None,
        *,
        error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._intents = intents or {}
        self._error = error
        self._close_error = close_error
        self.calls: list[bool] = []
        self.closed = False
        self.close_attempted = False

    async def get_open_position_intents(
        self, *, is_paper: bool = True
    ) -> dict[str, Decimal]:
        self.calls.append(is_paper)
        if self._error is not None:
            raise self._error
        return dict(self._intents)

    async def close(self) -> None:
        self.close_attempted = True
        if self._close_error is not None:
            raise self._close_error
        self.closed = True


def _dummy_settings(*, account_id: str | None = "acct-000012345678") -> Settings:
    return Settings(
        webull_app_key="dummy-key",
        webull_app_secret="dummy-secret",
        webull_env=WebullEnv.PAPER,
        webull_paper_api_endpoint="api.sandbox.example.com",
        webull_account_id=account_id,
        anthropic_api_key="dummy-anthropic",
        supabase_url="https://dummy.supabase.co",
        supabase_anon_key="dummy-anon",
        supabase_service_role_key="dummy-service",
        database_url="postgresql://dummy",
    )


async def _run(
    client: FakeClient, db: FakeDB, *, account_id: str | None = "acct-000012345678"
) -> ReconciliationResult:
    return await reconcile(
        settings=_dummy_settings(account_id=account_id),
        client=client,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Orchestrator behaviour
# --------------------------------------------------------------------------- #


async def test_reconcile_happy_path_reads_the_pinned_account_only() -> None:
    client = FakeClient()
    db = FakeDB({})

    result = await _run(client, db)

    # Best achievable outcome TODAY: positions verified, cash never compared
    # (no DB cash ledger), so NOT reconciled -- and therefore not trade-
    # permitting. This is the owner's ruling, not a bug.
    assert result.status is ReconciliationStatus.CASH_NOT_VERIFIED
    assert result.reconciled is False
    assert result.positions_reconciled is True
    assert result.owner_must_clear is False
    assert client.requested_ids == ["acct-000012345678"]  # exactly one account
    assert db.calls == [True]  # is_paper derived from the client, not hardcoded


async def test_reconcile_broker_failure_is_broker_unreadable() -> None:
    client = FakeClient(error=RuntimeError("webull exploded"))
    db = FakeDB({})

    result = await _run(client, db)

    _assert_halted(result, ReconciliationStatus.BROKER_UNREADABLE)
    assert db.calls == []  # never even asked the DB once broker truth is unknown


async def test_reconcile_db_failure_is_db_unreadable() -> None:
    client = FakeClient()
    db = FakeDB(error=RuntimeError("db exploded"))

    result = await _run(client, db)

    _assert_halted(result, ReconciliationStatus.DB_UNREADABLE)


async def test_reconcile_unpinned_account_halts_without_touching_the_broker() -> None:
    client = FakeClient()
    db = FakeDB({})

    result = await _run(client, db, account_id=None)

    _assert_halted(result, ReconciliationStatus.ACCOUNT_NOT_PINNED)
    assert client.requested_ids == []  # never guesses which account to reconcile
    assert db.calls == []


async def test_reconcile_blank_account_id_halts() -> None:
    result = await _run(FakeClient(), FakeDB({}), account_id="   ")
    _assert_halted(result, ReconciliationStatus.ACCOUNT_NOT_PINNED)


async def test_reconcile_surfaces_an_out_of_band_broker_position() -> None:
    # End-to-end version of the headline drift case: someone traded manually.
    client = FakeClient(positions=(_pos("TSLA", "5"),))
    db = FakeDB({})

    result = await _run(client, db)

    _assert_halted(result, ReconciliationStatus.UNEXPECTED_BROKER_POSITION)
    (mismatch,) = result.mismatches
    assert (mismatch.symbol, mismatch.actual) == ("TSLA", Decimal("5"))


async def test_reconcile_matches_db_intent_against_broker_truth() -> None:
    client = FakeClient(positions=(_pos("AAPL", "10"),))
    db = FakeDB({"AAPL": Decimal("10")})

    result = await _run(client, db)

    assert result.positions_reconciled is True  # the positions leg agreed...
    assert result.status is ReconciliationStatus.CASH_NOT_VERIFIED
    assert result.reconciled is False  # ...but a partial run is not reconciled


async def test_reconcile_never_returns_reconciled_true_while_cash_is_unverified() -> (
    None
):
    # The end-to-end version of the structural guarantee: with no cash ledger
    # wired, NOTHING the orchestrator can observe produces a trade-permitting
    # verdict.
    for client, db in (
        (FakeClient(), FakeDB({})),
        (FakeClient(positions=(_pos("AAPL", "10"),)), FakeDB({"AAPL": Decimal("10")})),
        (FakeClient(positions=(_pos("TSLA", "5"),)), FakeDB({})),
        (FakeClient(total_cash=None), FakeDB({})),
    ):
        result = await _run(client, db)
        assert result.reconciled is False
        assert result.cash_checked is False


async def test_reconcile_flags_a_quantity_difference_from_db_intent() -> None:
    client = FakeClient(positions=(_pos("AAPL", "10"),))
    db = FakeDB({"AAPL": Decimal("4")})  # e.g. only the partial fill was recorded

    result = await _run(client, db)

    _assert_halted(result, ReconciliationStatus.QUANTITY_MISMATCH)
    (mismatch,) = result.mismatches
    assert (mismatch.expected, mismatch.actual) == (Decimal("4"), Decimal("10"))


async def test_reconcile_unreadable_broker_cash_halts_as_transient() -> None:
    client = FakeClient(total_cash=None)
    db = FakeDB({})

    result = await _run(client, db)

    _assert_halted(result, ReconciliationStatus.BROKER_UNREADABLE)
    assert result.owner_must_clear is False


async def test_reconcile_drift_is_flagged_sticky_for_the_caller() -> None:
    client = FakeClient(positions=(_pos("TSLA", "5"),))

    result = await _run(client, FakeDB({}))

    # THE LATCH RULE: the scheduler must not let this auto-clear.
    assert result.category is HaltCategory.DRIFT
    assert result.owner_must_clear is True


async def test_reconcile_scopes_db_intents_to_the_live_environment() -> None:
    client = FakeClient(is_live=True)
    db = FakeDB({})

    await _run(client, db)

    assert db.calls == [False]  # live client -> live trades, never paper rows


async def test_reconcile_leaves_an_injected_db_open() -> None:
    db = FakeDB({})
    await _run(FakeClient(), db)
    assert db.closed is False  # caller owns an injected db
    assert db.close_attempted is False


# --------------------------------------------------------------------------- #
# Orchestrator: the pool it opens itself (N3 -- close() must not eat the verdict)
# --------------------------------------------------------------------------- #


class _FakeDatabaseFactory:
    """Stands in for the `Database` class inside the reconciliation module."""

    def __init__(self, db: FakeDB | None, *, connect_error: Exception | None = None):
        self._db = db
        self._connect_error = connect_error

    async def connect(self, dsn: str) -> FakeDB:
        if self._connect_error is not None:
            raise self._connect_error
        assert self._db is not None
        return self._db


async def test_reconcile_db_close_failure_does_not_replace_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failing close() in `finally` would otherwise raise straight over the
    # computed result -- breaking "reconcile never raises" and losing the
    # verdict. A torn-down pool says nothing about whether the books matched.
    db = FakeDB({}, close_error=RuntimeError("pool teardown failed"))
    monkeypatch.setattr(
        reconciliation_module, "Database", _FakeDatabaseFactory(db)
    )

    result = await reconcile(settings=_dummy_settings(), client=FakeClient())  # type: ignore[arg-type]

    assert db.close_attempted is True  # it really did try to close
    assert result.status is ReconciliationStatus.CASH_NOT_VERIFIED
    assert result.reconciled is False


async def test_reconcile_db_connect_failure_is_db_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconciliation_module,
        "Database",
        _FakeDatabaseFactory(None, connect_error=RuntimeError("no route to host")),
    )

    result = await reconcile(settings=_dummy_settings(), client=FakeClient())  # type: ignore[arg-type]

    _assert_halted(result, ReconciliationStatus.DB_UNREADABLE)
    assert result.owner_must_clear is False  # transient: may auto-clear
