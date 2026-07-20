---
name: db-query
description: Answer questions about bot behavior by querying decisions, trades, orders, theses, and equity_snapshots — read-only, dev/paper DB only. Use when asked why the bot did or skipped something, or for ad-hoc analysis of trading history.
argument-hint: [question about bot behavior/history]
---

Answer this question from the database: $ARGUMENTS

Rules:
- READ-ONLY. SELECT statements only — never INSERT/UPDATE/DELETE/DDL, even to "fix" data.
- Dev/paper database only; never point queries at production.
- Key joins: `decisions.id` ↔ `orders.decision_id` ↔ `trades.order_id`;
  `theses.symbol` + date window for research context; `llm_calls` for cost attribution.
- Show the SQL you ran, the result, and a plain-English interpretation. Quote actual rows
  as evidence — distinguish what the data shows from what you infer.
- Money values are `numeric` strings — never round-trip them through floats in analysis.
