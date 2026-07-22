---
name: permanent-halt-is-correct-today
description: The worker cannot reach may_trade=True end-to-end until a DB cash ledger exists — this is the designed posture, not a bug to fix
metadata:
  type: project
---

The bot is **permanently halted by design** right now, and every layer agrees:
`reconcile()` always passes `expected_cash=None` (no DB cash ledger exists until the order
path records trades) → status `CASH_NOT_VERIFIED` → `reconciled=False` → the latch returns
`may_trade=False`. `may_trade=True` is therefore unreachable end to end.

**Why:** owner ruling 2026-07-21 — "a partial verification is not a verification". Positions
can be compared; cash cannot, and comparing the broker's cash to our copy of the broker's cash
proves nothing. It resolves structurally when the cash ledger lands, not by anyone's decision.

**How to apply:** never "fix" a halted-looking worker or a red-looking dashboard by relaxing
this — loosening `cash_checked`, hand-building a `clean()` result in the production path, or
gating on `positions_reconciled` are all the same fail-open and all ESCALATIONS. Tests that
assert `may_trade is True` must be built on hand-made CLEAN results and labelled as testing the
*mechanism for when the ledger lands*, not today's behaviour. See
[[freeze-write-must-be-confirmed]].
