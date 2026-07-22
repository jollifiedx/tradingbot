"""Tests for the worker lifecycle + scheduler.

The latch (`app/worker/latch.py`) is pure and already exhaustively tested. It can
only be defeated by its CALLER, so this file is organised as one test (or more)
per way a careless scheduler defeats it -- the architect's list, which is the
acceptance criteria for this module. Each test asserts the SAFE behaviour, not
merely the absence of an exception.

The load-bearing test is `test_system_level_drift_then_clean_stays_halted`
(architect D2, and the requirement CLAUDE.md left open when the reconciliation
module landed): drift -> the freeze write ACTUALLY lands in the settings store
-> the next tick reads it back from the store and still refuses to trade -> a
fresh process reads it back and still refuses. Nothing is passed in by hand.

No network, no database, no wall clock: the store, the reconciler, the snapshot
job, the market calendar and the clock are all fakes.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from app.core.db import Database, DatabaseError
from app.core.models import BotSettings
from app.worker import scheduler as scheduler_module
from app.worker.latch import LatchDecision, LatchReason
from app.worker.market_hours import MarketClock
from app.worker.reconciliation import (
    Mismatch,
    ReconciliationResult,
    ReconciliationStatus,
    compare_positions,
)
from app.worker.scheduler import (
    HALT_REASON_FREEZE_WRITE_FAILED,
    HALT_REASON_LATCH_ERROR,
    HALT_REASON_SETTINGS_UNREADABLE,
    JOB_EQUITY_SNAPSHOT,
    JOB_POSTURE,
    JOB_RECONCILE,
    SchedulerConfig,
    SettingsStore,
    Worker,
    WorkerState,
)

T0 = datetime(2026, 7, 21, 15, 0, tzinfo=UTC)  # a Tuesday, mid-session


# --------------------------------------------------------------------------
# Fakes.
# --------------------------------------------------------------------------


class FakeSettingsStore:
    """Stands in for the `settings` row AND the DB's one-way freeze guarantee.

    Deliberately models the database's behaviour, not the worker's wishes:
    `engage_system_freeze` takes no arguments, can only ever set `frozen` to
    True, and -- like migration 20260721000001's trigger -- there is no code
    path here that could set it back to False. If the worker ever needs an
    unfreeze to make a test pass, the test cannot be written.
    """

    def __init__(self, *, frozen: bool = False, readable: bool = True) -> None:
        self.frozen = frozen
        self.readable = readable
        self.reads = 0
        self.freeze_writes = 0
        self.write_failures_remaining = 0
        # A write that returns without error but leaves `frozen` false -- the
        # silent-failure shape the worker must judge on the returned row.
        self.writes_take_effect = True

    async def get_settings(self) -> BotSettings:
        self.reads += 1
        if not self.readable:
            raise DatabaseError("failed to read settings")
        return _bot_settings(frozen=self.frozen)

    async def engage_system_freeze(self) -> BotSettings:
        if self.write_failures_remaining > 0:
            self.write_failures_remaining -= 1
            raise DatabaseError("failed to engage the system freeze")
        self.freeze_writes += 1
        if not self.writes_take_effect:
            return _bot_settings(frozen=self.frozen)
        self.frozen = True
        return _bot_settings(frozen=True)


def _bot_settings(*, frozen: bool) -> BotSettings:
    """A real `BotSettings` model, so the worker reads the real field."""
    return BotSettings(
        id=True,
        frozen=frozen,
        buy_power_cap=Decimal("0.00"),
        max_daily_loss=Decimal("0.00"),
        max_per_trade_cap=Decimal("0.00"),
        staleness_threshold_seconds=30,
        updated_at=T0,
        updated_by=None,
    )


class FakeReconciler:
    """An injectable stand-in for `reconcile()`, with concurrency bookkeeping."""

    def __init__(self, result: ReconciliationResult | None = None) -> None:
        self.result = result
        self.error: Exception | None = None
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> ReconciliationResult:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            if self.result is None:
                raise AssertionError("FakeReconciler needs a result or an error")
            return self.result
        finally:
            self.in_flight -= 1


class FakeSnapshot:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return object()


class FakeMarketCalendar:
    """Market hours under test control. The real calendar has its own suite."""

    def __init__(self, *, open_now: bool = True, close: datetime | None = None) -> None:
        self.open_now = open_now
        self.close = close

    @property
    def name(self) -> str:
        return "FAKE"

    def is_open(self, when: datetime) -> bool:
        return self.open_now

    def previous_close(self, when: datetime) -> datetime | None:
        return self.close


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _no_sleep(seconds: float) -> None:
    return None


# -- reconciliation outcomes (same fixtures the latch tests use) ------------


def _clean() -> ReconciliationResult:
    """A fully verified run -- positions AND cash compared.

    NOTE (architect N-h): this outcome is not reachable end to end today. The
    real `reconcile()` always passes `expected_cash=None` because no DB cash
    ledger exists, so production's best case is CASH_NOT_VERIFIED and the worker
    is permanently halted -- see `test_todays_real_posture_is_permanent_halt`.
    Every `may_trade is True` assertion below is therefore a test of the
    *mechanism* for the day the cash ledger lands, not a claim about today.
    """
    return ReconciliationResult.clean()


def _drift() -> ReconciliationResult:
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


def _cash_not_verified() -> ReconciliationResult:
    """Positions matched; cash was never compared. `positions_reconciled` is True."""
    return ReconciliationResult.cash_not_verified()


def _build(
    store: FakeSettingsStore | None = None,
    *,
    reconciler: FakeReconciler | None = None,
    snapshot: FakeSnapshot | None = None,
    market: FakeMarketCalendar | None = None,
    clock: FakeClock | None = None,
    config: SchedulerConfig | None = None,
) -> tuple[Worker, FakeSettingsStore, FakeReconciler, FakeClock]:
    store = store if store is not None else FakeSettingsStore()
    reconciler = reconciler if reconciler is not None else FakeReconciler(_clean())
    clock = clock if clock is not None else FakeClock()
    worker = Worker(
        db=store,
        reconcile_fn=reconciler,
        snapshot_fn=snapshot if snapshot is not None else FakeSnapshot(),
        market_clock=market if market is not None else FakeMarketCalendar(),
        config=config if config is not None else SchedulerConfig(),
        now_fn=clock,
        sleep_fn=_no_sleep,
    )
    return worker, store, reconciler, clock


# ==========================================================================
# THE REQUIRED SYSTEM TEST (architect D2).
# ==========================================================================


async def test_system_level_drift_then_clean_stays_halted() -> None:
    """Drift latches THROUGH THE STORE, and a clean run does not release it.

    Nothing is passed in by hand: tick 1 observes drift and the assertion is
    that the write reached the settings store; tick 2 gets a perfectly CLEAN
    reconciliation and refuses to trade *because it read the store back*. The
    only thing connecting the two ticks is the persisted flag -- which is the
    entire point of the owner's 2026-07-21 latch ruling.
    """
    worker, store, reconciler, _ = _build()
    assert store.frozen is False

    # Tick 1: a real disagreement between broker and books.
    reconciler.result = _drift()
    await worker.run_reconcile()
    await worker.tick()

    assert store.freeze_writes == 1, "the freeze must be WRITTEN, not just decided"
    assert store.frozen is True, "the halt must exist in the store, not in memory"
    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.DRIFT_HALT

    # Tick 2: the books now agree perfectly. It must change nothing.
    reads_before = store.reads
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()

    assert store.reads > reads_before, "tick 2 must re-read settings, not cache it"
    assert worker.state.may_trade is False, "a clean run must not release the latch"
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.FROZEN, (
        "the halt must be attributed to the PERSISTED flag -- that is the only "
        "thing connecting the drift we saw to the order we refuse"
    )
    assert store.frozen is True


async def test_halt_survives_a_process_restart() -> None:
    """A fresh worker against the same store reads frozen=true and stays halted."""
    worker, store, reconciler, _ = _build()
    reconciler.result = _drift()
    await worker.run_reconcile()
    await worker.tick()
    assert store.frozen is True

    # Simulate the supervisor restarting the process: brand-new Worker and
    # brand-new WorkerState, same database. Reconciliation now comes back clean.
    restarted, _, restarted_reconciler, _ = _build(store)
    restarted_reconciler.result = _clean()
    assert restarted.state.may_trade is False, "a fresh worker is born HALTED"

    await restarted.startup()

    assert restarted.state.may_trade is False
    assert restarted.state.decision is not None
    assert restarted.state.decision.reason is LatchReason.FROZEN
    assert restarted.trading_jobs_registered is False
    assert store.frozen is True, "the restart must never clear the flag"


# ==========================================================================
# 1. Re-read `settings` at the top of EVERY tick; never cache the flag.
# ==========================================================================


async def test_every_tick_reads_settings_again() -> None:
    worker, store, _, _ = _build()
    await worker.tick()
    after_one = store.reads
    await worker.tick()
    after_two = store.reads
    await worker.tick()
    assert after_one >= 1
    assert after_two - after_one == 1
    assert store.reads - after_two == 1


async def test_owner_freeze_mid_session_is_seen_on_the_next_tick() -> None:
    """The freeze the owner sets at 11:00 must stop the 11:00:30 tick."""
    worker, store, reconciler, _ = _build()
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True

    store.frozen = True  # the owner hits the switch in the dashboard

    await worker.tick()
    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.FROZEN


def test_worker_state_has_no_slot_to_cache_the_freeze_flag() -> None:
    """A tripwire, not a style check.

    Pinning the slot set means any new field -- `_frozen`, `_settings`,
    `_last_settings` -- breaks this test and forces a deliberate conversation.
    A cached freeze flag is how a worker stops noticing that it has been frozen.
    """
    assert set(WorkerState.__slots__) == {
        "_decided_at",
        "_decision",
        "_freeze_write_pending",
        "_latch_error",
        "_max_decision_age",
        "_max_result_age",
        "_now",
        "_result",
        "_result_at",
    }
    # Every slot above is either a verdict, its timestamp, an age bound, the
    # clock, or a sticky debt. None of them is a settings/freeze cache -- that
    # is the whole point of pinning the set.
    assert not any(
        "frozen" in slot or "settings" in slot for slot in WorkerState.__slots__
    )


# ==========================================================================
# 2. engage_freeze must actually WRITE. 3. A failed write is never swallowed.
# ==========================================================================


async def test_engage_freeze_persists_to_the_store() -> None:
    worker, store, reconciler, _ = _build()
    reconciler.result = _drift()
    await worker.run_reconcile()
    await worker.tick()
    assert store.freeze_writes == 1
    assert store.frozen is True


async def test_failed_freeze_write_refuses_to_trade_and_keeps_retrying() -> None:
    """The write fails; the halt must not quietly become advisory.

    The nasty part is the SECOND tick: the store still says frozen=false (the
    write never landed) and reconciliation now comes back clean, so the latch
    itself says CLEAR. The worker must still refuse, because it knows it owes
    the database a halt.
    """
    worker, store, reconciler, _ = _build()
    store.write_failures_remaining = 99
    reconciler.result = _drift()

    with capture_logs() as logs:
        await worker.run_reconcile()
        await worker.tick()

    assert store.frozen is False, "fixture check: the write really did fail"
    assert worker.state.freeze_write_pending is True
    assert worker.state.may_trade is False
    critical = [
        entry
        for entry in logs
        if entry["log_level"] == "critical"
        and entry.get("halt_reason") == HALT_REASON_FREEZE_WRITE_FAILED
    ]
    assert critical, "an unpersisted halt must be CRITICAL with a halt_reason"

    # Second tick: latch would say CLEAR (unfrozen store + clean books).
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.decision is not None
    assert worker.state.decision.may_trade is True, (
        "fixture check: the latch really does say CLEAR here"
    )
    assert worker.state.may_trade is False, (
        "an owed-but-unwritten halt must outrank the latch's verdict"
    )

    # Third tick: the database comes back; the retry lands and the flag sticks.
    store.write_failures_remaining = 0
    await worker.tick()
    assert store.frozen is True
    assert store.freeze_writes == 1
    assert worker.state.freeze_write_pending is False
    assert worker.state.may_trade is False, "now halted by the persisted flag"
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.FROZEN


async def test_freeze_write_is_retried_within_a_single_tick() -> None:
    worker, store, reconciler, _ = _build(
        config=SchedulerConfig(freeze_write_attempts=3, freeze_write_backoff_seconds=0)
    )
    store.write_failures_remaining = 2  # fails twice, succeeds on attempt 3
    reconciler.result = _drift()
    await worker.run_reconcile()
    await worker.tick()
    assert store.frozen is True
    assert worker.state.freeze_write_pending is False


async def test_a_freeze_write_that_does_not_set_the_flag_counts_as_failed() -> None:
    """Judge the write, not the call (architect B-1.3).

    A statement that returned a row whose `frozen` is still false halted
    nothing. Believing the absence of an exception would leave the worker
    convinced of a freeze that does not exist -- and, worse, clear the pending
    debt that is the only thing still refusing trades.
    """
    worker, store, reconciler, _ = _build(
        config=SchedulerConfig(freeze_write_attempts=2, freeze_write_backoff_seconds=0)
    )
    store.writes_take_effect = False
    reconciler.result = _drift()

    await worker.run_reconcile()
    await worker.tick()

    assert store.freeze_writes == 2, "each unconfirmed write is retried"
    assert store.frozen is False
    assert worker.state.freeze_write_pending is True
    assert worker.state.may_trade is False

    # It clears only once a write actually takes effect.
    store.writes_take_effect = True
    await worker.tick()
    assert store.frozen is True
    assert worker.state.freeze_write_pending is False


async def test_a_clean_reconciliation_never_clears_an_owed_freeze() -> None:
    """Architect B-1.2: only a confirmed persisted freeze clears the debt."""
    worker, store, reconciler, _ = _build(
        config=SchedulerConfig(freeze_write_attempts=1, freeze_write_backoff_seconds=0)
    )
    store.write_failures_remaining = 99
    reconciler.result = _drift()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.freeze_write_pending is True

    for _ in range(5):
        reconciler.result = _clean()
        await worker.run_reconcile()
        await worker.tick()
        assert worker.state.freeze_write_pending is True
        assert worker.state.may_trade is False


async def test_a_latch_that_raises_becomes_a_halt_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architect N-a: decide_posture is not structurally incapable of raising.

    A future ReconciliationStatus with no HaltCategory mapping would KeyError
    through `result.category`. That must halt the tick, not kill the process.
    """

    def _boom(*, result: object, currently_frozen: object) -> LatchDecision:
        raise KeyError("unmapped ReconciliationStatus")

    worker, store, reconciler, _ = _build()
    reconciler.result = _clean()
    await worker.run_reconcile()
    monkeypatch.setattr(scheduler_module, "decide_posture", _boom)

    with capture_logs() as logs:
        await worker.tick()  # must not raise

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.TRANSIENT_HALT
    assert store.freeze_writes == 0, "a code defect is not evidence of drift"
    assert any(entry["event"] == "worker.latch_raised" for entry in logs)


async def test_a_tick_never_raises_even_when_everything_fails() -> None:
    """A crash here is a supervisor restart into "no opinion" -- the worst state.

    Settings unreadable, reconciliation raising, freeze write failing: the tick
    must absorb all three, stay halted, and return.
    """
    worker, store, reconciler, _ = _build()
    store.readable = False
    store.write_failures_remaining = 99
    reconciler.error = RuntimeError("everything is on fire")
    reconciler.result = None

    await worker.startup()  # must not raise

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.SETTINGS_UNREADABLE


async def test_drift_seen_while_settings_are_unreadable_is_still_persisted() -> None:
    """Architect B1, end to end: the evidence must not be dropped.

    The latch already returns engage_freeze=True here; this proves the SCHEDULER
    acts on it rather than only acting when the reason happens to be DRIFT_HALT.
    """
    worker, store, reconciler, _ = _build()
    store.readable = False
    reconciler.result = _drift()

    await worker.run_reconcile()
    await worker.tick()

    assert store.frozen is True
    assert store.freeze_writes == 1
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.SETTINGS_UNREADABLE


# ==========================================================================
# 4. Never reuse a stale ReconciliationResult. 5. No last-known-good fallback.
# ==========================================================================


async def test_a_reconciliation_older_than_the_interval_is_not_reused() -> None:
    config = SchedulerConfig(reconcile_interval_seconds=300)
    worker, _, reconciler, clock = _build(config=config)
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True

    clock.advance(331)  # beyond interval + grace: a genuinely missed run
    await worker.tick()

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.NO_RECONCILIATION


async def test_a_result_exactly_at_the_interval_is_still_usable() -> None:
    """The boundary is stated once, here, so it cannot drift silently."""
    config = SchedulerConfig(reconcile_interval_seconds=300)
    worker, _, reconciler, clock = _build(config=config)
    reconciler.result = _clean()
    await worker.run_reconcile()
    clock.advance(330)  # exactly at interval + grace
    await worker.tick()
    assert worker.state.may_trade is True


async def test_result_is_aged_from_when_the_reconcile_started() -> None:
    """A reconcile that took 90s describes a 90s-old world."""
    config = SchedulerConfig(reconcile_interval_seconds=100)
    worker, _, reconciler, clock = _build(config=config)
    reconciler.result = _clean()

    gate = asyncio.Event()
    reconciler.gate = gate
    task = asyncio.create_task(worker.run_reconcile())
    await asyncio.sleep(0)
    clock.advance(90)  # the broker call is slow
    gate.set()
    await task
    reconciler.gate = None

    clock.advance(41)  # 131s since the run STARTED, past 100 + 30 grace
    await worker.tick()
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.NO_RECONCILIATION


async def test_a_raising_reconcile_clears_the_stored_result() -> None:
    """No fall back to last-known-good: an exception is not evidence."""
    worker, _, reconciler, _ = _build()
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True

    reconciler.error = RuntimeError("broker timed out")
    await worker.run_reconcile()
    await worker.tick()

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.NO_RECONCILIATION


async def test_reconcile_is_skipped_outside_market_hours_and_the_result_ages_out() -> (
    None
):
    """Closed market -> no reconciliation -> no opinion -> no trading."""
    market = FakeMarketCalendar(open_now=True)
    config = SchedulerConfig(reconcile_interval_seconds=300)
    worker, _, reconciler, clock = _build(market=market, config=config)
    reconciler.result = _clean()
    await worker.reconcile_job()
    await worker.tick()
    assert worker.state.may_trade is True
    assert reconciler.calls == 1

    market.open_now = False
    clock.advance(331)  # past interval + misfire grace
    await worker.reconcile_job()
    await worker.tick()

    assert reconciler.calls == 1, "the calendar, not the code, decides when we run"
    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.NO_RECONCILIATION


async def test_the_real_calendar_closes_the_reconcile_job_on_a_weekend() -> None:
    """One end-to-end pass with the REAL calendar, not the fake.

    Everything else here drives market hours through a stand-in; this proves the
    wiring to `exchange_calendars` is real and that the default worker gets it.
    """
    clock = FakeClock(datetime(2026, 7, 25, 17, 0, tzinfo=UTC))  # Saturday, 13:00 ET
    reconciler = FakeReconciler(_clean())
    weekend_worker = Worker(
        db=FakeSettingsStore(),
        reconcile_fn=reconciler,
        snapshot_fn=FakeSnapshot(),
        market_clock=MarketClock(),
        now_fn=clock,
        sleep_fn=_no_sleep,
    )
    await weekend_worker.reconcile_job()
    assert reconciler.calls == 0
    assert weekend_worker.state.may_trade is False


# ==========================================================================
# 6/7. Consume ONLY the LatchDecision. Never gate on reconciliation diagnostics.
# ==========================================================================


async def test_cash_not_verified_does_not_permit_trading() -> None:
    """`positions_reconciled` is True on this run -- gating on it would trade.

    This is the exact result every run produces today (there is no DB cash
    ledger), so a scheduler that gated on the positions bit would be trading
    with cash unverified from day one.
    """
    result = _cash_not_verified()
    assert result.positions_reconciled is True, "fixture check"
    assert result.reconciled is False

    worker, _, reconciler, _ = _build()
    reconciler.result = result
    await worker.run_reconcile()
    await worker.tick()

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.CASH_NOT_VERIFIED


async def test_todays_real_posture_is_permanent_halt() -> None:
    """Architect N-h: permanent halt is CORRECT today; do not work around it.

    This builds the result from the pure comparison using the EXACT arguments
    production passes -- a perfect account (books and broker agree, nothing
    held) with `expected_cash=None`, because no DB cash ledger exists. The best
    achievable posture is a halt, and it must stay that way until the order path
    lands a cash ledger. A future edit that makes this test report `may_trade`
    is a regression, not a fix.
    """
    best_possible = compare_positions(
        broker_positions=(),
        expected_positions=(),
        broker_cash=Decimal("10000.00"),
        expected_cash=None,
    )
    assert best_possible.status is ReconciliationStatus.CASH_NOT_VERIFIED

    worker, store, reconciler, _ = _build()
    reconciler.result = best_possible
    await worker.run_reconcile()
    await worker.tick()

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.CASH_NOT_VERIFIED
    assert store.freeze_writes == 0, "nothing for the owner to clear"
    assert worker.trading_jobs_registered is False


async def test_transient_halt_does_not_freeze_but_does_not_trade() -> None:
    worker, store, reconciler, _ = _build()
    reconciler.result = _transient()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is False
    assert store.freeze_writes == 0, "a blip must not latch"
    assert store.frozen is False

    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True, "a transient failure may auto-clear"


def test_the_scheduler_never_reads_reconciliation_diagnostics() -> None:
    """AST tripwire for the two hazards that read as "reconciled" but are not.

    `positions_reconciled` is True on every CASH_NOT_VERIFIED run, and
    `result.reconciled` is not the latch verdict -- only the persisted
    `settings.frozen` connects a past drift to a future order. Neither name may
    appear as an attribute access in this module.
    """
    forbidden = {"reconciled", "positions_reconciled", "cash_checked"}
    found = {
        node.attr
        for node in ast.walk(_scheduler_ast())
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
    assert found == set(), f"scheduler must not read {sorted(found)}"


def test_the_scheduler_gates_on_may_trade_not_on_the_reason() -> None:
    """`reason` may be logged, never branched on."""
    branch_tests: list[ast.AST] = [
        node.test
        for node in ast.walk(_scheduler_ast())
        if isinstance(node, ast.If | ast.IfExp)
    ]
    reasons_in_branches = {
        sub.attr
        for test in branch_tests
        for sub in ast.walk(test)
        if isinstance(sub, ast.Attribute) and sub.attr == "reason"
    }
    assert reasons_in_branches == set(), "posture must never branch on the reason"


def test_may_trade_requires_identity_true_not_truthiness() -> None:
    """A truthy-but-not-True verdict must not permit trading."""

    class _Truthy:
        may_trade = 1
        engage_freeze = False
        reason = LatchReason.CLEAR

    state = WorkerState(
        max_result_age=timedelta(seconds=300),
        max_decision_age=timedelta(seconds=60),
    )
    state.record_decision(_Truthy())  # type: ignore[arg-type]
    assert state.may_trade is False


def test_a_fresh_state_is_born_halted() -> None:
    state = WorkerState(
        max_result_age=timedelta(seconds=300),
        max_decision_age=timedelta(seconds=60),
    )
    assert state.decision is None
    assert state.may_trade is False


def test_state_permits_only_on_a_real_clear_decision() -> None:
    state = WorkerState(
        max_result_age=timedelta(seconds=300),
        max_decision_age=timedelta(seconds=60),
    )
    state.record_decision(
        LatchDecision(may_trade=True, engage_freeze=False, reason=LatchReason.CLEAR)
    )
    assert state.may_trade is True
    state.mark_freeze_write_failed()
    assert state.may_trade is False, "an owed freeze outranks a CLEAR decision"


# ==========================================================================
# 8. Unreadable settings -> None, NEVER False.
# ==========================================================================


async def test_unreadable_settings_halts_and_is_never_read_as_unfrozen() -> None:
    worker, store, reconciler, _ = _build()
    store.readable = False
    reconciler.result = _clean()

    with capture_logs() as logs:
        await worker.run_reconcile()
        await worker.tick()

    assert worker.state.may_trade is False
    assert worker.state.decision is not None
    assert worker.state.decision.reason is LatchReason.SETTINGS_UNREADABLE
    assert any(
        entry.get("halt_reason") == HALT_REASON_SETTINGS_UNREADABLE for entry in logs
    )


async def test_read_frozen_returns_none_on_failure_not_false() -> None:
    """Directly, so the distinction cannot be lost behind the latch."""
    worker, store, _, _ = _build()
    store.readable = False
    assert await worker._read_frozen() is None  # noqa: SLF001 -- the contract
    store.readable = True
    assert await worker._read_frozen() is False  # noqa: SLF001
    store.frozen = True
    assert await worker._read_frozen() is True  # noqa: SLF001


# ==========================================================================
# 10. max_instances=1, tight misfire grace, and no overlapping runs.
# ==========================================================================


def test_every_job_is_single_instance_with_a_tight_misfire_grace() -> None:
    config = SchedulerConfig()
    worker, _, _, _ = _build(config=config)
    scheduler = worker.build_scheduler()
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {JOB_POSTURE, JOB_RECONCILE, JOB_EQUITY_SNAPSHOT}
    for job in jobs.values():
        assert job.max_instances == 1, f"{job.id} may never run twice at once"
        assert job.coalesce is True, f"{job.id} must not replay missed runs"
        assert job.misfire_grace_time is not None, (
            f"{job.id} must not run indefinitely late (None = no limit)"
        )
        assert job.misfire_grace_time == config.misfire_grace_seconds
    assert config.misfire_grace_seconds <= config.reconcile_interval_seconds
    assert scheduler.running is False, "build_scheduler must not start anything"


async def test_the_lock_serialises_overlapping_reconciles() -> None:
    worker, _, reconciler, _ = _build()
    reconciler.result = _clean()
    gate = asyncio.Event()
    reconciler.gate = gate

    first = asyncio.create_task(worker.run_reconcile())
    second = asyncio.create_task(worker.run_reconcile())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert reconciler.calls == 2
    assert reconciler.max_in_flight == 1, "two reconciles must never overlap"


async def test_a_tick_cannot_run_while_a_reconcile_holds_the_lock() -> None:
    """One tick writing a freeze while another decides it may trade: never."""
    worker, _, reconciler, _ = _build()
    reconciler.result = _clean()
    gate = asyncio.Event()
    reconciler.gate = gate

    reconcile_task = asyncio.create_task(worker.run_reconcile())
    await asyncio.sleep(0)
    tick_task = asyncio.create_task(worker.tick())
    await asyncio.sleep(0)
    assert worker.state.decision is None, "the tick must be waiting for the lock"
    gate.set()
    await asyncio.gather(reconcile_task, tick_task)
    assert worker.state.decision is not None


# ==========================================================================
# 11. Nothing in the worker may ever write frozen=false.
# ==========================================================================


def test_the_worker_cannot_reach_any_settings_write_but_the_one_way_freeze() -> None:
    """The worker's whole view of the database is two methods.

    `update_settings` -- the only method that can write frozen=false -- is not
    on the protocol, so a worker-side unfreeze is a type error before it can be
    a runtime bug. `Database` must keep satisfying the protocol.
    """
    methods = {
        name
        for name, value in vars(SettingsStore).items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"get_settings", "engage_system_freeze"}
    assert hasattr(Database, "get_settings")
    assert hasattr(Database, "engage_system_freeze")


def test_the_scheduler_calls_no_other_database_method() -> None:
    """AST tripwire: every `self._db.<x>()` call in the module, enumerated."""
    called = {
        node.func.attr
        for node in ast.walk(_scheduler_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_db"
    }
    assert called == {"get_settings", "engage_system_freeze"}


def test_the_scheduler_never_mentions_update_settings() -> None:
    calls = {
        node.func.attr
        for node in ast.walk(_scheduler_ast())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "update_settings" not in calls


async def test_the_freeze_is_never_written_when_the_latch_did_not_ask() -> None:
    worker, store, reconciler, _ = _build()
    for result in (_clean(), _transient(), _cash_not_verified()):
        reconciler.result = result
        await worker.run_reconcile()
        await worker.tick()
    assert store.freeze_writes == 0
    assert store.frozen is False


# ==========================================================================
# Startup: born halted; no trading work scheduled unless the posture is CLEAR.
# ==========================================================================


async def test_startup_with_a_clean_run_registers_trading_jobs() -> None:
    worker, _, reconciler, _ = _build()
    reconciler.result = _clean()
    scheduler = await worker.start()
    try:
        assert worker.state.may_trade is True
        assert worker.trading_jobs_registered is True
        assert {job.id for job in scheduler.get_jobs()} == {
            JOB_POSTURE,
            JOB_RECONCILE,
            JOB_EQUITY_SNAPSHOT,
        }
    finally:
        scheduler.shutdown(wait=False)


async def test_startup_with_unreadable_settings_schedules_no_trading_work() -> None:
    worker, store, reconciler, _ = _build()
    store.readable = False
    reconciler.result = _clean()
    scheduler = await worker.start()
    try:
        assert worker.state.may_trade is False
        assert worker.trading_jobs_registered is False
    finally:
        scheduler.shutdown(wait=False)


async def test_startup_with_a_failed_reconcile_schedules_no_trading_work() -> None:
    worker, _, reconciler, _ = _build()
    reconciler.error = DatabaseError("db down")
    reconciler.result = None
    scheduler = await worker.start()
    try:
        assert worker.state.may_trade is False
        assert worker.state.decision is not None
        assert worker.state.decision.reason is LatchReason.NO_RECONCILIATION
        assert worker.trading_jobs_registered is False
    finally:
        scheduler.shutdown(wait=False)


async def test_startup_reconciles_before_deciding() -> None:
    worker, _, reconciler, _ = _build()
    reconciler.result = _clean()
    await worker.startup()
    assert reconciler.calls == 1
    assert worker.state.decision is not None


async def test_startup_while_frozen_schedules_no_trading_work() -> None:
    worker, _, reconciler, _ = _build(FakeSettingsStore(frozen=True))
    reconciler.result = _clean()
    scheduler = await worker.start()
    try:
        assert worker.trading_jobs_registered is False
        assert worker.state.decision is not None
        assert worker.state.decision.reason is LatchReason.FROZEN
    finally:
        scheduler.shutdown(wait=False)


# ==========================================================================
# The equity snapshot job (dashboard only -- must never touch posture).
# ==========================================================================


async def test_snapshot_runs_once_after_the_close() -> None:
    close = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
    clock = FakeClock(close + timedelta(minutes=20))
    snapshot = FakeSnapshot()
    worker, _, _, _ = _build(
        snapshot=snapshot,
        market=FakeMarketCalendar(close=close),
        clock=clock,
    )
    await worker.snapshot_job()
    assert snapshot.calls == 1
    await worker.snapshot_job()
    assert snapshot.calls == 1, "one snapshot per session close"


async def test_snapshot_waits_for_the_configured_delay_after_close() -> None:
    close = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
    clock = FakeClock(close + timedelta(minutes=1))
    snapshot = FakeSnapshot()
    worker, _, _, _ = _build(
        snapshot=snapshot, market=FakeMarketCalendar(close=close), clock=clock
    )
    await worker.snapshot_job()
    assert snapshot.calls == 0
    clock.advance(20 * 60)
    await worker.snapshot_job()
    assert snapshot.calls == 1


async def test_snapshot_runs_again_after_the_next_close() -> None:
    first_close = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
    market = FakeMarketCalendar(close=first_close)
    clock = FakeClock(first_close + timedelta(minutes=20))
    snapshot = FakeSnapshot()
    worker, _, _, _ = _build(snapshot=snapshot, market=market, clock=clock)
    await worker.snapshot_job()
    market.close = first_close + timedelta(days=1)
    clock.advance(24 * 3600)
    await worker.snapshot_job()
    assert snapshot.calls == 2


async def test_a_failed_snapshot_changes_nothing_about_posture() -> None:
    close = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
    clock = FakeClock(close + timedelta(minutes=20))
    snapshot = FakeSnapshot()
    snapshot.error = RuntimeError("broker down")
    worker, store, reconciler, _ = _build(
        snapshot=snapshot, market=FakeMarketCalendar(close=close), clock=clock
    )
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True

    await worker.snapshot_job()  # must not raise

    assert worker.state.may_trade is True
    assert store.freeze_writes == 0


async def test_snapshot_does_nothing_when_the_calendar_cannot_answer() -> None:
    snapshot = FakeSnapshot()
    worker, _, _, _ = _build(
        snapshot=snapshot, market=FakeMarketCalendar(close=None)
    )
    await worker.snapshot_job()
    assert snapshot.calls == 0


# ==========================================================================
# Config + logging contracts.
# ==========================================================================


def test_every_latch_reason_maps_to_a_halt_reason() -> None:
    """Exhaustive: a new LatchReason cannot land without deciding how it alerts."""
    mapping = scheduler_module._HALT_REASON_BY_LATCH_REASON  # noqa: SLF001
    assert set(mapping) == set(LatchReason)
    assert mapping[LatchReason.CLEAR] is None
    assert all(value for reason, value in mapping.items() if reason is not LatchReason.CLEAR)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"posture_interval_seconds": 0},
        {"reconcile_interval_seconds": -1},
        {"snapshot_check_interval_seconds": 0},
        {"snapshot_delay_after_close_seconds": 0},
        {"misfire_grace_seconds": 0},
        {"freeze_write_attempts": 0},
        {"freeze_write_backoff_seconds": -1.0},
    ],
)
def test_scheduler_config_rejects_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        SchedulerConfig(**kwargs)  # type: ignore[arg-type]


def test_result_max_age_is_the_interval_plus_the_misfire_grace() -> None:
    """Tied to the interval is what makes a genuinely missed run read as `None`.

    The grace is added (architect N-1) because both jobs fire at the same instant
    and the posture job takes the lock first -- an exact-interval bound would
    expire a result on ordinary jitter and halt during normal operation, which
    trains whoever reads the logs to ignore the alert. Deliberately NOT 2x the
    interval: that would tolerate a run that really did not happen.
    """
    config = SchedulerConfig(reconcile_interval_seconds=120, misfire_grace_seconds=30)
    assert config.result_max_age == timedelta(seconds=150)


def test_decision_max_age_is_two_posture_intervals() -> None:
    """One missed tick is jitter; two is a starved worker (architect B-1)."""
    config = SchedulerConfig(posture_interval_seconds=30)
    assert config.decision_max_age == timedelta(seconds=60)


async def test_halted_posture_is_logged_with_a_machine_readable_reason() -> None:
    worker, store, reconciler, _ = _build(FakeSettingsStore(frozen=True))
    reconciler.result = _clean()
    with capture_logs() as logs:
        await worker.run_reconcile()
        await worker.tick()
    halted = [entry for entry in logs if entry["event"] == "worker.posture_halted"]
    assert halted
    assert halted[-1]["halt_reason"] == "FROZEN"
    assert halted[-1]["may_trade"] is False
    assert store.freeze_writes == 0


async def test_the_posture_log_reports_the_state_not_the_latch() -> None:
    """With an unpersisted freeze the latch says CLEAR; the log must not."""
    worker, store, reconciler, _ = _build()
    store.write_failures_remaining = 99
    reconciler.result = _drift()
    await worker.run_reconcile()
    await worker.tick()

    reconciler.result = _clean()
    await worker.run_reconcile()
    with capture_logs() as logs:
        await worker.tick()
    halted = [entry for entry in logs if entry["event"] == "worker.posture_halted"]
    assert halted
    assert halted[-1]["may_trade"] is False
    assert halted[-1]["latch_may_trade"] is True
    assert halted[-1]["halt_reason"] == HALT_REASON_FREEZE_WRITE_FAILED


# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------


def _scheduler_ast() -> ast.Module:
    source = Path(scheduler_module.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


# ==========================================================================
# Architect B-1 / D-1 / D-2: a cached verdict must age out, a clock that steps
# backwards must not revive stale evidence, and a latch defect must stick.
# ==========================================================================


async def test_a_verdict_older_than_the_bound_stops_permitting_trading() -> None:
    """Ageing the evidence is not enough -- the verdict itself must expire.

    Architect B-1: `may_trade` used to be published forever. An order path
    calling it had no way to tell a current yes from a six-hour-old one.
    """
    config = SchedulerConfig(posture_interval_seconds=30)
    worker, _, reconciler, clock = _build(config=config)
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True

    clock.advance(61)  # past 2x the posture interval: the tick is starved

    assert worker.state.may_trade is False, "a stale yes is not a yes"
    assert worker.state.decision_is_current is False


async def test_a_starved_tick_cannot_keep_publishing_yes() -> None:
    """The realistic starvation path, asserted WHILE the tick is blocked.

    A slow reconcile holding the state lock blocks the posture tick, and
    `max_instances=1` drops the queued ones. Probed by the architect: the worker
    reported may_trade True across an owner freeze for the whole hang.
    """
    config = SchedulerConfig(posture_interval_seconds=30)
    worker, store, reconciler, clock = _build(config=config)
    reconciler.result = _clean()
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is True

    gate = asyncio.Event()
    reconciler.gate = gate
    hung = asyncio.create_task(worker.run_reconcile())
    await asyncio.sleep(0)

    store.frozen = True  # the owner freezes from her phone, mid-hang
    clock.advance(61)  # the tick that would notice cannot run

    assert worker.state.may_trade is False, (
        "a worker whose posture tick is starved must stop saying yes"
    )

    gate.set()
    await hung
    reconciler.gate = None


async def test_a_clock_step_backwards_does_not_revive_stale_evidence() -> None:
    """Architect D-1: `_utc_now` is not monotonic.

    An NTP correction stepping the wall clock backwards made an expired result
    look fresh again -- a negative age passed the `> max_age` test. Same family
    as the safety gate's NaN fail-open: a domain edge read as permission.
    """
    config = SchedulerConfig(reconcile_interval_seconds=300)
    worker, _, reconciler, clock = _build(config=config)
    reconciler.result = _clean()
    await worker.run_reconcile()

    clock.advance(-3600)  # NTP steps the clock back an hour

    assert worker.state.fresh_result(clock()) is None, (
        "a negative age is not freshness"
    )


async def test_a_latch_defect_sticks_for_the_life_of_the_process() -> None:
    """Architect D-2: a pure function's failure is deterministic, not transient.

    Halting only the current tick would let the very next one return CLEAR,
    giving intermittently-permitted trading on an input-dependent safety defect.
    """
    worker, _, reconciler, _ = _build()
    reconciler.result = _clean()

    def _boom(**_kwargs: object) -> LatchDecision:
        raise RuntimeError("latch defect")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduler_module, "decide_posture", _boom)
        await worker.run_reconcile()
        await worker.tick()

    assert worker.state.latch_error is True
    assert worker.state.may_trade is False

    # The latch is healthy again and reconciliation is clean -- and it STILL
    # refuses, because the defect was in the safety decision itself.
    await worker.run_reconcile()
    await worker.tick()
    assert worker.state.may_trade is False, "a latch defect must not self-clear"


async def test_a_latch_defect_is_reported_distinctly_and_writes_no_freeze() -> None:
    """It must not masquerade as a books-vs-broker disagreement.

    Reporting LATCH_ERROR as RECONCILE_MISMATCH would send Esther hunting at the
    broker for a discrepancy that does not exist. And no freeze is written: a
    code defect is not evidence of drift and there is nothing for her to clear.
    """
    worker, store, reconciler, _ = _build()
    reconciler.result = _drift()

    def _boom(**_kwargs: object) -> LatchDecision:
        raise RuntimeError("latch defect")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduler_module, "decide_posture", _boom)
        with capture_logs() as logs:
            await worker.run_reconcile()
            await worker.tick()

    reasons = {entry.get("halt_reason") for entry in logs}
    assert HALT_REASON_LATCH_ERROR in reasons
    assert store.freeze_writes == 0, "a code defect must not write a freeze"


async def test_a_naive_clock_is_never_treated_as_fresh() -> None:
    """Architect N-A: refusing to guess costs one halted tick.

    A naive stamp cannot be subtracted from an aware one -- it raises TypeError,
    and this path sits inside both `may_trade` and the "a tick never raises"
    guarantee. An earlier version had this guard; a rewrite dropped it and no
    test noticed, which is why it is pinned here now.
    """
    worker, _, reconciler, clock = _build()
    reconciler.result = _clean()
    await worker.run_reconcile()

    naive = datetime(2026, 7, 21, 15, 0)  # noqa: DTZ001 -- the case under test

    assert worker.state.fresh_result(naive) is None, "a naive clock is not evidence"
