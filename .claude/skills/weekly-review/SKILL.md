---
name: weekly-review
description: Generate the weekly report card — trades, P&L vs SPY, rule hit-rates, thesis accuracy, full cost accounting. Use at week's end or when asked how the bot performed.
argument-hint: [week, defaults to last completed trading week]
---

Weekly review for: $ARGUMENTS (default: last completed trading week per exchange_calendars).

Read-only. Query the DB (dev/paper), write the report to `docs/reviews/YYYY-WW.md`:

1. **Headline:** week P&L (net of ALL costs) vs SPY buy-and-hold same window; cumulative
   paper scorecard vs SPY since start. This number decides promotion — never massage it.
2. **Trades:** count, win rate, average win/loss, largest loss, holding times.
3. **Costs:** fees + estimated spread/slippage + LLM spend (`llm_calls`) — the true net.
4. **Thesis accuracy:** for theses whose trades closed this week — what the LLM predicted
   vs what happened; running accuracy rate.
5. **Rules:** which rules fired, hit-rate per rule, any rule that never fires (dead) or
   always fires (meaningless).
6. **Safety:** halts this week and their reasons; explicitly state "zero safety-system
   failures" or list them (a failure here leads the report, not the P&L).
7. **What the reasoning got wrong:** honest misses, for next week's research prompts.

Tone: honest report card, not a pitch. Losing weeks are stated plainly.
