---
name: drift-safety-untested
description: Recurring drift pattern — safety-relevant code (idempotency prep, credential silencing, fail-closed paths) shipped without regression tests. Check new diffs against it.
metadata:
  type: feedback
---

Recurring DRIFT to check on every review: safety-relevant behavior implemented correctly but with **no test that would catch its regression**.

**Why:** CLAUDE.md mandates "safety code (caps, freeze flag, halts, reconciliation) gets exhaustive unit tests — it's the code that loses money when wrong." Correct-but-untested safety code silently rots: a later edit removes the protection and the suite stays green.

**How to apply:** When reviewing a diff, for each safety-relevant behavior ask "which test fails if someone deletes this line?" If none, flag DRIFT. Concrete instances seen:
- Webull wrapper (commit 86565d0): `_build_api_client` sets `auto_retry=False` (Invariant 4 idempotency prep) and silences the SDK's credential-echoing logger — but the test suite pre-seeds `_trade_client`/`_data_client`, so that whole construction path is never exercised. See `backend/app/core/webull/client.py:182-197`.

Watch especially for: construction/wiring paths that tests bypass via dependency injection; fail-closed branches; credential-handling; anything tied to Invariants 3 (fail closed) and 4 (idempotency). Related: [[invariant-enforcement-must-be-mechanism-not-convention]] (a future memory — enforcement should be real, e.g. DB triggers over RLS-only, tests over prose).
