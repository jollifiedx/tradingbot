---
name: order-execution
description: Work on the money path — idempotent order submission, pre-order safety gate, fill/reject/timeout handling. Owner-triggered only; even in paper mode this is the code that loses money when wrong.
disable-model-invocation: true
argument-hint: [execution-path task]
---

Execution-path task: $ARGUMENTS

Iron rules (Architecture Invariants 1-5 — implement, never weaken):
- Client order ID generated and persisted to `orders` BEFORE submission. On
  timeout/ambiguity: query order status by client ID; NEVER blind-retry a POST.
- Pre-order safety gate, re-checked before EVERY order from a fresh `settings` read:
  frozen flag off? buy-power cap respected (including open orders)? daily-loss limit not
  breached? market data fresh? reconciliation clean? Any check failing — or `settings`
  UNREADABLE — → no order. Fail closed.
- Partial fills, rejects, and cancels are recorded as new rows; `orders`/`decisions` rows
  are never updated or deleted.
- Every order references the `decisions` row that authorized it (audit chain:
  decision → order → trade).
- Paper environment only. Live orders and paper→live promotion are owner-only acts.

Every change here ships IN THE SAME DIFF as its safety tests (see `safety-tests` skill
scenarios). A test-less change to this path is incomplete — do not report it as done.
After any change: run the full safety suite and include real pytest output. Then request
architect review before merge.
