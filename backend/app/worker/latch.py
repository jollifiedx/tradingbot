"""The halt latch: decides whether the worker may trade, and when to self-freeze.

Reconciliation (`app/worker/reconciliation.py`) is stateless -- it only reports
what one run observed. THIS module turns those reports into a posture that is
*sticky across runs and across restarts*, which is the part the owner ruled on
(docs/decisions.md, 2026-07-21) and the part the architect flagged as most
likely to go wrong silently.

THE LATCH RULE
--------------
- ``HaltCategory.DRIFT`` -- a real disagreement between the broker and our
  books. STICKY: the worker engages the freeze flag (persisted in `settings`),
  so it survives restarts and shows on the dashboard. Only the OWNER clears it,
  by unfreezing. A later CLEAN run must never re-enable trading on its own.
- ``HaltCategory.TRANSIENT`` -- we could not look (broker/DB unreadable,
  account unpinned). Halts this run, writes NO freeze, and may clear itself on
  a later CLEAN run.
- ``HaltCategory.NOT_VERIFIED`` -- the structural cash gap (no DB cash ledger
  until the order path lands). Halts, writes no freeze, and is NOT something
  the owner can "clear" -- it resolves when the ledger exists.

How stickiness actually works (the mechanism, not a convention): a DRIFT result
sets ``settings.frozen = true``. Every later decision -- including the first one
after a restart -- checks the frozen flag FIRST, so a subsequent CLEAN
reconciliation cannot unlatch it. There is no code path in the worker that
clears the flag; :data:`LatchDecision` has no "unfreeze" field to set. Only the
owner, through the dashboard, sets ``frozen = false``.

Fail closed: an unknown frozen flag (``None``, i.e. settings unreadable) or a
missing reconciliation result halts. Uncertainty is never permission.

This module is pure -- no I/O, no clock, no DB. The caller applies the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.worker.reconciliation import HaltCategory, ReconciliationResult


class LatchReason(StrEnum):
    """Why the worker holds the posture it does. One reason per decision."""

    CLEAR = "clear"
    FROZEN = "frozen"
    SETTINGS_UNREADABLE = "settings_unreadable"
    NO_RECONCILIATION = "no_reconciliation"
    DRIFT_HALT = "drift_halt"
    TRANSIENT_HALT = "transient_halt"
    CASH_NOT_VERIFIED = "cash_not_verified"


@dataclass(frozen=True, slots=True)
class LatchDecision:
    """What the worker must do now.

    :attr:`may_trade` is the only bit that permits trading, and it is True in
    exactly one case (see :func:`decide_posture`). :attr:`engage_freeze` asks
    the caller to persist a self-halt by writing ``settings.frozen = true``.

    There is deliberately NO field that clears the freeze: the worker can only
    ever engage a safety mechanism, never release one. Releasing is the owner's
    act alone.
    """

    may_trade: bool
    engage_freeze: bool
    reason: LatchReason

    def __post_init__(self) -> None:
        """Reject any decision that both permits trading and self-halts."""
        if self.may_trade and self.engage_freeze:
            raise ValueError("a decision cannot permit trading and freeze at once")
        if self.may_trade and self.reason is not LatchReason.CLEAR:
            raise ValueError("trading is permitted only for LatchReason.CLEAR")


_HALTED_ONLY = frozenset(
    {
        LatchReason.FROZEN,
        LatchReason.SETTINGS_UNREADABLE,
        LatchReason.NO_RECONCILIATION,
        LatchReason.TRANSIENT_HALT,
        LatchReason.CASH_NOT_VERIFIED,
    }
)


def decide_posture(
    *,
    result: ReconciliationResult | None,
    currently_frozen: bool | None,
) -> LatchDecision:
    """Decide whether the worker may trade, given the latest reconciliation.

    Checked in a deliberate order; the first rule that applies wins.

    1. ``currently_frozen`` is not exactly ``False`` -> halt. ``True`` is the
       owner's kill switch OR a previously latched drift halt; ``None`` means
       the settings row was unreadable, which fails closed (Invariant 2). This
       rule runs FIRST, which is precisely what makes a drift halt sticky: once
       the flag is set, no later reconciliation result is even consulted.
    2. No reconciliation result -> halt. The worker never trades on "no
       opinion" (a missed or errored run must not read as permission).
    3. ``DRIFT`` -> halt AND engage the freeze, so the halt outlives this
       process and the owner has to acknowledge it.
    4. ``TRANSIENT`` -> halt, no freeze. May clear on a later CLEAN run.
    5. ``NOT_VERIFIED`` -> halt, no freeze. Resolves structurally, not by the
       owner.
    6. Otherwise the run is CLEAN and the bot is unfrozen -> trading permitted.

    Pure: no I/O, no clock, no DB. Never raises on any combination of inputs.
    """
    if currently_frozen is None:
        return LatchDecision(
            may_trade=False,
            engage_freeze=False,
            reason=LatchReason.SETTINGS_UNREADABLE,
        )
    if currently_frozen:
        return LatchDecision(
            may_trade=False, engage_freeze=False, reason=LatchReason.FROZEN
        )
    if result is None:
        return LatchDecision(
            may_trade=False,
            engage_freeze=False,
            reason=LatchReason.NO_RECONCILIATION,
        )

    category = result.category
    if category is HaltCategory.DRIFT:
        # The only case that writes state: persist the halt so it survives a
        # restart and surfaces to the owner as a freeze they must clear.
        return LatchDecision(
            may_trade=False, engage_freeze=True, reason=LatchReason.DRIFT_HALT
        )
    if category is HaltCategory.TRANSIENT:
        return LatchDecision(
            may_trade=False, engage_freeze=False, reason=LatchReason.TRANSIENT_HALT
        )
    if category is HaltCategory.NOT_VERIFIED:
        return LatchDecision(
            may_trade=False, engage_freeze=False, reason=LatchReason.CASH_NOT_VERIFIED
        )

    if not result.reconciled:
        # Defensive: a non-NONE category is handled above, so reaching here with
        # an unreconciled result would mean a new category was added without a
        # rule. Halt rather than fall through to permission.
        return LatchDecision(
            may_trade=False, engage_freeze=False, reason=LatchReason.TRANSIENT_HALT
        )
    return LatchDecision(
        may_trade=True, engage_freeze=False, reason=LatchReason.CLEAR
    )
