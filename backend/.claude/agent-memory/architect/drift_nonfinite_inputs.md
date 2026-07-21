---
name: drift-nonfinite-inputs
description: Recurring drift — fail-closed safety guards that check None but not NaN/Inf, letting non-finite inputs pass as "safe"
metadata:
  type: feedback
---

Fail-closed guards in the money/order path must reject **non-finite** inputs (NaN, +/-Inf),
not just `None`. A guard that only special-cases `None` and otherwise relies on `>`/`>=`
comparisons has a hole:

- `float('nan') > threshold` returns **False** silently → the check reads as "fresh/under cap"
  and the order is **ALLOWED** (fail OPEN). This directly violates Invariant #3
  (never trade through uncertainty) — NaN *is* uncertainty.
- `Decimal('NaN') > x` and `>= x` **raise** `decimal.InvalidOperation` — not a returned
  deny, so a pure "inputs in, decision out" contract is broken and an uncaught raise can
  crash the worker loop.

**Why:** Found in the first `evaluate_order_safety` review (2026-07-20). The module claimed
"fail closed everywhere / None is never treated as safe" but had no finiteness check; a NaN
`seconds_since_tick` produced `allowed=True`. This is the exact class of gap where money leaks.

**How to apply:** When reviewing any guard/gate that gates orders or caps, grep the module for
`isnan`, `is_finite`, `isfinite`, `is_nan`. If absent and the function accepts `float`/`Decimal`
money or time inputs, flag it: require an explicit finiteness reject (deny, don't raise) plus a
boundary test feeding NaN/Inf to each numeric input asserting `allowed=False` with the mapped
reason. Pairs with the sign-convention footgun — see [[drift-signed-magnitude-inputs]].
