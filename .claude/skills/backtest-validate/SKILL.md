---
name: backtest-validate
description: Validate strategy changes with vectorbt and maintain the forward paper-trading scorecard vs SPY. Use after any rules change or when evaluating strategy performance.
argument-hint: [what to validate, date range]
---

Validation task: $ARGUMENTS

Evaluation discipline (from research/viability-analysis.md — non-negotiable):
- Historical backtests (vectorbt) may tune DETERMINISTIC rules only.
- Anything involving LLM output is judged EXCLUSIVELY on forward paper-trading results —
  the model's training data contains the backtest answers (look-ahead bias). A historical
  backtest of LLM-influenced decisions is marketing, not evidence; refuse to present one
  as validation.
- Every report includes: total return, max drawdown, win rate, profit factor, AND costs
  (fees, spread, slippage assumptions stated explicitly), AND the SPY buy-and-hold
  comparison over the same window.
- A strategy that loses to SPY after costs is reported as FAILING regardless of win rate.
- Paper scorecard: cumulative paper P&L vs SPY since paper-start, updated from
  `equity_snapshots`; this is the promotion gate metric — never massage it.
- State sample size honestly; flag any conclusion drawn from fewer than a meaningful
  number of trades.
