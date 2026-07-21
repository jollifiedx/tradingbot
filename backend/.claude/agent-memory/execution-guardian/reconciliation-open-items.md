---
name: reconciliation-open-items
description: The cash ledger is a HARD BLOCKER for trading — with no DB cash expectation, reconcile can never return reconciled=True, so the safety gate denies every order
metadata:
  type: project
---

`app/worker/reconciliation.py` landed 2026-07-21 (pure `compare_positions` + thin async `reconcile`), wired
nowhere. Two deliberate gaps, both documented in-module:

1. **Cash is read but never compared — and that now BLOCKS TRADING.** Esther ruled (2026-07-21, architect B1
   Option A) that a partial verification is not a verification: when `cash_checked` is False the result is
   `CASH_NOT_VERIFIED` with `reconciled=False`, enforced structurally (`reconciled` is a derived property,
   `__post_init__` rejects inconsistent instances). Since the DB has no cash-intent ledger, **the best
   achievable outcome today is not-reconciled**, and `evaluate_order_safety` denies every order on
   `UNRECONCILED`. Building an order path without a cash ledger therefore produces a bot that cannot trade —
   plan the two together.
2. **Long positions only.** `trades.quantity` is positive-constrained with no side column, so
   `Database.get_open_position_intents()` cannot represent a short. A drift-guard test asserts `trades` has no
   `side`/`direction` column, so adding one breaks the build on purpose.

**Why:** both were preferred over inventing a comparison the DB cannot honestly support — an invented
expectation either false-halts constantly or hides real drift. The cash tolerance (`DEFAULT_CASH_TOLERANCE`,
one cent) absorbs representation noise only; widening it to make reconciliation pass is a safety weakening and
an ESCALATION, never a tuning knob.

**How to apply:** the cash expectation must come from the DB's own trade/fee ledger, never from the broker's
equity snapshot (that would reconcile the broker against itself). Also honour THE LATCH RULE when wiring the
scheduler: `HaltCategory.DRIFT` results are sticky and must never auto-clear; `TRANSIENT` may; `NOT_VERIFIED`
is not owner-clearable. Related: [[verification-loop]].
