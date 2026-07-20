---
name: logging-conventions
description: Structured logging rules for backend code — structlog, correlation IDs, halt reasons, no secrets. Background knowledge loaded when writing backend code.
user-invocable: false
paths:
  - "backend/**"
---

Standing rules for backend logging:

- Structured JSON logs via structlog; never bare `print` or f-string logging in worker/api.
- Correlation: every order-path log line carries `client_order_id` and `decision_id`;
  every research-path line carries `thesis_id`/`batch_id`. Timelines must be joinable to
  DB rows.
- Halts log their reason as an enum (`STALE_DATA`, `DAILY_LOSS`, `FROZEN`,
  `SETTINGS_UNREADABLE`, `RECONCILE_MISMATCH`, `CONNECTION_LOST`) plus the triggering
  values — a halt without a machine-readable reason is a bug.
- Levels: DEBUG local-only; INFO state transitions; WARNING recoverable anomalies
  (reconnects, retries); ERROR failed operations; CRITICAL safety events (also → push
  notification path).
- NEVER log: API keys/secrets, full account numbers, JWT contents. Account balance
  amounts only at INFO in reconciliation summaries.
- All log timestamps UTC ISO-8601.
