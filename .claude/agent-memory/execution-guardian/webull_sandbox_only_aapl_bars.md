---
name: webull-sandbox-only-aapl-bars
description: Webull paper SANDBOX serves daily historical bars only for AAPL; MSFT/SPY consistently return "Webull server error" (500) regardless of category/count
metadata:
  type: project
---

The Webull paper **sandbox** only returns usable historical daily bars for **AAPL**. MSFT and
SPY consistently fail with `WebullAPIError: Webull server error` (a 500), across both
`US_STOCK`/`US_ETF` categories and different counts — verified 2026-07-30 during the OBSERVE MODE
sandbox smoke run. AAPL returned a full 250-bar daily series (through 2026-07-29).

**Why:** the sandbox ships canned demo data for a limited symbol set (AAPL is the classic demo
symbol), not a live-data mirror. This is an environment limitation, not a bug in the wrapper or
in observe mode — per-symbol isolation correctly recorded AAPL and skipped MSFT/SPY with logged
errors.

**How to apply:** do NOT shrink the manual `WATCHLIST` (app/worker/observe.py) to just AAPL to
"make the sandbox happy" — that would curve-fit to a sandbox quirk. The watchlist of liquid names
is correct; MSFT/SPY history should resolve once pointed at a data source that has it (live/real
paper data). When smoke-testing anything that needs multi-symbol history on the sandbox, expect
AAPL-only and treat other symbols' 500s as environment, not code. Related: [[permanent-halt-is-correct-today]].
