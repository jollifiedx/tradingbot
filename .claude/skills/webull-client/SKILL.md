---
name: webull-client
description: Build or extend the typed wrapper around the official Webull SDK — auth, account snapshot, historical bars, order status, funding. Use for any Webull REST/SDK integration work (not streaming, not order placement).
argument-hint: [endpoint or capability to wrap]
---

Webull client task: $ARGUMENTS

- SDK: `webull-openapi-python-sdk` (docs: developer.webull.com/apis/docs/). ALL Webull
  access in the codebase goes through the wrapper in `backend/app/core/` — one choke point
  for logging, retries, and paper/live routing.
- Every wrapper method: Pydantic request/response models; explicit timeout; typed
  exceptions (never let raw SDK errors leak upward); env routing via `WEBULL_ENV`
  (paper|live) — never hardcode either.
- Every method ships with mocked-SDK unit tests covering: happy path, timeout, malformed
  response, auth failure.
- Rate limits: ~600 req/min trading ops, ~15 req/sec orders — wrapper enforces client-side
  throttling so we never depend on the server rejecting us.
- Never log or echo `WEBULL_APP_KEY`/`WEBULL_APP_SECRET`.
- Order PLACEMENT/modification/cancellation logic belongs to the execution path
  (execution-guardian agent, `order-execution` skill) — if the task drifts there, stop and
  hand off.
