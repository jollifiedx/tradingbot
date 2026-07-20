---
name: rules-engine
description: Implement or modify deterministic strategy rules — pandas-ta indicators, entry/exit conditions, position sizing, stop placement. Use for any trading-rule logic work.
argument-hint: [rule change]
---

Rules-engine task: $ARGUMENTS

- Rules code is PURE: same inputs → same outputs. No I/O, no network, no LLM calls, no
  wall-clock reads, no randomness without a seeded generator. Inputs (bars, indicators,
  conviction scores, caps) arrive as arguments; the engine never fetches.
- Indicators come from pandas-ta computed in code — never ask a model to read a chart.
  Do NOT add TA-Lib (C build pain; decided in tech-stack.md).
- Money math in Decimal. Sizing respects the buy-power cap as an INPUT; changing cap or
  loss-limit VALUES is owner-approval — escalate, don't edit.
- Every rule ships with unit tests: happy path plus empty bars, gaps, halted symbols,
  first-bar-of-day, and boundary values on every threshold.
- Anti-curve-fit: flag any rule accumulating more than a handful of tuned parameters;
  simpler rules survive forward testing better.
- Validate meaningful changes with the `backtest-validate` skill before wiring anywhere.
