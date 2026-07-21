---
name: drift-signed-magnitude-inputs
description: Footgun — safety guards that take a "loss magnitude" (positive = bad) as a bare Decimal, invertible if caller passes signed P&L
metadata:
  type: feedback
---

When a safety guard accepts a "loss so far" / "drawdown" as a bare `Decimal` with the
convention *positive magnitude = down money*, the type does not stop a caller from passing
**signed P&L** (negative when losing). If they do, a real $600 loss arrives as `-600`, and
`-600 >= max_daily_loss` is False → the circuit breaker **does not fire** → order allowed.
Highest-consequence inversion in the whole gate: it silently disables the daily-loss halt.

**Why:** Raised in the `evaluate_order_safety` review (2026-07-20). The pure gate is correct
per its documented contract, but the risk lands entirely on the (not-yet-written) caller in
the order path. Documentation alone doesn't make it hard to get wrong.

**How to apply:** When the caller (execution-guardian order path) is written, require a test
that pins the sign convention end-to-end (a genuine loss halts). Prefer designs that make
inversion impossible: compute the magnitude inside the guard from realized+unrealized, or use
a distinct newtype, rather than trusting a bare `Decimal`. Pairs with [[drift-nonfinite-inputs]].
