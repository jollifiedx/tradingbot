---
name: safety-tests
description: The exhaustive test suite for money-guarding logic — caps, freeze, halts, idempotency, fail-closed behavior. Use when writing or extending tests for any safety mechanism, and after any change to the order path.
argument-hint: [scenario or module to cover]
---

Safety-test task: $ARGUMENTS

This suite is the project's real product: the code that loses money when wrong must be the
best-tested code in the repo. pytest + pytest-asyncio + freezegun (time control).

Mandatory scenario families (extend, never prune):
- **Cap:** order exactly at cap, one cent over, cap counting open orders, cap lowered
  mid-session with positions above the new cap.
- **Freeze:** flag set mid-session halts next order; flag set during in-flight order
  doesn't orphan state; UNREADABLE `settings` row → treated as frozen (fail closed).
- **Daily-loss halt:** breach exactly at limit; breach via slippage on exit; no re-arm
  until next trading day (exchange_calendars, not midnight).
- **Staleness:** no tick for N sec halts entries; recovery requires snapshot re-sync;
  clock-skew between tick time and local time.
- **Idempotency:** crash after persist-before-submit, resubmit on ambiguous timeout
  (must query, not re-POST), duplicate client order ID rejected.
- **Reconciliation:** every drift scenario in the `reconcile` skill.

Rules: each test asserts the SAFE behavior (no order placed / halt raised), not just the
absence of an exception. No mocking away the check under test. Coverage on
`backend/app/worker/` safety paths is reported in CI and must not decrease.
