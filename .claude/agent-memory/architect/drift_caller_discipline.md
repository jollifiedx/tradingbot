---
name: drift-caller-discipline
description: Recurring drift pattern — a safety guarantee that only holds if the (not-yet-written) caller uses the module correctly. Esther has ruled against this shape twice; flag it proactively.
metadata:
  type: feedback
---

Recurring DRIFT to hunt in every worker/safety diff: a module computes the right safety
verdict but leaves the *guarantee* to the caller's discipline. Ask of every pure safety
module: "what is the dumbest thing the future caller can do, and does the module let it
happen silently?" If the answer is "yes, but the docstring says not to", that is DRIFT —
docstrings are not mechanisms.

**Why:** Esther has now ruled this way twice on the same day (docs/decisions.md 2026-07-21).
On reconciliation she explicitly rejected "rely on the wiring PR to check `cash_checked`"
because *the guarantee living in caller discipline is the exact pattern that produced the
finding*; the fix was structural (`reconciled` derived, `__post_init__` rejecting
inconsistent states). Same shape as the earlier safety-gate NaN fail-open. She prefers
paying for the mechanism now even when it "costs nothing today".

**How to apply:**
- Prefer derived properties over stored flags; `__post_init__` rejection over convention.
- Check whether the safe outcome depends on argument *ordering* or on the caller passing
  `None` rather than a default — those are silent fail-opens waiting for a tired wiring PR.
- A pure module cannot enforce persistence or freshness. When it hands a verdict that must
  be *persisted* or must be *recent*, say so as a BLOCKER-level wiring requirement and
  enumerate the concrete ways a careless caller defeats it (cache the flag, swallow the
  failed write, reuse a stale result, read the diagnostic field instead of the verdict).
- Watch for observations dropped by an early-return ladder: a higher-priority halt reason
  returning before a lower-priority *latching* side effect is computed loses the latch.
  (Seen in `app/worker/latch.py`: DRIFT observed while settings were unreadable, or while
  already frozen, returned `engage_freeze=False` — the drift was never persisted. Fixed in
  3576d5e by computing the latch bit before the ladder.)
- **Guard predicates that read a column the careless writer never touches.** A DB trigger
  keyed on `new.<col>` cannot tell "explicitly set" from "inherited from the old row". The
  `settings` one-way freeze guard rejects `frozen true→false AND new.updated_by IS NULL`, so
  a bare `update settings set frozen = false` silently *inherits* the previous writer's
  non-NULL attribution and passes. Whenever a guard's discriminator is an ordinary column,
  ask "what does the row look like if the statement simply omits it?"
- **A verdict that must be persisted is only as sticky as the write that persists it.** The
  branch added to fix a latch fail-open (DRIFT + settings unreadable ⇒ `engage_freeze=True`)
  is the branch whose DB write is *most likely to fail*, because settings being unreadable
  usually means the DB is down. Demand: retry-until-persisted, refuse to trade while a
  freeze is pending, and never let a restart drop the pending flag silently.

Related: [[drift-safety-untested]] — the same diffs often also lack the regression test.
