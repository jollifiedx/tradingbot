---
name: review-observe-shared-audit-table
description: Observe-mode writes real strategy decisions (incl. BUY) into the same append-only `decisions` table the future order path will use — the two are only distinguishable by settings_snapshot IS NULL. Check at order-path review.
metadata:
  type: project
---

OBSERVE MODE (`app/worker/observe.py`, reviewed 2026-07-30) appends real
`StrategyDecision`s — including BUY, from DRAFT-risk-param `swing_trend_v1` — to the
append-only `decisions` table, placing no orders. Rows can never be deleted (Invariant 5).

The only marker separating an observe-mode decision from a future order-path decision is
`settings_snapshot IS NULL` (observe leaves it, plus `llm_rationale`/`thesis_id`, NULL). There
is no explicit `mode`/`source` column. A BUY decision recorded while the worker is
frozen/halted is NOT annotated with that posture — the row alone doesn't say "no order was
placed" or "system was frozen."

**Why:** For a single-user owner who understands observe mode this is acceptable/self-documenting
(orders/trades tables are the record of real orders; there are none). But conviction on a DRAFT-param
BUY could be misread as validated, and once the order path co-writes this table the NULL-snapshot
tell is implicit.

**How to apply:** (1) At order-path review, confirm order-path decisions populate `settings_snapshot`
so the NULL tell stays reliable, or push for an explicit source marker (audit-table schema change ⇒
Esther approval). (2) When the dashboard renders the decision log, observe-mode rows should be
visually distinguished and conviction not surfaced as a validated/actionable signal. Ties to the OWNER
TODO on DRAFT SwingConfig risk params. See [[drift-nonfinite-inputs]] for the Bar boundary that
guards the same observe path.
