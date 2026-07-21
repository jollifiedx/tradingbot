---
name: drift-partial-verification-and-latch
description: Recurring drift — a partially-verified "safe" result consumed as a bare bool, and stateless halt verdicts that silently auto-resume without a latch
metadata:
  type: feedback
---

Two related fail-open shapes to check in any safety/verification module:

**1. Partial verification collapsed into a bare bool.** A result object that carries both a
verdict (`reconciled=True`) and a "which legs actually ran" flag (`cash_checked=False`) is only
safe if the *consumer* can see both. The safety gate takes `reconciled: bool | None`, so the
natural wiring `reconciled=result.reconciled` silently discards the caveat and trades on a
partially-verified state. Flag when a module can emit "clean" while a leg the invariant names
was never compared.

**2. Stateless halt verdicts auto-resume.** A pure/stateless checker returns a fresh verdict per
run and persists nothing. A halt on run N followed by a clean run N+1 flips trading back on with
no human acknowledgement — effectively a silent recovery, which Invariant 6's "halt and alert,
never silent-fix" is meant to prevent. Transient-error auto-clear may be fine; a real drift
mismatch auto-clearing is not.

**Why:** Both found reviewing `app/worker/reconciliation.py` (2026-07-21). The module was
otherwise exemplary (pure, never raises, fail-closed on None/non-finite), which is exactly why
these are worth remembering — the holes were at the *consumption boundary*, not in the logic.

**How to apply:** For any new gate/checker, ask (a) can it return "safe" with a required check
skipped, and can the consumer tell? Prefer a combined `safe_to_trade` accessor over exposing a
raw bool. (b) Is there a documented latch rule for who clears a halt and how? If neither is
written down, raise it before the module is wired, not after. Pairs with
[[drift-nonfinite-inputs]] and [[drift-signed-magnitude-inputs]].
