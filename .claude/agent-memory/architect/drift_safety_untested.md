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

- PATCH /settings control surface (execution-guardian, working tree vs 58bb328): the API handler + request model are thoroughly unit-tested with mocks (11 tests), but the *real* `Database.update_settings` SQL (coalesce of omitted fields, `updated_by` attribution, firing the `log_settings_history` trigger) is only exercised through a `_FakeDB` mock — no rolled-back live-DB integration test. The trigger itself was proven separately by the schema's 15/15 migration smoke tests, so this was signed off as OK-to-commit with a recommendation to add a transaction-rolled-back integration test before the control surface gates live money. See `backend/app/core/db.py:205-266`.

Watch especially for: construction/wiring paths that tests bypass via dependency injection; real SQL helpers behind a mock/fake DB; fail-closed branches; credential-handling; anything tied to Invariants 3 (fail closed) and 4 (idempotency). Related: [[invariant-enforcement-must-be-mechanism-not-convention]] (a future memory — enforcement should be real, e.g. DB triggers over RLS-only, tests over prose).
