---
name: drift-stale-verdict
description: Recurring drift — freshness is enforced on a safety module's INPUTS but not on the cached verdict it publishes, so a permission outlives the evidence behind it
metadata:
  type: feedback
---

Whenever a module caches a safety *verdict* for a later consumer, check that the verdict
itself has an age bound — not just its inputs. The shape: a state object carefully ages out
its raw evidence (`fresh_result()` returns None past the reconcile interval) but stores the
resulting decision with no timestamp, so `may_trade` keeps returning True for as long as
nothing re-decides.

**Why:** Found in `app/worker/scheduler.py` (2026-07-21, WorkerState). Verified by probe: after
one CLEAN tick, advancing the clock six hours with no tick left `may_trade is True` while
`fresh_result()` was already `None`; and with a slow/hung reconcile holding the state lock,
`may_trade` stayed True straight through an owner freeze because the tick was blocked. The
module's own docstring claimed "every field that could go stale is served through an age
check" — the decision was the field it forgot. This is the same defeat as "reusing a stale
reconciliation", one layer out, and it is easy to miss because the halting logic is correct.

**How to apply:**
- Ask of any cached posture/permission: *who refreshes it, and what happens if that refresher
  stops running?* If the answer is "it keeps saying yes", that is a fail-open.
- Concrete starvation mechanisms to check: a lock held across network/DB I/O (with no timeout),
  APScheduler `max_instances=1` skipping runs while one instance blocks, misfire-grace drops.
- The fix is cheap and structural: store `_decided_at`, bound the verdict by an age (≈2× the
  refresh interval), and test the blocked-refresher case explicitly.
- Related: a staleness check written as `now - at > max_age` also treats a NEGATIVE age as
  fresh — an NTP step-back serves an expired result. Bound both ends.
- Pairs with [[drift-caller-discipline]] (freshness cannot be left to the caller) and
  [[drift-partial-verification-and-latch]].
