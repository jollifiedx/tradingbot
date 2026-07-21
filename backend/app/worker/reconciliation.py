"""Reconciliation: does the broker's reality match what the DB says we intended?

CLAUDE.md invariant #6 -- **Webull is the source of truth for positions and
cash; the DB is the source of truth for intent and reasoning.** This module
compares the two and, on ANY difference, reports "not reconciled" with a
machine-readable reason and a full, human-readable list of exactly what differs.

What this module deliberately does NOT do
------------------------------------------
- It never *fixes* anything. No DB write, no order, no cancel, no "adjust the
  books to match the broker". A mismatch is a halt + alert and a human decision
  (or, later, an explicitly documented rule) -- never an improvised repair.
- It never decides *when* to run. No scheduler exists yet; the caller (worker
  startup, and later a periodic job) decides that.
- It never raises out of :func:`reconcile`: an unreadable broker or DB is an
  *outcome* (``BROKER_UNREADABLE`` / ``DB_UNREADABLE``, not reconciled), because
  the one thing a startup-gate must never do is crash in a way a supervisor
  restarts into "no opinion".

Shape (mirrors ``safety_gate.py``, on purpose)
-----------------------------------------------
1. :func:`compare_positions` -- **pure**: no I/O, no network, no DB, no clock,
   no randomness, no logging. Everything it needs is passed in, and it returns
   an immutable :class:`ReconciliationResult`. That is what makes every drift
   scenario exhaustively unit-testable without a broker or a database.
2. :func:`reconcile` -- a thin async wrapper that fetches (broker snapshot for
   the ONE pinned account, expected open positions from the DB), calls the pure
   function, logs the outcome, and returns the result. No logic lives here that
   could instead live in the pure function.

The ``reconciled`` bit is exactly what feeds ``evaluate_order_safety(...,
reconciled=...)`` in :mod:`app.worker.safety_gate`: that gate denies every order
when ``reconciled`` is not exactly ``True``, so an unreadable broker, an
unreadable DB, or any drift all end in "no orders" without a separate mechanism.

Fail-closed contract (invariant #3)
------------------------------------
Uncertainty is NEVER "clean". Any input that is missing (``None``) or non-finite
(``NaN``/``Inf``) makes the result *not reconciled*, mapped to the reason for the
check that input belongs to -- never treated as zero, never skipped, and never
raised. Non-finite matters as much as ``None``: ``Decimal('NaN') == x`` is
silently ``False`` and ``abs(Decimal('NaN') - x) > tol`` *raises*, so both would
otherwise corrupt the verdict rather than halt it.

A PARTIAL verification is not a verification (owner ruling, 2026-07-21)
-----------------------------------------------------------------------
If the cash leg was never compared, ``reconciled`` is ``False`` -- status
``CASH_NOT_VERIFIED``. This is enforced *structurally*, not by convention: the
public ``reconciled`` bit is a derived property (``status is CLEAN``), and
``CLEAN`` is only ever produced when positions matched **and**
``cash_checked`` is ``True``; :meth:`ReconciliationResult.__post_init__` rejects
any inconsistent hand-built combination. There is deliberately no way for a
caller to opt out. The finer-grained facts survive for postmortems:
``positions_reconciled`` (did the position comparison itself pass?) and
``cash_checked`` (was cash compared at all?) are separate fields, so
"positions matched, cash was never compared" remains readable in the logs.

THE LATCH RULE -- what the (future) scheduler MUST do with a halt
------------------------------------------------------------------
Not every "not reconciled" means the same thing, and the caller must not treat
them alike. Every status carries a :class:`HaltCategory` (see
:attr:`ReconciliationResult.category`, computed conservatively across ALL
mismatches found, not just the headline status):

- ``TRANSIENT`` (``BROKER_UNREADABLE``, ``DB_UNREADABLE``,
  ``ACCOUNT_NOT_PINNED``): nothing is known to differ -- we simply could not
  look. The worker stays halted for now, and this MAY auto-clear when a later
  run comes back ``CLEAN``. No human action required beyond fixing the outage.
- ``DRIFT`` (``UNEXPECTED_BROKER_POSITION``, ``MISSING_BROKER_POSITION``,
  ``QUANTITY_MISMATCH``, ``DUPLICATE_POSITION``, ``CASH_MISMATCH``): the books
  and the broker genuinely disagree. **STICKY. The caller MUST latch this and
  MUST NOT let it flip back to reconciled on its own** -- not on the next clean
  run, not after a restart. A later ``CLEAN`` result does not mean the drift was
  resolved; it may simply mean the evidence moved. Real money moved in a way we
  did not intend, so a human decides (invariant #6: halt and alert, never
  silent-fix). The owner clears it deliberately via the existing freeze flag.
- ``NOT_VERIFIED`` (``CASH_NOT_VERIFIED``): not a drift halt and NOT something
  the owner can or should "clear" -- there is nothing to acknowledge. It
  resolves structurally, when the DB gains a cash ledger to compare against.
  Latching it would be pointless; clearing it by hand would be a lie.

This module cannot enforce the latch (it is stateless by design and returns a
verdict per run), so the requirement is stated here and pinned by tests: a
scheduler that just overwrites its "reconciled" flag with each run's result
would silently un-halt after real drift.

Money and share quantities are :class:`~decimal.Decimal` throughout -- never
float. Quantities are compared **exactly** (fractional shares are real: a
0.000001-share difference is real drift, not noise). Cash gets a tight
tolerance; see :data:`DEFAULT_CASH_TOLERANCE`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import structlog

from app.core.config import load_settings
from app.core.db import Database
from app.core.webull import AccountSnapshotRequest, WebullClient

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from app.core.config import Settings

log = structlog.get_logger()

# The halt-reason enum this module emits into logs, per the logging-conventions
# skill (a halt without a machine-readable reason is a bug). The narrower
# `ReconciliationStatus` is logged alongside it as `status`.
HALT_REASON = "RECONCILE_MISMATCH"

DEFAULT_CASH_TOLERANCE = Decimal("0.01")
"""Maximum cash difference (absolute, account currency) still treated as clean.

Why a tolerance exists at all: the broker's cash figure and any DB-side cash
expectation are produced by two different systems, from different events, at
different instants, and Webull returns money as decimal strings whose scale can
vary between endpoints. A sub-cent representation/rounding difference is not
evidence of drift.

Why it is this tight: one cent is the smallest unit the account is actually
denominated in, so anything a real fill, fee or transfer could contribute is
strictly larger than the tolerance and still halts. The tolerance absorbs
representation noise ONLY -- it must never be widened to "make reconciliation
pass"; widening it is a safety change and an ESCALATION, not a tuning knob.
Comparison is ``abs(broker - expected) > tolerance``: exactly at the tolerance
is clean, one cent beyond it is a mismatch.
"""


class ReconciliationStatus(StrEnum):
    """Outcome of a reconciliation run -- ``CLEAN`` or the single worst mismatch.

    On a mismatch this is the highest-priority failing check (see
    :data:`_STATUS_PRIORITY`), so alerts get one deterministic headline reason;
    :attr:`ReconciliationResult.mismatches` still carries *every* difference
    found, which is what a postmortem reads.
    """

    CLEAN = "clean"
    ACCOUNT_NOT_PINNED = "account_not_pinned"
    BROKER_UNREADABLE = "broker_unreadable"
    DB_UNREADABLE = "db_unreadable"
    DUPLICATE_POSITION = "duplicate_position"
    UNEXPECTED_BROKER_POSITION = "unexpected_broker_position"
    MISSING_BROKER_POSITION = "missing_broker_position"
    QUANTITY_MISMATCH = "quantity_mismatch"
    CASH_MISMATCH = "cash_mismatch"
    CASH_NOT_VERIFIED = "cash_not_verified"


class HaltCategory(StrEnum):
    """How the caller must treat a not-reconciled outcome. See "THE LATCH RULE".

    ``NONE`` is the reconciled case. The other three are the owner's ruling of
    2026-07-21: transient failures may auto-clear, real drift is sticky until a
    human clears it, and "we never checked" is neither.
    """

    NONE = "none"
    TRANSIENT = "transient"
    DRIFT = "drift"
    NOT_VERIFIED = "not_verified"


# Every status maps to exactly one category (exhaustiveness is pinned by a test
# so a new status cannot be added without deciding how the caller must treat it).
#
# ACCOUNT_NOT_PINNED is TRANSIENT rather than DRIFT: nothing was found to
# differ -- we refused to look, because the account to reconcile was not
# configured. Setting WEBULL_ACCOUNT_ID legitimately makes the next run clean,
# and there is no drift for the owner to acknowledge.
_STATUS_CATEGORY: dict[ReconciliationStatus, HaltCategory] = {
    ReconciliationStatus.CLEAN: HaltCategory.NONE,
    ReconciliationStatus.ACCOUNT_NOT_PINNED: HaltCategory.TRANSIENT,
    ReconciliationStatus.BROKER_UNREADABLE: HaltCategory.TRANSIENT,
    ReconciliationStatus.DB_UNREADABLE: HaltCategory.TRANSIENT,
    ReconciliationStatus.DUPLICATE_POSITION: HaltCategory.DRIFT,
    ReconciliationStatus.UNEXPECTED_BROKER_POSITION: HaltCategory.DRIFT,
    ReconciliationStatus.MISSING_BROKER_POSITION: HaltCategory.DRIFT,
    ReconciliationStatus.QUANTITY_MISMATCH: HaltCategory.DRIFT,
    ReconciliationStatus.CASH_MISMATCH: HaltCategory.DRIFT,
    ReconciliationStatus.CASH_NOT_VERIFIED: HaltCategory.NOT_VERIFIED,
}

# Most-severe-first. Used to fold the categories of EVERY mismatch found into
# one category for the caller: if any real drift was seen, the whole result is
# sticky drift -- even when a broader, transient-looking status (e.g.
# BROKER_UNREADABLE from an unreadable cash figure) wins the headline. Reporting
# the headline's category instead would let a transient label un-latch a run
# that actually saw drift.
_CATEGORY_SEVERITY: tuple[HaltCategory, ...] = (
    HaltCategory.DRIFT,
    HaltCategory.TRANSIENT,
    HaltCategory.NOT_VERIFIED,
    HaltCategory.NONE,
)


# Deterministic headline order when several kinds of mismatch are present.
# Rationale, broadest-doubt first:
#   BROKER_UNREADABLE / DB_UNREADABLE -- one whole side of the comparison is
#     missing, so nothing below could even be evaluated.
#   DUPLICATE_POSITION -- an input is ambiguous; any per-symbol verdict computed
#     from it would be arbitrary.
#   UNEXPECTED_BROKER_POSITION -- real money is in a position we have no record
#     of intending (manual trade, leftover state, a fill we never recorded).
#     Most dangerous of the per-symbol cases.
#   MISSING_BROKER_POSITION -- we believe we hold something we do not.
#   QUANTITY_MISMATCH -- both sides agree the position exists, sizes differ.
#   CASH_MISMATCH -- narrowest: positions agree, the cash leg does not.
# ACCOUNT_NOT_PINNED is produced only by the orchestrator (the pure comparison
# knows nothing about accounts) and so does not appear in this ladder.
# CASH_NOT_VERIFIED is likewise absent: it is never a *mismatch* kind, only the
# status of a run where nothing differed but cash was never compared.
_STATUS_PRIORITY: tuple[ReconciliationStatus, ...] = (
    ReconciliationStatus.BROKER_UNREADABLE,
    ReconciliationStatus.DB_UNREADABLE,
    ReconciliationStatus.DUPLICATE_POSITION,
    ReconciliationStatus.UNEXPECTED_BROKER_POSITION,
    ReconciliationStatus.MISSING_BROKER_POSITION,
    ReconciliationStatus.QUANTITY_MISMATCH,
    ReconciliationStatus.CASH_MISMATCH,
)


class BrokerPosition(Protocol):
    """Structural type for "an open position the broker reports".

    :class:`app.core.webull.Position` satisfies it. Typing the pure comparison
    against a Protocol rather than the concrete model keeps this module free of
    any dependency on the broker wrapper *and* keeps the defence-in-depth
    numeric checks below reachable in tests: the wrapper's Pydantic model
    already refuses a non-finite ``quantity`` at the boundary (verified in
    ``tests/test_reconciliation.py``), so without a structural stand-in those
    branches could not be exercised at all. Two lines of defence, both tested.
    """

    @property
    def symbol(self) -> str: ...

    @property
    def quantity(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class ExpectedPosition:
    """One open position the DB's *intent* record says we should be holding.

    Derived from `trades` (see :meth:`app.core.db.Database.
    get_open_position_intents`) -- never from the broker, or it would be
    reconciling the broker against itself.
    """

    symbol: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One concrete difference, stated with BOTH values so a human can act.

    ``expected`` is the DB/intent side, ``actual`` is the broker side; either is
    ``None`` when that side has no value at all (position absent, figure
    unreadable). ``detail`` is a short plain-English sentence for the alert.
    """

    kind: ReconciliationStatus
    symbol: str | None
    expected: Decimal | None
    actual: Decimal | None
    detail: str

    def as_log_fields(self) -> dict[str, str | None]:
        """Log-safe rendering: Decimals stringified, nothing sensitive included."""
        return {
            "kind": self.kind.value,
            "symbol": self.symbol,
            "expected": None if self.expected is None else str(self.expected),
            "actual": None if self.actual is None else str(self.actual),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Immutable verdict of one reconciliation run.

    :attr:`reconciled` is the only bit the safety gate consumes, and it is a
    **derived** property -- ``status is CLEAN`` -- not a stored flag a caller
    could set. ``CLEAN`` is produced only when the positions matched AND the
    cash leg was actually compared, so "partial verification reported as
    reconciled" is impossible by construction rather than by discipline (owner
    ruling, 2026-07-21). :meth:`__post_init__` additionally rejects any
    hand-built instance that contradicts that rule.

    Diagnostics are preserved separately so a postmortem loses nothing:

    - ``positions_reconciled`` -- did the position comparison itself pass?
    - ``cash_checked`` -- was cash compared at all? ``False`` today, because the
      DB holds no cash expectation (no order path, so no cash ledger).

    A run with ``positions_reconciled=True, cash_checked=False`` therefore reads
    exactly as it should: "positions matched, cash was never compared", status
    ``CASH_NOT_VERIFIED``, ``reconciled=False``.
    """

    status: ReconciliationStatus
    positions_reconciled: bool
    mismatches: tuple[Mismatch, ...] = ()
    cash_checked: bool = False

    def __post_init__(self) -> None:
        """Reject internally inconsistent verdicts at construction time.

        This guards the hand-built path (the classmethods below cannot produce a
        bad combination). It raises only on a programmer error -- never on
        market/broker data -- so the "compare never raises on any input"
        contract is unaffected.
        """
        if self.status is ReconciliationStatus.CLEAN and not (
            self.positions_reconciled and self.cash_checked and not self.mismatches
        ):
            raise ValueError(
                "CLEAN requires positions_reconciled and cash_checked and no "
                "mismatches; a partial verification is not a verification"
            )
        if (
            self.status
            not in (
                ReconciliationStatus.CLEAN,
                ReconciliationStatus.CASH_NOT_VERIFIED,
            )
            and not self.mismatches
        ):
            raise ValueError(
                "a halt must say what differed (CASH_NOT_VERIFIED is the one "
                "not-reconciled status with nothing to report)"
            )

    @property
    def reconciled(self) -> bool:
        """The conservative bit the safety gate consumes. Derived, never stored."""
        return self.status is ReconciliationStatus.CLEAN

    @property
    def category(self) -> HaltCategory:
        """How the caller must treat this outcome -- see "THE LATCH RULE".

        Computed across EVERY mismatch found, not just the headline status: if
        any real drift was seen, the whole run is sticky ``DRIFT`` even when a
        broader transient status won the headline. Never let a transient label
        un-latch a run that actually observed drift.
        """
        categories = {_STATUS_CATEGORY[self.status]}
        categories.update(_STATUS_CATEGORY[m.kind] for m in self.mismatches)
        for candidate in _CATEGORY_SEVERITY:
            if candidate in categories:
                return candidate
        return HaltCategory.TRANSIENT  # unreachable; still not "NONE"

    @property
    def owner_must_clear(self) -> bool:
        """True only for sticky drift -- the one case a human has to acknowledge."""
        return self.category is HaltCategory.DRIFT

    @classmethod
    def clean(cls) -> ReconciliationResult:
        """The one reconciled outcome: nothing differed AND cash was compared."""
        return cls(
            status=ReconciliationStatus.CLEAN,
            positions_reconciled=True,
            mismatches=(),
            cash_checked=True,
        )

    @classmethod
    def cash_not_verified(
        cls, *, positions_reconciled: bool = True
    ) -> ReconciliationResult:
        """Positions agreed, but cash was never compared -> NOT reconciled.

        Not a drift halt: there is nothing for the owner to clear. It resolves
        structurally, when the DB gains a cash ledger to compare against.
        """
        return cls(
            status=ReconciliationStatus.CASH_NOT_VERIFIED,
            positions_reconciled=positions_reconciled,
            mismatches=(),
            cash_checked=False,
        )

    @classmethod
    def halt(
        cls,
        status: ReconciliationStatus,
        mismatches: Sequence[Mismatch],
        *,
        positions_reconciled: bool = False,
        cash_checked: bool = False,
    ) -> ReconciliationResult:
        """A not-reconciled outcome: headline ``status`` plus every difference."""
        return cls(
            status=status,
            positions_reconciled=positions_reconciled,
            mismatches=tuple(mismatches),
            cash_checked=cash_checked,
        )

    def as_log_fields(self) -> dict[str, object]:
        """Structured, log-safe summary (no account ids, no secrets)."""
        return {
            "reconciled": self.reconciled,
            "status": self.status.value,
            "category": self.category.value,
            "owner_must_clear": self.owner_must_clear,
            "positions_reconciled": self.positions_reconciled,
            "cash_checked": self.cash_checked,
            "mismatch_count": len(self.mismatches),
            "mismatches": [m.as_log_fields() for m in self.mismatches],
        }


# --------------------------------------------------------------------------- #
# Pure comparison
# --------------------------------------------------------------------------- #


def _normalise_symbol(symbol: str | None) -> str:
    """Key symbols consistently across both sides of the comparison.

    Webull returns uppercase US tickers; the DB stores whatever the rules engine
    wrote. Upper-casing and stripping whitespace prevents a *false* halt caused
    purely by formatting. This normalises the comparison KEY only -- it never
    changes, merges or hides a quantity, and an unrecognisable symbol (e.g. an
    empty one) simply fails to match and is reported as a mismatch.
    """
    return (symbol or "").strip().upper()


def _is_usable(value: Decimal | None) -> bool:
    """True only if ``value`` is a real, comparable number (not None/NaN/Inf)."""
    return value is not None and value.is_finite()


def _index_by_symbol(
    entries: Iterable[tuple[str, Decimal | None]], *, side: str
) -> tuple[dict[str, Decimal | None], list[Mismatch]]:
    """Build a symbol -> quantity map, refusing to silently drop duplicates.

    A second entry for the same symbol would be swallowed by a plain dict
    comprehension -- that is precisely the kind of silent fail-open this module
    exists to prevent, so duplicates are reported instead of merged (summing
    them would be improvising a resolution).
    """
    indexed: dict[str, Decimal | None] = {}
    duplicates: list[Mismatch] = []
    for symbol, quantity in entries:
        key = _normalise_symbol(symbol)
        if key in indexed:
            duplicates.append(
                Mismatch(
                    kind=ReconciliationStatus.DUPLICATE_POSITION,
                    symbol=key,
                    expected=indexed[key],
                    actual=quantity,
                    detail=(
                        f"{side} reported {key} more than once; the position is "
                        "ambiguous and was not merged"
                    ),
                )
            )
            continue
        indexed[key] = quantity
    return indexed, duplicates


def _compare_cash(
    *,
    broker_cash: Decimal | None,
    expected_cash: Decimal | None,
    cash_tolerance: Decimal,
) -> tuple[list[Mismatch], bool]:
    """Compare the cash leg. Returns (mismatches, whether cash was compared).

    Broker cash must ALWAYS be present and finite: invariant #6 names cash as
    broker truth, so a snapshot whose cash we cannot read is not a snapshot we
    can reconcile against. That case is reported as ``BROKER_UNREADABLE`` (a
    TRANSIENT failure to *read*), not ``CASH_MISMATCH`` -- reserving
    ``CASH_MISMATCH`` for "both figures were read and they differ", which is
    real, sticky DRIFT. Mislabelling an unreadable figure as drift would demand
    an owner acknowledgement for what is usually a bad payload.

    ``expected_cash is None`` is different again -- it means the DB holds no cash
    expectation yet (no order path, no cash ledger), so the equality check is
    genuinely not applicable and is reported as *not checked* rather than as
    passing. The caller turns that into ``CASH_NOT_VERIFIED``.

    An unusable *tolerance* stays ``CASH_MISMATCH`` on purpose: it is a bug in
    the money path (we cannot even state the rule), and the loudest, most
    human-blocking outcome is the right one -- it must never be confused with
    the ordinary "no ledger yet" state.
    """
    # An unusable tolerance means we cannot even state the rule -- halt rather
    # than fall back to a default (a caller passing garbage here is a bug in the
    # money path, and a negative tolerance would make every comparison fail
    # anyway).
    if (
        cash_tolerance is None
        or not cash_tolerance.is_finite()
        or cash_tolerance < 0
    ):
        return (
            [
                Mismatch(
                    kind=ReconciliationStatus.CASH_MISMATCH,
                    symbol=None,
                    expected=expected_cash,
                    actual=broker_cash,
                    detail="cash tolerance is missing, non-finite or negative",
                )
            ],
            False,
        )

    if broker_cash is None or not broker_cash.is_finite():
        return (
            [
                Mismatch(
                    kind=ReconciliationStatus.BROKER_UNREADABLE,
                    symbol=None,
                    expected=expected_cash,
                    actual=broker_cash,
                    detail=(
                        "broker cash balance is missing or non-finite; cash "
                        "truth unreadable"
                    ),
                )
            ],
            False,
        )

    if expected_cash is None:
        # Not a failure and not a pass: nothing to compare against yet.
        return [], False

    if not expected_cash.is_finite():
        return (
            [
                Mismatch(
                    kind=ReconciliationStatus.CASH_MISMATCH,
                    symbol=None,
                    expected=expected_cash,
                    actual=broker_cash,
                    detail="expected cash balance is non-finite",
                )
            ],
            False,
        )

    # Both sides proven finite above, so this arithmetic cannot raise.
    if abs(broker_cash - expected_cash) > cash_tolerance:
        return (
            [
                Mismatch(
                    kind=ReconciliationStatus.CASH_MISMATCH,
                    symbol=None,
                    expected=expected_cash,
                    actual=broker_cash,
                    detail=(
                        "cash balance differs from DB expectation by more than "
                        f"the {cash_tolerance} tolerance"
                    ),
                )
            ],
            True,
        )
    return [], True


def _worst_status(mismatches: Sequence[Mismatch]) -> ReconciliationStatus:
    """The highest-priority mismatch kind present (never ``CLEAN``)."""
    kinds = {m.kind for m in mismatches}
    for status in _STATUS_PRIORITY:
        if status in kinds:
            return status
    # Unreachable with the kinds this module produces, but never fall through to
    # CLEAN: an unrecognised mismatch is still a mismatch.
    return mismatches[0].kind


def compare_positions(
    *,
    broker_positions: Sequence[BrokerPosition] | None,
    expected_positions: Sequence[ExpectedPosition] | None,
    broker_cash: Decimal | None,
    expected_cash: Decimal | None = None,
    cash_tolerance: Decimal = DEFAULT_CASH_TOLERANCE,
) -> ReconciliationResult:
    """Compare broker truth against DB intent. Pure; fail-closed; never raises.

    Keyword-only on purpose: in the money path an argument in the wrong position
    is a bug that loses money, so callers must name every input.

    Parameters
    ----------
    broker_positions:
        Every open position the broker reports for the pinned account (Webull =
        truth). ``None`` means the broker read failed / was not attempted ->
        ``BROKER_UNREADABLE``. An empty sequence is a real answer ("flat"), and
        is NOT the same as ``None``.
    expected_positions:
        The open positions the DB's intent record implies we hold. ``None``
        means the DB read failed -> ``DB_UNREADABLE``. An empty sequence is a
        real answer ("we intend to hold nothing") -- which is exactly the
        situation today, since no order path exists yet.
    broker_cash:
        The broker's cash balance (``AccountBalance.total_cash``). ``None`` or
        non-finite -> ``BROKER_UNREADABLE`` (cash truth unreadable; transient).
    expected_cash:
        The DB's cash expectation, or ``None`` when the DB holds none yet. When
        ``None`` the cash equality check cannot run, so the result is
        ``CASH_NOT_VERIFIED`` and ``reconciled=False`` -- explicitly partial,
        never silently "clean" (owner ruling, 2026-07-21). The positions verdict
        is still reported via ``positions_reconciled``.
    cash_tolerance:
        Absolute cash difference still treated as clean; see
        :data:`DEFAULT_CASH_TOLERANCE`. Missing/non-finite/negative ->
        ``CASH_MISMATCH``.

    Returns
    -------
    ReconciliationResult
        ``clean()`` iff nothing differs AND cash was actually compared;
        ``cash_not_verified()`` when positions agreed but there was no cash
        expectation to compare against; otherwise ``halt(<worst status>, <every
        difference found>)``. Position quantities are compared EXACTLY; only
        cash has a tolerance.

    Notes
    -----
    A broker position with quantity ``0`` is reported as a position, not
    ignored. Suppressing it would be an undocumented rule invented inside the
    money path; a spurious halt costs nothing but a human glance, whereas
    silently dropping broker rows is how real drift hides.
    """
    if broker_positions is None:
        return ReconciliationResult.halt(
            ReconciliationStatus.BROKER_UNREADABLE,
            [
                Mismatch(
                    kind=ReconciliationStatus.BROKER_UNREADABLE,
                    symbol=None,
                    expected=None,
                    actual=None,
                    detail="broker positions unavailable; broker truth unproven",
                )
            ],
        )

    if expected_positions is None:
        return ReconciliationResult.halt(
            ReconciliationStatus.DB_UNREADABLE,
            [
                Mismatch(
                    kind=ReconciliationStatus.DB_UNREADABLE,
                    symbol=None,
                    expected=None,
                    actual=None,
                    detail="DB intent unavailable; expected positions unproven",
                )
            ],
        )

    broker_by_symbol, broker_dupes = _index_by_symbol(
        ((p.symbol, p.quantity) for p in broker_positions), side="broker"
    )
    expected_by_symbol, expected_dupes = _index_by_symbol(
        ((e.symbol, e.quantity) for e in expected_positions), side="db"
    )
    duplicates = broker_dupes + expected_dupes
    if duplicates:
        # Ambiguous input: every per-symbol verdict below would be arbitrary.
        return ReconciliationResult.halt(
            ReconciliationStatus.DUPLICATE_POSITION,
            duplicates,
            positions_reconciled=False,
        )

    mismatches: list[Mismatch] = []
    for symbol in sorted(set(broker_by_symbol) | set(expected_by_symbol)):
        in_broker = symbol in broker_by_symbol
        in_db = symbol in expected_by_symbol
        broker_qty = broker_by_symbol.get(symbol)
        expected_qty = expected_by_symbol.get(symbol)

        if in_broker and not in_db:
            mismatches.append(
                Mismatch(
                    kind=ReconciliationStatus.UNEXPECTED_BROKER_POSITION,
                    symbol=symbol,
                    expected=None,
                    actual=broker_qty,
                    detail=(
                        "broker holds a position the DB has no record of "
                        "intending (manual trade, leftover state, or an "
                        "unrecorded fill)"
                    ),
                )
            )
            continue

        if in_db and not in_broker:
            mismatches.append(
                Mismatch(
                    kind=ReconciliationStatus.MISSING_BROKER_POSITION,
                    symbol=symbol,
                    expected=expected_qty,
                    actual=None,
                    detail=(
                        "DB expects an open position the broker does not report "
                        "(closed out of band, or an exit we never recorded)"
                    ),
                )
            )
            continue

        # Present on both sides: quantities must match exactly, and both must be
        # usable numbers -- an unusable quantity proves nothing, so it halts.
        if not _is_usable(broker_qty) or not _is_usable(expected_qty):
            mismatches.append(
                Mismatch(
                    kind=ReconciliationStatus.QUANTITY_MISMATCH,
                    symbol=symbol,
                    expected=expected_qty,
                    actual=broker_qty,
                    detail="a position quantity is missing or non-finite",
                )
            )
            continue
        if broker_qty != expected_qty:
            mismatches.append(
                Mismatch(
                    kind=ReconciliationStatus.QUANTITY_MISMATCH,
                    symbol=symbol,
                    expected=expected_qty,
                    actual=broker_qty,
                    detail="broker quantity differs from the DB's expected quantity",
                )
            )

    # The positions verdict is decided before the cash leg, and kept separate, so
    # a postmortem can still read "positions matched, cash was never compared".
    positions_reconciled = not mismatches

    cash_mismatches, cash_checked = _compare_cash(
        broker_cash=broker_cash,
        expected_cash=expected_cash,
        cash_tolerance=cash_tolerance,
    )
    mismatches.extend(cash_mismatches)

    if mismatches:
        return ReconciliationResult.halt(
            _worst_status(mismatches),
            mismatches,
            positions_reconciled=positions_reconciled,
            cash_checked=cash_checked,
        )
    if not cash_checked:
        # Positions agreed and nothing differed, but cash was never compared --
        # a partial verification is not a verification (owner ruling).
        return ReconciliationResult.cash_not_verified(
            positions_reconciled=positions_reconciled
        )
    return ReconciliationResult.clean()


# --------------------------------------------------------------------------- #
# Orchestrator (thin: fetch -> pure compare -> log -> return)
# --------------------------------------------------------------------------- #


def _mask_account_id(account_id: str) -> str:
    """Last-4 mask for logs -- never emit a full Webull account id/number.

    (Deliberate twin of the helper in :mod:`app.worker.snapshot`; duplicated
    rather than cross-imported so neither job depends on the other.)
    """
    tail = account_id[-4:]
    return f"***{tail}" if len(account_id) >= 4 else "***"


def _log_outcome(result: ReconciliationResult, *, masked: str, is_paper: bool) -> None:
    """Emit the run's outcome, at a level matching what the caller must do.

    CRITICAL for real drift (sticky; a human must clear it), WARNING for a
    transient failure or a never-verified cash leg (both keep the worker
    halted, neither is an owner-clearable drift event), INFO when clean. Every
    line carries the category so an alerting rule can route on it.
    """
    if result.owner_must_clear:
        log.critical(
            "reconcile.mismatch",
            halt_reason=HALT_REASON,
            invariant="6",
            latch="sticky: must NOT auto-clear; owner clears via the freeze flag",
            account_id_masked=masked,
            is_paper=is_paper,
            **result.as_log_fields(),
        )
        return
    if not result.reconciled:
        # Transient (could not look) or cash never verified (nothing to clear):
        # still halted, but not an owner-acknowledgement event. `halt_reason`
        # stays RECONCILE_MISMATCH because that is the machine-readable enum for
        # "reconciliation did not pass"; `category` is what distinguishes them.
        latch = (
            "resolves structurally when the DB gains a cash ledger; nothing to clear"
            if result.category is HaltCategory.NOT_VERIFIED
            else "may auto-clear on a later clean run"
        )
        log.warning(
            "reconcile.not_reconciled",
            halt_reason=HALT_REASON,
            invariant="6",
            latch=latch,
            account_id_masked=masked,
            is_paper=is_paper,
            **result.as_log_fields(),
        )
        return
    log.info(
        "reconcile.clean",
        account_id_masked=masked,
        is_paper=is_paper,
        **result.as_log_fields(),
    )


async def reconcile(
    *,
    settings: Settings | None = None,
    client: WebullClient | None = None,
    db: Database | None = None,
) -> ReconciliationResult:
    """Reconcile the ONE pinned account against DB intent. Read-only; never raises.

    Steps: read `settings.webull_account_id` (the account this bot is pinned to
    -- never "all accounts", never a guess), fetch that account's snapshot from
    Webull, derive expected open positions from the DB, run the pure
    :func:`compare_positions`, log the outcome, return the result.

    Mutates nothing: no DB write, no order, no cancel, no auto-correction. The
    caller decides what to do with a halt (today: keep the worker halted; the
    ``reconciled`` bit is what :func:`app.worker.safety_gate.evaluate_order_
    safety` consumes, and it denies every order unless that bit is exactly
    ``True``).

    `settings`, `client` and `db` are injectable for testing; in production all
    three default to real instances built from the environment. A `db` this
    function opened itself is closed before returning; an injected one is left
    to the caller.

    Failures are outcomes, not exceptions: a broker error ->
    ``BROKER_UNREADABLE``, a DB error -> ``DB_UNREADABLE``, an unpinned account
    -> ``ACCOUNT_NOT_PINNED``. All are "not reconciled" and all are TRANSIENT.
    Today, with no DB cash ledger, the best achievable outcome is
    ``CASH_NOT_VERIFIED`` (``reconciled=False``, ``positions_reconciled=True``)
    -- deliberately: see "A PARTIAL verification is not a verification".

    The caller must honour THE LATCH RULE (module docstring): a ``DRIFT``
    category result is sticky and must never be allowed to auto-clear.
    """
    settings = settings or load_settings()
    client = client or WebullClient(settings)
    is_paper = not client.is_live

    account_id = (settings.webull_account_id or "").strip()
    if not account_id:
        result = ReconciliationResult.halt(
            ReconciliationStatus.ACCOUNT_NOT_PINNED,
            [
                Mismatch(
                    kind=ReconciliationStatus.ACCOUNT_NOT_PINNED,
                    symbol=None,
                    expected=None,
                    actual=None,
                    detail=(
                        "WEBULL_ACCOUNT_ID is unset; refusing to guess which "
                        "account to reconcile"
                    ),
                )
            ],
        )
        _log_outcome(result, masked="***", is_paper=is_paper)
        return result

    masked = _mask_account_id(account_id)

    owns_db = db is None
    if db is None:
        try:
            db = await Database.connect(settings.database_url)
        except Exception as exc:  # any failure to reach the DB is DB_UNREADABLE
            result = _db_unreadable(exc, stage="connect")
            _log_outcome(result, masked=masked, is_paper=is_paper)
            return result

    try:
        try:
            snapshot = await asyncio.to_thread(
                client.get_account_snapshot,
                AccountSnapshotRequest(account_id=account_id),
            )
        except Exception as exc:
            # Broad on purpose: ANY failure to obtain broker truth (SDK error,
            # timeout, malformed payload, bad request model) is the same
            # outcome -- broker unproven, so not reconciled. Only the exception
            # TYPE is logged; messages can carry request detail.
            # BaseException (CancelledError, KeyboardInterrupt) still propagates.
            result = ReconciliationResult.halt(
                ReconciliationStatus.BROKER_UNREADABLE,
                [
                    Mismatch(
                        kind=ReconciliationStatus.BROKER_UNREADABLE,
                        symbol=None,
                        expected=None,
                        actual=None,
                        detail=(
                            "broker account snapshot failed "
                            f"({type(exc).__name__}); broker truth unproven"
                        ),
                    )
                ],
            )
            _log_outcome(result, masked=masked, is_paper=is_paper)
            return result

        try:
            intents = await db.get_open_position_intents(is_paper=is_paper)
        except Exception as exc:
            result = _db_unreadable(exc, stage="read")
            _log_outcome(result, masked=masked, is_paper=is_paper)
            return result

        expected = tuple(
            ExpectedPosition(symbol=symbol, quantity=quantity)
            for symbol, quantity in intents.items()
        )
        result = compare_positions(
            broker_positions=snapshot.positions,
            expected_positions=expected,
            broker_cash=snapshot.balance.total_cash,
            # No DB cash ledger exists yet (no order path -> no cash intent), so
            # there is nothing to compare the broker's cash against. Passing the
            # broker's own equity snapshot back in here would be reconciling the
            # broker against itself -- see HANDOFF/wiring notes.
            expected_cash=None,
        )
        _log_outcome(result, masked=masked, is_paper=is_paper)
        return result
    finally:
        if owns_db:
            # N3: a failing close() in `finally` would REPLACE the verdict we
            # just computed with an exception, breaking "reconcile never
            # raises" -- and a torn-down pool is irrelevant to whether the books
            # matched. Log it and let the return value stand.
            try:
                await db.close()
            except Exception as exc:
                log.warning(
                    "reconcile.db_close_failed",
                    account_id_masked=masked,
                    error_type=type(exc).__name__,
                )


def _db_unreadable(exc: BaseException, *, stage: str) -> ReconciliationResult:
    """Map any DB failure to the ``DB_UNREADABLE`` outcome (type only, no message)."""
    return ReconciliationResult.halt(
        ReconciliationStatus.DB_UNREADABLE,
        [
            Mismatch(
                kind=ReconciliationStatus.DB_UNREADABLE,
                symbol=None,
                expected=None,
                actual=None,
                detail=(
                    f"DB {stage} failed ({type(exc).__name__}); expected "
                    "positions unproven"
                ),
            )
        ],
    )
