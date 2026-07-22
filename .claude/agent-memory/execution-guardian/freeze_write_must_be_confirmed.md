---
name: freeze-write-must-be-confirmed
description: A safety write counts only when the returned row proves it landed; an unconfirmed halt must force refuse-to-trade until a retry succeeds
metadata:
  type: feedback
---

For any write that PERSISTS a safety decision (today: `engage_system_freeze()`), success means
the **returned row proves the state changed** (`result.frozen is True`) — not "no exception was
raised". Until a write is confirmed, an in-memory "debt" flag must force refuse-to-trade
independently of whatever the safety logic says, retry every cycle, and be cleared only by a
confirmed write — never by a later clean check.

**Why:** the architect's B-1 on the scheduler diff. The freeze write is most likely to fail at
exactly the moment it matters (the DB is down, which is also why the settings read failed), and
logging-and-continuing discards a drift observation one layer out from where the latch just
fixed it — the auto-recovery Esther ruled against on 2026-07-21.

**How to apply:** applies to every future safety persist in the order path (client order ID
written before submission, halt records, kill-switch state). The paired test shape is: make the
write fail, assert the NEXT cycle still refuses; then let it succeed and assert the flag clears
only then. Also assert the "returned row didn't change" case, not just the exception case.
See [[permanent-halt-is-correct-today]].
