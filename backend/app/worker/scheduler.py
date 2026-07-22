"""Worker lifecycle + scheduler: the thing that keeps the bot's posture honest.

The pure pieces already exist: :mod:`app.worker.reconciliation` reports what one
run observed, and :mod:`app.worker.latch` turns a report plus the persisted
freeze flag into a :class:`~app.worker.latch.LatchDecision`. Neither of them can
be defeated on its own -- **this** module is where a careless implementation
would defeat them, so every rule below is a rule about this file.

LIFECYCLE (CLAUDE.md invariants #2, #3, #6)
-------------------------------------------
1. Born HALTED. A fresh :class:`WorkerState` has no decision, and "no opinion"
   is never permission.
2. Config loads or the process fails (see :mod:`app.core.config`).
3. Startup: probe `settings`, reconcile once, then run one full tick.
4. Only then are jobs scheduled, and trading jobs only if that startup tick
   came back CLEAR.

THE ELEVEN WAYS A SCHEDULER DEFEATS THE LATCH (and where each is handled)
-------------------------------------------------------------------------
1. *Caching the freeze flag.* Every tick re-reads `settings` from the DB
   (:meth:`Worker._read_frozen`). :class:`WorkerState` has no slot to hold it --
   its ``__slots__`` are pinned by a test precisely so nobody can add one.
   Without this the worker never sees an owner freeze mid-session, nor the
   freeze it wrote itself.
2. *Treating ``engage_freeze`` as advisory.* It calls
   :meth:`SettingsStore.engage_system_freeze` and the halt is only real once
   that write lands. An in-memory halt dies with the process; the restart reads
   ``frozen = false`` and trades.
3. *Swallowing a failed freeze write.* :meth:`Worker._engage_freeze` retries,
   and until it succeeds :attr:`WorkerState.freeze_write_pending` forces
   ``may_trade`` to False regardless of what the latch says. It logs CRITICAL
   with a machine-readable ``halt_reason`` and it does **not** exit -- a crash
   here is a supervisor restart into "no opinion", which is the one state that
   forgets the drift entirely. Success is judged on the RETURNED row
   (``frozen is True``), not on the absence of an exception: a write that
   "succeeded" without setting the flag is a failed halt. The pending flag lives
   for the life of the process and is cleared ONLY by a confirmed write --
   never by a later clean reconciliation.
4. *Reusing a stale reconciliation.* :meth:`WorkerState.fresh_result` returns
   ``None`` once the stored result is older than the reconcile interval, and
   ``None`` is the latch's fail-closed input. A missed run must read as "no
   opinion", never as silence.
5. *Falling back to last-known-good.* A reconcile that raises stores ``None``,
   clearing the previous result rather than keeping it.
6. *Gating on reconciliation diagnostics.* Only the :class:`LatchDecision` is
   consumed. ``positions_reconciled`` is True on every ``CASH_NOT_VERIFIED``
   run, so gating on it would trade with cash unverified. This module never
   reads it (pinned by an AST test).
7. *Passing ``result.reconciled`` where the latch verdict belongs.* Only the
   persisted ``settings.frozen`` connects a past drift to a future order.
8. *Turning an unreadable settings row into ``False``.* :meth:`Worker.
   _read_frozen` returns ``None`` on any failure -- never ``False``.
9. *Gating on the reason string.* :attr:`WorkerState.may_trade` compares
   ``decision.may_trade is True`` -- identity, not truthiness, and never
   ``reason``.
10. *Letting two ticks race.* Every job body that touches state takes one
    :class:`asyncio.Lock`, and every APScheduler job is registered with
    ``max_instances=1``, ``coalesce=True`` and a tight ``misfire_grace_time``.
11. *Writing ``frozen = false``.* Structurally impossible here: the worker's
    view of the database is the two-method :class:`SettingsStore` protocol, so
    ``update_settings`` is not reachable, ``engage_system_freeze`` takes no
    arguments and hardcodes ``true``, and migration ``20260721000001`` has the
    database itself reject a system unfreeze. Three layers, none of them a
    convention.

Owner ruling 2026-07-21 (docs/decisions.md): the worker may ENGAGE the freeze,
one-way, never release it. Clearing it is Esther's act alone.

KNOWN RESIDUAL HOLES (written down deliberately, not discovered later)
----------------------------------------------------------------------
- **Drift observed -> freeze write fails -> the process dies.** The pending flag
  is in memory, so it dies with the process. A restart reads ``frozen = false``,
  and if the drift evidence has moved by then (position closed out of band, a
  late fill recorded) the next run reads CLEAN and trading resumes with nobody
  having acknowledged the disagreement. It is narrow -- the write usually fails
  because the database is unreachable, in which case ``get_settings()`` fails
  too and the latch halts on ``SETTINGS_UNREADABLE`` anyway -- but it is real.
  Closing it properly needs durable local state (or the freeze write moved into
  the same transaction as whatever recorded the drift), which is an owner-level
  design decision, not a patch.
- **The freeze is level-triggered.** While drift persists, every tick re-asserts
  the freeze. That is deliberate: it is idempotent, cheap, and it is what makes
  the retry above work. The cost is that ``settings.updated_at`` (and a
  ``settings_history`` row per tick) stops meaning "when the owner last changed
  something" during a sustained drift halt. Read ``changed_by IS NULL`` to tell
  the bot's halts from Esther's actions.
- **``may_trade = True`` is currently unreachable end to end.** It needs
  ``ReconciliationStatus.CLEAN``, which needs ``cash_checked = True``, which
  cannot happen until the DB has a cash ledger to compare against (owner ruling
  2026-07-21: a partial verification is not a verification). Permanent halt is
  therefore the CORRECT posture today and is asserted as such in the tests. Do
  not "fix" it to make a run look healthy -- it lifts on its own when the order
  path lands a cash ledger.

What this module deliberately does NOT contain: a trading loop, an order path,
a strategy, or any market-data subscription. None of those exist yet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.worker.latch import LatchDecision, LatchReason, decide_posture
from app.worker.market_hours import MarketClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.core.models import BotSettings
    from app.worker.reconciliation import ReconciliationResult

log = structlog.get_logger()

# Machine-readable halt reasons (logging-conventions skill: a halt without one is
# a bug). The first five mirror the enum in that skill; FREEZE_WRITE_FAILED is
# this module's addition -- "we decided to halt but could not persist it" is a
# distinct, louder condition than any of the others and must be alertable.
HALT_REASON_FROZEN = "FROZEN"
HALT_REASON_SETTINGS_UNREADABLE = "SETTINGS_UNREADABLE"
HALT_REASON_RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
HALT_REASON_FREEZE_WRITE_FAILED = "FREEZE_WRITE_FAILED"
# A defect in the safety decision itself, not a market event. Distinct on
# purpose (architect D-2): reporting it as RECONCILE_MISMATCH would send the
# owner hunting at the broker for a disagreement that does not exist.
HALT_REASON_LATCH_ERROR = "LATCH_ERROR"

# Every latch reason maps to exactly one machine-readable halt reason (None for
# the one reason that is not a halt). Exhaustiveness is pinned by a test, so a
# new LatchReason cannot be added without deciding how it alerts.
_HALT_REASON_BY_LATCH_REASON: dict[LatchReason, str | None] = {
    LatchReason.CLEAR: None,
    LatchReason.FROZEN: HALT_REASON_FROZEN,
    LatchReason.SETTINGS_UNREADABLE: HALT_REASON_SETTINGS_UNREADABLE,
    LatchReason.NO_RECONCILIATION: HALT_REASON_RECONCILE_MISMATCH,
    LatchReason.DRIFT_HALT: HALT_REASON_RECONCILE_MISMATCH,
    LatchReason.TRANSIENT_HALT: HALT_REASON_RECONCILE_MISMATCH,
    LatchReason.CASH_NOT_VERIFIED: HALT_REASON_RECONCILE_MISMATCH,
}

JOB_POSTURE = "posture"
JOB_RECONCILE = "reconcile"
JOB_EQUITY_SNAPSHOT = "equity_snapshot"


class SettingsStore(Protocol):
    """The worker's ENTIRE view of the database. Deliberately two methods.

    :class:`app.core.db.Database` satisfies this structurally. Narrowing the
    type here is a safety mechanism, not tidiness: ``update_settings`` -- the
    only method that can write ``frozen = false`` -- is not reachable through
    this protocol, so a worker-side unfreeze is a type error before it is ever
    a runtime bug. See invariant #11 in the module docstring.
    """

    async def get_settings(self) -> BotSettings: ...

    async def engage_system_freeze(self) -> BotSettings: ...


class MarketCalendar(Protocol):
    """What the worker needs to know about market hours, and nothing more.

    :class:`app.worker.market_hours.MarketClock` satisfies it. Stated as a
    protocol so the scheduler's tests can drive the market open/closed without
    building a real calendar, while the production path still gets
    `exchange_calendars` as the sole authority.
    """

    @property
    def name(self) -> str: ...

    def is_open(self, when: datetime) -> bool: ...

    def previous_close(self, when: datetime) -> datetime | None: ...


class SchedulerConfig:
    """Timings for the worker's jobs. All seconds, all positive.

    ``reconcile_interval_seconds`` does double duty on purpose: it sets how
    often reconciliation runs AND anchors the age at which a stored result stops
    counting (:attr:`result_max_age` = this interval plus the misfire grace; see
    that property for why the grace is added). Tying them together means a
    skipped, misfired or failed reconcile cannot leave a result that still looks
    fresh -- the worker simply loses its opinion and halts.
    """

    __slots__ = (
        "freeze_write_attempts",
        "freeze_write_backoff_seconds",
        "misfire_grace_seconds",
        "posture_interval_seconds",
        "reconcile_interval_seconds",
        "snapshot_check_interval_seconds",
        "snapshot_delay_after_close_seconds",
    )

    def __init__(
        self,
        *,
        posture_interval_seconds: int = 30,
        reconcile_interval_seconds: int = 300,
        snapshot_check_interval_seconds: int = 900,
        snapshot_delay_after_close_seconds: int = 900,
        misfire_grace_seconds: int = 30,
        freeze_write_attempts: int = 5,
        freeze_write_backoff_seconds: float = 1.0,
    ) -> None:
        values = {
            "posture_interval_seconds": posture_interval_seconds,
            "reconcile_interval_seconds": reconcile_interval_seconds,
            "snapshot_check_interval_seconds": snapshot_check_interval_seconds,
            "snapshot_delay_after_close_seconds": snapshot_delay_after_close_seconds,
            "misfire_grace_seconds": misfire_grace_seconds,
            "freeze_write_attempts": freeze_write_attempts,
        }
        for name, value in values.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if freeze_write_backoff_seconds < 0:
            raise ValueError("freeze_write_backoff_seconds must be non-negative")
        self.posture_interval_seconds = posture_interval_seconds
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.snapshot_check_interval_seconds = snapshot_check_interval_seconds
        self.snapshot_delay_after_close_seconds = snapshot_delay_after_close_seconds
        self.misfire_grace_seconds = misfire_grace_seconds
        self.freeze_write_attempts = freeze_write_attempts
        self.freeze_write_backoff_seconds = freeze_write_backoff_seconds

    @property
    def result_max_age(self) -> timedelta:
        """How old a reconciliation may be and still count as evidence.

        The reconcile interval PLUS the misfire grace, not the interval exactly.
        Both jobs fire at t=300 and the posture job takes the state lock first,
        so an exact-interval bound would score a result as expired by a few
        milliseconds of ordinary jitter and halt on ``NO_RECONCILIATION`` during
        normal operation -- recurring warnings that teach whoever reads them to
        ignore this alert. The grace is the same window APScheduler will still
        run a late job in, so anything beyond it is a genuinely missed run.
        Deliberately NOT 2x the interval: that would tolerate a run that really
        did not happen.
        """
        return timedelta(
            seconds=self.reconcile_interval_seconds + self.misfire_grace_seconds
        )

    @property
    def decision_max_age(self) -> timedelta:
        """How old the published verdict may be before it stops meaning anything.

        Aging the evidence is not enough: a verdict computed from fresh evidence
        is itself stale once the tick that would refresh it stops running
        (architect B-1). Two posture intervals -- one missed tick is jitter, two
        is a starved worker, and a starved worker must not keep publishing
        ``may_trade = True`` to an order path that is still calling it.
        """
        return timedelta(seconds=2 * self.posture_interval_seconds)

    @property
    def snapshot_delay_after_close(self) -> timedelta:
        return timedelta(seconds=self.snapshot_delay_after_close_seconds)


class WorkerState:
    """The worker's current posture. What a future order path must consult.

    ``__slots__`` is the safety surface and is pinned by a test. In particular
    there is **no slot for the freeze flag**: the flag lives in the database,
    is re-read every tick, and caching it here is exactly how a worker stops
    noticing that the owner froze it (or that it froze itself).

    Nothing here is a source of truth. It is a cache of the latest *verdict*,
    and EVERY field that can go stale is served through an age check rather than
    read directly -- the evidence (:attr:`_result`) and the verdict itself
    (:attr:`_decision`). Ageing only the evidence is not enough: a verdict
    computed from fresh evidence is still stale once the tick that would refresh
    it stops running, and an order path calling :attr:`may_trade` has no way to
    tell a current "yes" from a six-hour-old one (architect B-1).
    """

    __slots__ = (
        "_decided_at",
        "_decision",
        "_freeze_write_pending",
        "_latch_error",
        "_max_decision_age",
        "_max_result_age",
        "_now",
        "_result",
        "_result_at",
    )

    def __init__(
        self,
        *,
        max_result_age: timedelta,
        max_decision_age: timedelta,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._max_result_age = max_result_age
        self._max_decision_age = max_decision_age
        self._now = now_fn if now_fn is not None else _utc_now
        # Born HALTED: no decision has been made, and no decision is not "yes".
        self._decision: LatchDecision | None = None
        self._decided_at: datetime | None = None
        self._result: ReconciliationResult | None = None
        self._result_at: datetime | None = None
        self._freeze_write_pending = False
        self._latch_error = False

    # -- posture --------------------------------------------------------- #

    @property
    def may_trade(self) -> bool:
        """The ONE bit that permits trading. False unless everything is provably fine.

        Five ways to be False, in order of precedence:

        1. A freeze we decided on but could not persist is still outstanding --
           refuse to trade until the write lands, or the halt is a lie.
        2. The latch itself raised at some point in this process's life. That is
           a code defect in the safety decision, and a pure function's failure is
           deterministic in its input class rather than transient -- so it sticks
           until a restart (see :meth:`mark_latch_error`).
        3. No decision has been made yet (fresh process, startup not finished).
        4. The published verdict has aged out. A worker whose posture tick is
           starved must stop saying yes, not keep repeating its last yes.
        5. The latch said no. Compared with ``is True``, never truthiness and
           never via :attr:`LatchDecision.reason` -- a caller that gates on the
           reason is one enum member away from trading on a halt.
        """
        if self._freeze_write_pending:
            return False
        if self._latch_error:
            return False
        decision = self._decision
        if decision is None:
            return False
        if not _within(self._now(), self._decided_at, self._max_decision_age):
            return False
        return decision.may_trade is True

    @property
    def decision(self) -> LatchDecision | None:
        """The latest latch verdict, or None if none has been made yet.

        Diagnostic only -- it carries no age. Gate on :attr:`may_trade`.
        """
        return self._decision

    @property
    def decision_age(self) -> timedelta | None:
        """How long ago the published verdict was computed, for logs/alerts."""
        if self._decided_at is None:
            return None
        return self._now() - self._decided_at

    @property
    def decision_is_current(self) -> bool:
        """True while the published verdict is young enough to mean anything."""
        return _within(self._now(), self._decided_at, self._max_decision_age)

    @property
    def freeze_write_pending(self) -> bool:
        """True while a decided freeze has not yet been persisted to `settings`."""
        return self._freeze_write_pending

    @property
    def latch_error(self) -> bool:
        """True once the latch has raised in this process. Cleared only by restart."""
        return self._latch_error

    def record_decision(self, decision: LatchDecision) -> None:
        """Publish a verdict, stamped with the moment it was computed."""
        self._decision = decision
        self._decided_at = self._now()

    # -- reconciliation ---------------------------------------------------- #

    def record_reconciliation(
        self, result: ReconciliationResult | None, *, at: datetime
    ) -> None:
        """Store a reconciliation outcome, or clear it when there isn't one.

        Passing ``None`` (the run failed, or was never attempted) CLEARS the
        stored result. There is no "keep the last good one" branch: a result we
        did not just obtain is not evidence about the world now.

        ``at`` is when the run *started*, not when it finished -- a reconcile
        that took 90 seconds describes a 90-second-old world, and dating it from
        completion would make it look fresher than it is.

        Defensive: reconciliation currently runs UNDER the state lock, so
        out-of-order completion is unreachable today. The ordering guard exists
        so that moving the await out of the lock stays a one-line change rather
        than a correctness question (architect D-4 -- an earlier draft of this
        docstring claimed the move had already happened). An OLDER successful
        result never overwrites a newer one; a ``None`` always applies, because
        clearing is the fail-closed direction and losing evidence can only ever
        halt us.
        """
        if result is None:
            self._result = None
            self._result_at = None
            return
        if self._result_at is not None and at < self._result_at:
            return
        self._result = result
        self._result_at = at

    def fresh_result(self, now: datetime) -> ReconciliationResult | None:
        """The stored result, or ``None`` if it is not provably current.

        ``None`` is the latch's fail-closed input (already exhaustively tested
        in ``test_latch.py``), so an interval where reconciliation did not run
        -- skipped, misfired, errored, or the process was busy -- reads as "no
        opinion" and halts. Never as permission.
        """
        result = self._result
        if result is None:
            return None
        if not _within(now, self._result_at, self._max_result_age):
            return None
        return result

    # -- freeze write bookkeeping ------------------------------------------ #

    def mark_freeze_write_failed(self) -> None:
        """Record that a decided halt is not yet in the database.

        Lives for the life of the process. Nothing clears it except a confirmed
        write -- in particular a later CLEAN reconciliation must not, or the
        auto-recovery the owner ruled against on 2026-07-21 comes back one layer
        further out.
        """
        self._freeze_write_pending = True

    def mark_freeze_write_succeeded(self) -> None:
        """Clear the debt. Called ONLY after a write returned ``frozen is True``."""
        self._freeze_write_pending = False

    def mark_latch_error(self) -> None:
        """Record that the latch itself raised. Sticky for the process lifetime.

        `decide_posture` is a pure function, so a raise is deterministic in its
        input class rather than transient: the very next tick could take the
        same branch again. Letting the next tick return CLEAR would give
        intermittently-permitted trading on an input-dependent safety defect
        (architect D-2).

        Deliberately NOT a database freeze: a code defect is not evidence of
        drift, Esther could not discharge it by looking at the broker, and it
        would pollute `settings_history` with a non-event. Cleared only by a
        restart -- which is exactly what deploying the fix does.
        """
        self._latch_error = True


class Worker:
    """Owns the worker's lifecycle: startup, the tick, and the scheduled jobs.

    Every collaborator is injected, so the tests exercise the real control flow
    with no network, no database and no wall clock.
    """

    def __init__(
        self,
        *,
        db: SettingsStore,
        reconcile_fn: Callable[[], Awaitable[ReconciliationResult]],
        snapshot_fn: Callable[[], Awaitable[object]],
        market_clock: MarketCalendar | None = None,
        config: SchedulerConfig | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._reconcile = reconcile_fn
        self._snapshot = snapshot_fn
        self._market = market_clock if market_clock is not None else MarketClock()
        self._config = config if config is not None else SchedulerConfig()
        self._now = now_fn if now_fn is not None else _utc_now
        self._sleep = sleep_fn if sleep_fn is not None else asyncio.sleep
        self._state = WorkerState(
            max_result_age=self._config.result_max_age,
            max_decision_age=self._config.decision_max_age,
            now_fn=self._now,
        )
        # One lock for every job body that touches state. Two ticks must never
        # race -- one writing a freeze while the other decides it may trade.
        self._lock = asyncio.Lock()
        self._last_snapshot_close: datetime | None = None
        self.trading_jobs_registered = False

    @property
    def state(self) -> WorkerState:
        """The posture a future order path consults before every order."""
        return self._state

    @property
    def market_clock(self) -> MarketCalendar:
        return self._market

    # -- startup ----------------------------------------------------------- #

    async def startup(self) -> None:
        """Born HALTED -> probe settings -> reconcile once -> decide. In that order.

        Never raises on an unreadable settings row or a failed reconcile: both
        are *outcomes* that leave the worker halted. A startup that crashes is a
        supervisor restart loop, and a restart loop is a worker that never gets
        as far as persisting the drift it saw.
        """
        log.info(
            "worker.starting",
            state="HALTED",
            calendar=self._market.name,
            reconcile_interval_seconds=self._config.reconcile_interval_seconds,
            posture_interval_seconds=self._config.posture_interval_seconds,
        )
        startup_frozen = await self._read_frozen()
        log.info(
            "worker.startup_settings_probe",
            settings_readable=startup_frozen is not None,
            frozen=startup_frozen,
        )
        await self.run_reconcile()
        await self.tick()

    async def start(self) -> AsyncIOScheduler:
        """Run startup, build the scheduler, register jobs, start it.

        Trading jobs are registered only if the startup tick came back CLEAR.
        That is a *second* gate. :attr:`WorkerState.may_trade` is NECESSARY
        and NEVER SUFFICIENT: it is a cached verdict, and Invariant 2 requires
        reading `settings` fresh before EVERY order. The age bound narrows the
        window in which a cached yes can outlive an owner freeze; it does not
        remove it (architect D-3 -- probed at up to one posture interval).

        The order path, when it exists, MUST carry all of:
        1. a fresh ``get_settings()`` immediately before each submission,
           ``None`` -> deny;
        2. ``WorkerState.may_trade`` checked at submission time;
        3. ``evaluate_order_safety()`` wired with its documented inputs,
           including the ``loss_so_far`` positive-magnitude sign test;
        4. an end-to-end test where the owner freezes mid-session and the next
           ORDER ATTEMPT is refused -- not merely the next tick's log line;
        5. the same test with the posture tick starved.
        """
        await self.startup()
        scheduler = self.build_scheduler()
        if self._state.may_trade:
            self._register_trading_jobs(scheduler)
        else:
            decision = self._state.decision
            log.warning(
                "worker.trading_jobs_not_scheduled",
                reason=None if decision is None else decision.reason.value,
                halt_reason=_halt_reason_for(decision, state=self._state),
                freeze_write_pending=self._state.freeze_write_pending,
            )
        scheduler.start()
        log.info("worker.scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])
        return scheduler

    async def run(self) -> None:
        """Start and then run until cancelled (the process entrypoint)."""
        scheduler = await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler.shutdown(wait=False)
            log.info("worker.scheduler_stopped")

    def build_scheduler(self) -> AsyncIOScheduler:
        """Register the safety/maintenance jobs. No trading job exists yet.

        Every job: ``max_instances=1`` (no two instances of a job ever run at
        once), ``coalesce=True`` (a burst of missed runs collapses into one --
        catching up on ten stale reconciles would be worse than useless), and a
        tight ``misfire_grace_time`` (a run that is late by more than the grace
        is DROPPED rather than executed against a stale trigger time; the stored
        result then ages out and the worker halts, which is the intended
        reading of a missed run).
        """
        scheduler = AsyncIOScheduler(timezone="UTC")
        grace = self._config.misfire_grace_seconds
        scheduler.add_job(
            self.tick,
            IntervalTrigger(seconds=self._config.posture_interval_seconds),
            id=JOB_POSTURE,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=grace,
        )
        scheduler.add_job(
            self.reconcile_job,
            IntervalTrigger(seconds=self._config.reconcile_interval_seconds),
            id=JOB_RECONCILE,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=grace,
        )
        scheduler.add_job(
            self.snapshot_job,
            IntervalTrigger(seconds=self._config.snapshot_check_interval_seconds),
            id=JOB_EQUITY_SNAPSHOT,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=grace,
        )
        return scheduler

    def _register_trading_jobs(self, scheduler: AsyncIOScheduler) -> None:
        """The ONE place a future trading job may be registered.

        Nothing is registered today: no rules engine and no order path exist.
        Keeping the hook means the startup gate is real and tested now, rather
        than being remembered later.
        """
        self.trading_jobs_registered = True
        log.info(
            "worker.trading_jobs_registered",
            count=0,
            detail="no trading job exists yet; posture is CLEAR",
        )

    # -- the tick ---------------------------------------------------------- #

    async def tick(self) -> None:
        """Re-read `settings`, decide the posture, persist any freeze. Never raises.

        Order matters and is load-bearing:

        1. Retry an outstanding freeze write first -- the worker must not form a
           new opinion while a halt it already decided on is unpersisted.
        2. Re-read `settings` from the database. EVERY tick, no exceptions, no
           cache. This is how an owner freeze mid-session is seen, and how the
           worker sees the freeze it wrote itself.
        3. Take the reconciliation result only if it is younger than
           :attr:`SchedulerConfig.result_max_age` (the reconcile interval plus
           the misfire grace); otherwise ``None``.
        4. Ask the latch. Consume only its decision.
        5. Persist the freeze if it asked for one.
        """
        async with self._lock:
            await self._retry_pending_freeze()
            currently_frozen = await self._read_frozen()
            result = self._state.fresh_result(self._now())
            decision = self._decide(result=result, currently_frozen=currently_frozen)
            self._state.record_decision(decision)
            if decision.engage_freeze:
                await self._engage_freeze(trigger=decision.reason.value)
            self._log_posture(decision, reconciliation_available=result is not None)

    def _decide(
        self,
        *,
        result: ReconciliationResult | None,
        currently_frozen: bool | None,
    ) -> LatchDecision:
        """Ask the latch, and halt if the latch itself cannot answer.

        :func:`decide_posture` is documented as never raising, and for every
        input it is tested against that holds. It is not, however, *structurally*
        incapable of raising: it reads ``result.category``, which looks statuses
        up in a dict, so a future ``ReconciliationStatus`` added without a
        category mapping would ``KeyError`` straight through this call
        (architect N-a). An exception from the safety decision must not become
        an exception from the tick -- it must become a halt.

        No freeze is written in that case: an exception is not evidence of
        drift, and manufacturing an owner-clearable halt out of a code bug would
        page Esther for something she cannot clear by looking at the broker.
        """
        try:
            return decide_posture(result=result, currently_frozen=currently_frozen)
        except Exception as exc:
            # Sticky for the process lifetime: a pure function's failure is
            # deterministic in its input class, so "halt just this tick" would
            # permit trading again on the next one (architect D-2).
            self._state.mark_latch_error()
            log.critical(
                "worker.latch_raised",
                halt_reason=HALT_REASON_LATCH_ERROR,
                invariant="3",
                error_type=type(exc).__name__,
                detail=(
                    "the latch could not produce a verdict; halted for the life "
                    "of this process. This is a code defect, not a market event "
                    "-- there is nothing for the owner to clear, and no freeze "
                    "is written. Deploy a fix and restart."
                ),
            )
            return LatchDecision(
                may_trade=False,
                engage_freeze=False,
                reason=LatchReason.TRANSIENT_HALT,
            )

    async def reconcile_job(self) -> None:
        """Periodic reconciliation -- during market hours only, per the calendar.

        Outside market hours nothing is reconciled, so the stored result ages
        out and the posture becomes ``NO_RECONCILIATION``. That is correct: the
        worker has no current evidence and must not trade on last night's.
        """
        if not self._market.is_open(self._now()):
            log.debug("worker.reconcile_skipped", detail="market closed per calendar")
            return
        await self.run_reconcile()

    async def run_reconcile(self) -> None:
        """Run one reconciliation and store the outcome. Never raises.

        A raising reconcile stores ``None``. There is deliberately no
        last-known-good fallback: an exception means we do not know the state of
        the world, and "we do not know" must never be served to the latch as
        evidence.
        """
        async with self._lock:
            started_at = self._now()
            result: ReconciliationResult | None
            try:
                result = await self._reconcile()
            except Exception as exc:
                log.error(
                    "worker.reconcile_raised",
                    halt_reason=HALT_REASON_RECONCILE_MISMATCH,
                    invariant="6",
                    error_type=type(exc).__name__,
                    detail="stored result cleared; no last-known-good fallback",
                )
                result = None
            self._state.record_reconciliation(result, at=started_at)

    async def snapshot_job(self) -> None:
        """One equity snapshot per session, shortly after that session's close.

        The close time is asked of the calendar, never assumed -- a half day
        closes at 13:00 ET and a winter close is 21:00 UTC, and neither is
        written down anywhere in this codebase.

        Failures are logged and dropped: the snapshot feeds the dashboard, not
        the money path, and it must never influence trading posture in either
        direction. ``insert_equity_snapshot`` upserts per UTC day, so a repeat
        run is harmless.
        """
        now = self._now()
        close = self._market.previous_close(now)
        if close is None:
            return
        if now - close < self._config.snapshot_delay_after_close:
            return
        if self._last_snapshot_close == close:
            return
        try:
            await self._snapshot()
        except Exception as exc:
            log.error(
                "worker.snapshot_failed",
                error_type=type(exc).__name__,
                session_close=close.isoformat(),
            )
            return
        self._last_snapshot_close = close
        log.info("worker.snapshot_taken", session_close=close.isoformat())

    # -- settings + freeze -------------------------------------------------- #

    async def _read_frozen(self) -> bool | None:
        """Read `settings.frozen` fresh from the database. ``None`` if unreadable.

        NEVER ``False`` on failure. ``None`` is the latch's "settings
        unreadable" input and halts (invariant #2); ``False`` would read as
        "the owner has not frozen us" and permit trading blind.
        """
        try:
            settings = await self._db.get_settings()
        except Exception as exc:
            log.error(
                "worker.settings_unreadable",
                halt_reason=HALT_REASON_SETTINGS_UNREADABLE,
                invariant="2",
                error_type=type(exc).__name__,
            )
            return None
        return settings.frozen

    async def _retry_pending_freeze(self) -> None:
        """Retry a freeze the worker decided on but could not persist."""
        if not self._state.freeze_write_pending:
            return
        log.critical(
            "worker.freeze_write_retrying",
            halt_reason=HALT_REASON_FREEZE_WRITE_FAILED,
            detail="a decided halt is not yet persisted; trading stays refused",
        )
        await self._engage_freeze(trigger="retry")

    async def _engage_freeze(self, *, trigger: str) -> None:
        """Persist the self-halt: ``settings.frozen = true``. One-way, retried.

        Owner ruling 2026-07-21: the worker may engage the freeze so a drift
        halt survives a restart, and may never release it.
        :meth:`SettingsStore.engage_system_freeze` takes no arguments and
        hardcodes ``true``; there is nothing to pass that could make it an
        unfreeze.

        If every attempt fails the failure is NOT swallowed: the state is marked
        pending (which forces ``may_trade`` False), a CRITICAL line is emitted
        with a machine-readable ``halt_reason``, and the worker keeps running so
        the next tick can retry. Raising here would take the process down and a
        supervisor would restart it into "no opinion" -- with the drift
        unrecorded and ``frozen`` still false in the database.
        """
        attempts = self._config.freeze_write_attempts
        for attempt in range(1, attempts + 1):
            failure: str | None = None
            try:
                written = await self._db.engage_system_freeze()
            except Exception as exc:
                failure = type(exc).__name__
            else:
                # Judge the WRITE, not the call. A statement that returned a row
                # whose `frozen` is not exactly True did not halt anything, and
                # treating "no exception" as "halted" would leave the worker
                # believing in a freeze that does not exist.
                if written.frozen is not True:
                    failure = "write_did_not_set_frozen"
            if failure is not None:
                log.error(
                    "worker.freeze_write_failed",
                    halt_reason=HALT_REASON_FREEZE_WRITE_FAILED,
                    trigger=trigger,
                    attempt=attempt,
                    attempts=attempts,
                    error_type=failure,
                )
                if attempt < attempts and self._config.freeze_write_backoff_seconds:
                    await self._sleep(
                        self._config.freeze_write_backoff_seconds * attempt
                    )
                continue
            self._state.mark_freeze_write_succeeded()
            log.critical(
                "worker.freeze_engaged",
                halt_reason=HALT_REASON_RECONCILE_MISMATCH,
                invariant="6",
                trigger=trigger,
                attempt=attempt,
                detail="settings.frozen persisted true; only the owner clears it",
            )
            return
        self._state.mark_freeze_write_failed()
        log.critical(
            "worker.freeze_write_unpersisted",
            halt_reason=HALT_REASON_FREEZE_WRITE_FAILED,
            invariant="6",
            trigger=trigger,
            attempts=attempts,
            detail=(
                "decided to halt but could not write settings.frozen; trading "
                "refused and the write will be retried every tick"
            ),
        )

    # -- logging ------------------------------------------------------------ #

    def _log_posture(
        self, decision: LatchDecision, *, reconciliation_available: bool
    ) -> None:
        """Emit the posture. Reports the STATE's verdict, not the latch's.

        They differ in exactly one case, and it is the dangerous one: an
        unpersisted freeze. The latch, reading a `settings` row whose write
        failed, can legitimately say CLEAR while the worker still refuses to
        trade. Logging the latch's answer there would put "may_trade: true" in
        the record of a halted worker.
        """
        may_trade = self._state.may_trade
        fields = {
            "may_trade": may_trade,
            "latch_may_trade": decision.may_trade,
            "reason": decision.reason.value,
            "freeze_write_pending": self._state.freeze_write_pending,
            # Architect N-C: a postmortem must be able to read "the verdict was
            # four minutes old" without inferring it from timestamps, and must
            # be able to tell a latch defect from a market event at a glance.
            "latch_error": self._state.latch_error,
            "decision_is_current": self._state.decision_is_current,
            "reconciliation_available": reconciliation_available,
        }
        if may_trade:
            log.info("worker.posture_clear", **fields)
            return
        log.warning(
            "worker.posture_halted",
            halt_reason=_halt_reason_for(decision, state=self._state),
            **fields,
        )


def _halt_reason_for(
    decision: LatchDecision | None, *, state: WorkerState | None = None
) -> str | None:
    """Machine-readable halt reason for a decision, or None when not halted."""
    if state is not None and state.freeze_write_pending:
        return HALT_REASON_FREEZE_WRITE_FAILED
    if state is not None and state.latch_error:
        return HALT_REASON_LATCH_ERROR
    if decision is None:
        # No decision yet: the worker is halted because it was born halted.
        return HALT_REASON_RECONCILE_MISMATCH
    # `.get` rather than `[]` (architect N-5): this is reached from inside the
    # "a tick never raises" guarantee, so an unmapped reason must degrade to a
    # halt label, not take the process down.
    return _HALT_REASON_BY_LATCH_REASON.get(
        decision.reason, HALT_REASON_RECONCILE_MISMATCH
    )


def _within(now: datetime, at: datetime | None, max_age: timedelta) -> bool:
    """True only if ``at`` is a stamp that is provably recent enough.

    Bounded at BOTH ends on purpose (architect D-1). The obvious form --
    ``now - at > max_age`` -- treats a NEGATIVE age as fresh, so a wall-clock
    step backwards (an NTP correction; ``_utc_now`` is not monotonic) revives an
    already-expired stamp. That is the same domain-edge family as the safety
    gate's NaN fail-open: an input nobody pictured, silently read as permission.

    A missing stamp is never fresh -- "we have no idea when this was decided"
    must read as no opinion, never as recent.
    """
    if at is None:
        return False
    # A naive stamp cannot be compared to an aware one -- subtracting them
    # raises TypeError, and this helper is reached from inside both `may_trade`
    # and the "a tick never raises" guarantee. Refusing to guess costs one
    # halted tick; guessing costs the guarantee. (Architect N-A: an earlier
    # version had this guard and the rewrite dropped it.)
    if at.tzinfo is None or now.tzinfo is None:
        return False
    age = now - at
    return timedelta(0) <= age <= max_age


def _utc_now() -> datetime:
    return datetime.now(UTC)
