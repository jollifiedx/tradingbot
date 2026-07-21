"""Pre-order safety gate: the pure decision function every order must pass.

This is the single choke point that answers one question -- *is it safe to place
this order right now?* -- and nothing else. It is deliberately **pure**:

- no I/O, no network, no DB, no wall-clock read, no logging, no globals;
- inputs in, an immutable :class:`GateDecision` out;
- every value the checks need (settings row, order notional, deployed capital,
  today's loss, seconds since the last tick, reconciliation flag) is *passed in*.

The (future) order path is what fetches fresh state -- re-reads `settings`,
computes deployed capital from reconciled positions, reads the market-data
staleness clock -- and then calls this function. Keeping the decision logic pure
is exactly what makes it exhaustively unit-testable: the money-losing branches can
all be exercised without a broker, a database, or a clock. Do not add I/O here.

Fail-closed contract (CLAUDE.md invariants #2 and #3)
-----------------------------------------------------
The gate denies on *any* failing check **and** on any required input we cannot
prove safe -- both a missing (``None``) value **and** a non-finite one (``NaN``
or ``+/-Inf``). Neither is ever treated as "zero" or "skip"; each is treated as
the corresponding check *failing*, because an input we could not compute (or
that computed to garbage) is an input we cannot prove safe. Non-finite matters
as much as ``None``: a NaN freshness clock silently satisfies ``nan > threshold
== False`` (fails OPEN -- the worst direction), and a ``Decimal('NaN')`` makes
the ``>=``/``>`` comparisons *raise* instead of denying -- both violate the
"inputs in, GateDecision out, never raise" contract. loss/capital come from
prices and positions and freshness from timestamps, so all can go NaN/Inf on
exactly the bad data where we most need to halt. Finiteness is checked with
``math.isfinite`` (float) / ``Decimal.is_finite`` (Decimals), folded into the
same ladder rung the input belongs to:

- ``settings is None``               -> SETTINGS_UNREADABLE (can't read the rules)
- ``reconciled`` not exactly True    -> UNRECONCILED        (books unproven)
- ``seconds_since_tick`` None/non-finite -> STALE_DATA       (freshness unproven)
- ``loss_so_far`` None/non-finite    -> DAILY_LOSS           (within-limit unproven)
- ``order_notional`` None/non-finite -> PER_TRADE_CAP        (size unproven)
- ``deployed_capital`` None/non-finite -> BUY_POWER_CAP      (exposure unproven)

Priority order (deterministic; the reported reason is the FIRST failing check)
------------------------------------------------------------------------------
When more than one check would fail, the gate reports the highest-priority one.
The order is chosen so that a broader "the system is not safe to trade at all"
condition is reported before a narrower order-specific one, and so that each
check only runs once every number it depends on has already been proven
trustworthy by the checks above it:

1. SETTINGS_UNREADABLE -- without the settings row we don't even know the
   thresholds; nothing else can be evaluated. Must be first.
2. FROZEN              -- the owner's explicit kill switch. The strongest
   deliberate signal there is; once we can read it, it outranks everything.
3. UNRECONCILED        -- our books don't match the broker. Every downstream
   number (deployed capital, loss) is then untrustworthy, so reject before
   relying on any of them.
4. STALE_DATA          -- market data isn't fresh, so any price-derived figure
   (notional, unrealized loss) is suspect. Reject before those checks use them.
5. DAILY_LOSS          -- account-wide circuit breaker; independent of this one
   order's size. Halt the whole session before sizing an individual order.
6. PER_TRADE_CAP       -- order-specific: is THIS order too large on its own?
7. BUY_POWER_CAP       -- order-specific: would this order push total deployed
   capital over the cap? Last because it depends on both a trustworthy
   deployed-capital figure and the order's own notional.

Boundary semantics (documented and tested to the cent / second)
---------------------------------------------------------------
- PER_TRADE_CAP:  denied when ``order_notional > max_per_trade_cap``.
  Exactly at the cap is ALLOWED; one cent over is denied.
- BUY_POWER_CAP:  denied when ``deployed_capital + order_notional > buy_power_cap``.
  Exactly at the cap is ALLOWED; one cent over is denied.
- DAILY_LOSS:     denied when ``loss_so_far >= max_daily_loss``.
  Exactly AT the limit is a BREACH (denied). This matches the project's
  "never trade through uncertainty" posture: the limit is the line you do not
  cross, so touching it halts rather than requiring you to exceed it before
  stopping. ``loss_so_far`` is the day's loss *magnitude* -- positive means down
  money; zero or negative (a profit) is comfortably under any non-negative cap.
- STALE_DATA:     denied when ``seconds_since_tick > staleness_threshold_seconds``.
  Exactly at the threshold is FRESH (allowed); one second over is stale (denied).

Money is :class:`~decimal.Decimal` throughout -- never float. Keep this module
boring: an explicit priority ladder, no data-driven cleverness in the money path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.core.models import BotSettings


class GateReason(StrEnum):
    """Why the gate allowed or denied an order.

    On a denial this is the FIRST failing check in the documented priority order
    (see the module docstring), so it is a single, deterministic explanation --
    not a set of everything that happened to be wrong.
    """

    OK = "ok"
    SETTINGS_UNREADABLE = "settings_unreadable"
    FROZEN = "frozen"
    UNRECONCILED = "unreconciled"
    STALE_DATA = "stale_data"
    DAILY_LOSS = "daily_loss"
    PER_TRADE_CAP = "per_trade_cap"
    BUY_POWER_CAP = "buy_power_cap"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Immutable result of :func:`evaluate_order_safety`.

    ``allowed`` is the only bit the order path acts on; ``reason`` explains it
    (``OK`` iff allowed). Frozen so a decision cannot be mutated after the fact --
    a safety verdict is a value, not a mutable flag.
    """

    allowed: bool
    reason: GateReason

    @classmethod
    def allow(cls) -> GateDecision:
        """The one allowed outcome: every check passed."""
        return cls(allowed=True, reason=GateReason.OK)

    @classmethod
    def deny(cls, reason: GateReason) -> GateDecision:
        """A denial with the first failing check's reason."""
        return cls(allowed=False, reason=reason)


def evaluate_order_safety(
    *,
    settings: BotSettings | None,
    order_notional: Decimal | None,
    deployed_capital: Decimal | None,
    loss_so_far: Decimal | None,
    seconds_since_tick: float | None,
    reconciled: bool | None,
) -> GateDecision:
    """Decide whether one proposed order is safe to place. Pure; fail-closed.

    Keyword-only on purpose: in the money path an argument in the wrong position
    is a bug that loses money, so callers must name every input.

    Parameters
    ----------
    settings:
        The live risk/control row (``settings`` table), re-read by the caller
        immediately before this call. ``None`` means the row could not be read
        -> DENY(SETTINGS_UNREADABLE) (CLAUDE.md invariant #2, fail closed).
    order_notional:
        The proposed order's notional value in account currency (price *
        quantity) -- the money this single order would commit. Checked against
        ``settings.max_per_trade_cap``. ``None`` or non-finite (``NaN``/``Inf``)
        -> DENY(PER_TRADE_CAP).
    deployed_capital:
        Total capital already deployed / committed across current positions and
        working orders, from reconciled state. ``deployed_capital +
        order_notional`` is checked against ``settings.buy_power_cap``. ``None``
        or non-finite -> DENY(BUY_POWER_CAP).
    loss_so_far:
        Today's realized + unrealized loss so far, as a magnitude (positive =
        down money; <= 0 = flat/up). Checked against ``settings.max_daily_loss``.
        ``None`` or non-finite -> DENY(DAILY_LOSS).
    seconds_since_tick:
        Seconds elapsed since the last market-data tick, computed by the caller
        (the gate never reads a clock). Checked against
        ``settings.staleness_threshold_seconds``. ``None`` or non-finite
        (a ``NaN``/``Inf`` clock never counts as fresh) -> DENY(STALE_DATA).
    reconciled:
        ``True`` iff the most recent startup/periodic reconciliation was clean
        (CLAUDE.md invariant #6). Anything else -- ``False`` or ``None`` --
        -> DENY(UNRECONCILED).

    Returns
    -------
    GateDecision
        ``allow()`` iff every check passed, else ``deny(<first failing reason>)``.
    """
    # 1. Settings unreadable -> we don't know the rules; deny before anything else.
    if settings is None:
        return GateDecision.deny(GateReason.SETTINGS_UNREADABLE)

    # 2. Frozen -> the owner's explicit kill switch outranks every other check.
    if settings.frozen:
        return GateDecision.deny(GateReason.FROZEN)

    # 3. Unreconciled (or unknown) -> books unproven; downstream numbers untrusted.
    if reconciled is not True:
        return GateDecision.deny(GateReason.UNRECONCILED)

    # 4. Stale (unknown, non-finite, or over-threshold) market data -> price-
    #    derived figures are suspect. A NaN/Inf clock is treated as stale (never
    #    trusted to satisfy `nan > threshold == False`). At the threshold is
    #    fresh; strictly over is stale.
    if (
        seconds_since_tick is None
        or not math.isfinite(seconds_since_tick)
        or seconds_since_tick > settings.staleness_threshold_seconds
    ):
        return GateDecision.deny(GateReason.STALE_DATA)

    # 5. Daily-loss circuit breaker -> at-or-past the limit halts the session.
    #    A non-finite loss is treated as a breach (deny before it can raise).
    if (
        loss_so_far is None
        or not loss_so_far.is_finite()
        or loss_so_far >= settings.max_daily_loss
    ):
        return GateDecision.deny(GateReason.DAILY_LOSS)

    # 6. Per-trade cap -> this order, on its own, may not exceed the cap.
    #    A non-finite notional is rejected (deny before it can raise).
    #    At the cap is allowed; over the cap is denied.
    if (
        order_notional is None
        or not order_notional.is_finite()
        or order_notional > settings.max_per_trade_cap
    ):
        return GateDecision.deny(GateReason.PER_TRADE_CAP)

    # 7. Buy-power cap -> deployed capital plus this order may not exceed the cap.
    #    A non-finite deployed figure is rejected first, so the sum below (with an
    #    already-finite order_notional from rung 6) can never raise. At the cap is
    #    allowed; over the cap is denied.
    if (
        deployed_capital is None
        or not deployed_capital.is_finite()
        or deployed_capital + order_notional > settings.buy_power_cap
    ):
        return GateDecision.deny(GateReason.BUY_POWER_CAP)

    # All checks passed.
    return GateDecision.allow()
