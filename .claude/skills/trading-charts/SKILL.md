---
name: trading-charts
description: lightweight-charts integrations — candlesticks with trade markers, equity curve with SPY overlay, drawdown shading. Use for any chart work in the dashboard.
argument-hint: [chart task]
---

Chart task: $ARGUMENTS

- Price/candlestick charts: TradingView `lightweight-charts` (imperative API — wrap it in
  React components that own the chart instance lifecycle; create on mount, `remove()` on
  unmount, update via refs, never re-create per render).
- Trade markers: entry/exit arrows on candles linked to `trades` rows; clicking a marker
  surfaces the decision rationale (the `decisions.llm_rationale` + rules fired).
- Equity curve: bot equity vs SPY buy-and-hold from `equity_snapshots`, same axis, with
  drawdown shading. This comparison is the project's honesty metric — never render bot
  P&L without the SPY line.
- Non-price visualizations (cost breakdown, win-rate) use Recharts.
- Data arrives as strings/Decimal-safe from the API; convert to numbers at the chart
  boundary only, display values formatted from the original strings.
- Timestamps: charts receive UTC epoch values; axis labels render local.
