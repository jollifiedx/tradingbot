# Skills Inventory: Personal AI Trading Bot

**Date:** 2026-07-19
**Source PRD:** `./research/PRD.md` **does not exist.** This inventory is derived from the de facto PRD:
[viability-analysis.md](./viability-analysis.md) + [tech-stack.md](./tech-stack.md) (as recorded in `.claude/CLAUDE.md`).
If a formal PRD is written later, re-audit this list against it.

**Skills doc reviewed:** [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills)
(the docs.anthropic.com URL 301-redirects there).

---

## How this inventory applies the skills documentation

- **Location:** all skills live in `.claude/skills/<name>/SKILL.md` (project scope, shared via git).
- **Invocation control** (from the docs' frontmatter reference):
  - `disable-model-invocation: true` on anything with side effects on money, deploys, or secrets —
    Claude must never trigger these on its own. Marked **[USER-ONLY]** below.
  - `user-invocable: false` on background-knowledge skills that aren't meaningful as commands.
    Marked **[KNOWLEDGE]** below.
  - `paths:` frontmatter scopes knowledge skills so they only load when touching matching files.
- **Safety note from the docs:** skills are context, not enforcement. Every "never do X" in a skill
  below that guards real money must ALSO exist as a PreToolUse hook or `permissions.deny` rule
  before live trading. Skills guide; hooks enforce.
- **Complexity ratings:** Simple = an afternoon; Moderate = 1–3 days; Complex = a week+ and/or
  safety-critical (demands exhaustive tests).

---

## Category A — Database Operations

### A1. `db-migrate`
| | |
|---|---|
| **Description** | Create, review, and apply a Supabase SQL migration (new table, column, index, RLS policy) following project schema conventions. |
| **Input** | Plain-English schema change request; current schema (via Supabase CLI diff or MCP). |
| **Output** | Timestamped SQL file in `supabase/migrations/`, applied to local/dev DB, verified with a smoke query. |
| **Dependencies** | [Supabase CLI](https://supabase.com/docs/guides/deployment/database-migrations); Supabase MCP server; skill `A4 schema-conventions`. |
| **Complexity** | Moderate |
| **Invocation** | `/db-migrate add outcome column to theses linking back to trades` |

### A2. `db-query` **[KNOWLEDGE + user]**
| | |
|---|---|
| **Description** | Answer questions about bot behavior by querying `decisions`, `trades`, `orders`, `theses`, `equity_snapshots` — read-only, dev/paper DB only. The project's main debugging lens ("why did it skip NVDA on Tuesday?"). |
| **Input** | Natural-language question; DB access via [Supabase MCP](https://supabase.com/docs/guides/ai-tools/mcp). |
| **Output** | SQL + result + plain-English interpretation. |
| **Dependencies** | Supabase MCP (read-only role); schema knowledge from `A4`. |
| **Complexity** | Simple |
| **Invocation** | `/db-query show every trade where the LLM vetoed the rules engine, with hypothetical P&L` |

### A3. `db-backup-restore` **[USER-ONLY]**
| | |
|---|---|
| **Description** | Run/verify the nightly `pg_dump` backup and perform a test restore into a scratch DB. The decision log is the project's most valuable artifact. |
| **Input** | `DATABASE_URL` (env), backup destination. |
| **Output** | Verified backup artifact + restore-test report. |
| **Dependencies** | `pg_dump`/`psql` ([PostgreSQL docs](https://www.postgresql.org/docs/current/app-pgdump.html)); GitHub Actions cron ([docs](https://docs.github.com/en/actions)). |
| **Complexity** | Moderate |
| **Invocation** | `/db-backup-restore test restore latest backup` |

### A4. `schema-conventions` **[KNOWLEDGE]** — `paths: ["supabase/**", "backend/app/core/**"]`
| | |
|---|---|
| **Description** | Standing rules loaded when touching schema/models: `decisions`/`orders` are append-only (no UPDATE/DELETE); money is Postgres `numeric` / Python `Decimal`, never float; all timestamps `timestamptz` UTC; pgvector for `theses.embedding`; RLS on every table. |
| **Input** | (Loads automatically with matching files.) |
| **Output** | Schema work that conforms without re-explaining. |
| **Dependencies** | [pgvector guide](https://supabase.com/docs/guides/ai); [Postgres numeric types](https://www.postgresql.org/docs/current/datatype-numeric.html). |
| **Complexity** | Simple |
| **Invocation** | Model-invoked only, when editing matching files. |

---

## Category B — Authentication & Authorization

### B1. `auth-setup`
| | |
|---|---|
| **Description** | Implement/modify the auth chain: Supabase Auth email + mandatory TOTP 2FA, single-user ID allowlist enforced server-side, FastAPI middleware verifying the Supabase JWT on every route. |
| **Input** | Supabase project ref; owner user ID; route list. |
| **Output** | Working auth middleware + frontend session handling + tests proving a non-allowlisted JWT is rejected. |
| **Dependencies** | [Supabase Auth](https://supabase.com/docs/guides/auth) (MFA/TOTP: [docs](https://supabase.com/docs/guides/auth/auth-mfa)); [FastAPI security](https://fastapi.tiangolo.com/tutorial/security/); `PyJWT`. |
| **Complexity** | Moderate |
| **Invocation** | `/auth-setup add JWT verification to the settings mutation routes` |

### B2. `secrets-hygiene` **[KNOWLEDGE]**
| | |
|---|---|
| **Description** | Standing rules: Webull/Anthropic/service-role keys exist only in Railway env vars or local `.env` (gitignored); paper vs live are separate Railway environments with separate Webull keys; rotation checklist; what to do on suspected leak (freeze bot first, rotate second). |
| **Input** | (Loads when relevant.) |
| **Output** | No secret ever committed; consistent env-var naming per CLAUDE.md table. |
| **Dependencies** | [Railway variables](https://docs.railway.com/guides/variables). |
| **Complexity** | Simple |
| **Invocation** | Model-invoked when touching config/env files. |

---

## Category C — External API Integration

### C1. `webull-client`
| | |
|---|---|
| **Description** | Build/extend the typed wrapper around the official Webull SDK: auth/signatures, account snapshot, historical bars, order status, funding ops. All Webull calls in the codebase go through this wrapper (one choke point for logging, retries, paper/live routing via `WEBULL_ENV`). |
| **Input** | Which endpoint/capability to wrap; [SDK source](https://github.com/webull-inc/webull-openapi-python-sdk). |
| **Output** | Wrapper methods with Pydantic request/response models + unit tests with mocked SDK. |
| **Dependencies** | `webull-openapi-python-sdk` ([API docs](https://developer.webull.com/apis/docs/)); [Pydantic](https://docs.pydantic.dev/). |
| **Complexity** | Complex |
| **Invocation** | `/webull-client wrap the account balance and positions endpoints` |

### C2. `market-data-stream`
| | |
|---|---|
| **Description** | MQTT real-time quote subscription + gRPC order-event subscription, with the staleness heartbeat (no tick for N sec → halt flag), reconnect-with-backoff, and REST snapshot re-sync on reconnect. Implements the dead-man's switch — the scariest failure mode in the system. |
| **Input** | Symbol watchlist; staleness threshold from `settings`. |
| **Output** | Streaming module feeding the rules engine; halt behavior covered by tests (simulated silent stream, mid-tick disconnect). |
| **Dependencies** | Webull SDK MQTT/gRPC ([SDK docs](https://developer.webull.com/apis/docs/sdk/)); `asyncio`. Depends on `C1`. |
| **Complexity** | Complex |
| **Invocation** | `/market-data-stream add reconnect backoff with snapshot resync` |

### C3. `llm-research-pipeline`
| | |
|---|---|
| **Description** | The nightly slow path: gather news/filings, retrieve past theses for each candidate (pgvector similarity), run `claude-opus-4-8` deep research via Batch API (−50%), write ranked watchlist + theses + conviction + embeddings to DB. Also the thesis→outcome feedback loop when trades close. |
| **Input** | Candidate universe; prior theses + outcomes from DB; news sources. |
| **Output** | Rows in `theses` + watchlist for tomorrow; cost logged to `llm_calls`. |
| **Dependencies** | [Anthropic SDK](https://docs.claude.com/en/api/client-sdks); [Batch API](https://docs.claude.com/en/docs/build-with-claude/batch-processing); [prompt caching](https://docs.claude.com/en/docs/build-with-claude/prompt-caching); [embeddings via Voyage](https://docs.claude.com/en/docs/build-with-claude/embeddings); pgvector. Depends on `A1`. |
| **Complexity** | Complex |
| **Invocation** | `/llm-research-pipeline add SEC filing ingestion to the nightly run` |

### C4. `news-ingestion`
| | |
|---|---|
| **Description** | Fetch and normalize news/filings text for the research pipeline (dev-time prototyping via the installed Firecrawl MCP; production via a proper source). Deduplicate, timestamp, and store with strict published-at capture (look-ahead hygiene). |
| **Input** | Symbols/date range; source list. |
| **Output** | Clean, timestamped documents ready for the LLM. |
| **Dependencies** | [Firecrawl MCP](https://docs.firecrawl.dev/mcp) (dev); chosen prod news API (TBD — open decision). Feeds `C3`. |
| **Complexity** | Moderate |
| **Invocation** | `/news-ingestion prototype an earnings-news fetcher for the watchlist` |

### C5. `order-execution` **[USER-ONLY]**
| | |
|---|---|
| **Description** | Work on the order path itself: idempotent submission (client order ID persisted to `orders` before send), status-query-never-blind-retry on ambiguity, pre-order safety gate (frozen? cap? daily loss? data fresh?). USER-ONLY because even in paper mode, changes here are changes to the money path. |
| **Input** | Order intent model; `settings` row; current positions. |
| **Output** | Execution module changes + exhaustive tests (timeout mid-submit, duplicate send, reject, partial fill). |
| **Dependencies** | `C1`, `A4`, `E1`. [Place Order API](https://developer.webull.com/apis/docs/reference/trading/). |
| **Complexity** | Complex |
| **Invocation** | `/order-execution add partial-fill handling to the exit logic` |

---

## Category D — Trading Domain (rules engine & validation)

### D1. `rules-engine`
| | |
|---|---|
| **Description** | Implement/modify deterministic strategy rules: pandas-ta indicators, entry/exit conditions, position sizing, stop placement. Enforces the invariant that no LLM call exists in this path. |
| **Input** | Rule spec in plain English; indicator parameters. |
| **Output** | Pure, unit-tested rule functions (same input → same output, no I/O). |
| **Dependencies** | [pandas-ta](https://github.com/twopirllc/pandas-ta); [pandas](https://pandas.pydata.org/docs/); `A4`. |
| **Complexity** | Complex |
| **Invocation** | `/rules-engine add an ATR-based stop-distance rule` |

### D2. `backtest-validate`
| | |
|---|---|
| **Description** | Validate a rules-engine change with vectorbt over historical bars, and maintain the forward paper-trading scorecard vs SPY buy-and-hold. Bakes in the viability analysis's discipline: historical backtests inform *rules* only; anything touching LLM output is judged on forward paper results exclusively (look-ahead bias). |
| **Input** | Strategy version; date range; cost/slippage assumptions. |
| **Output** | Metrics report (return, drawdown, win rate, cost drag) + SPY comparison; go/no-go note. |
| **Dependencies** | [vectorbt](https://vectorbt.dev/); historical bars via `C1`. |
| **Complexity** | Complex |
| **Invocation** | `/backtest-validate compare rules v3 vs v2 over the last 90 days of paper data` |

### D3. `reconcile`
| | |
|---|---|
| **Description** | Build/debug the reconciliation job: compare Webull truth (positions/cash) against DB intent; on mismatch → halt + alert, never silent-fix. Runs at worker startup and periodically. |
| **Input** | Webull account snapshot; DB `orders`/`trades` state. |
| **Output** | Reconciliation module + tests for drift scenarios (manual out-of-band trade, missed fill event, crash mid-order). |
| **Dependencies** | `C1`, `A4`. |
| **Complexity** | Moderate |
| **Invocation** | `/reconcile handle the case where a fill event arrived while the worker was restarting` |

---

## Category E — Testing & Validation

### E1. `safety-tests`
| | |
|---|---|
| **Description** | The exhaustive test suite for money-guarding logic: buy-power cap arithmetic, freeze-flag obedience, daily-loss halt, staleness halt, idempotency, fail-closed on unreadable `settings`. Any PR touching safety code must run this. This is the code that loses money when wrong. |
| **Input** | Module under test; failure scenario list (maintained in the skill's supporting `scenarios.md`). |
| **Output** | pytest suite passing in CI; coverage report for `worker/` safety paths. |
| **Dependencies** | [pytest](https://docs.pytest.org/); [freezegun](https://github.com/spulec/freezegun) (time control); `pytest-asyncio`. |
| **Complexity** | Complex |
| **Invocation** | `/safety-tests cover the case where settings reads fail mid-session` |

### E2. `run-local` (via `/run-skill-generator`)
| | |
|---|---|
| **Description** | The recorded recipe to launch the full stack locally: `supabase start` (local Docker stack), worker in paper mode with mock/replay data, API, Vite dev server. Generated once with the bundled `/run-skill-generator` so `/run` and `/verify` work reliably thereafter. |
| **Input** | (One-time generation; then none.) |
| **Output** | `.claude/skills/run-tradingbot/` recipe; app running locally. |
| **Dependencies** | [Supabase local dev](https://supabase.com/docs/guides/local-development); Docker; bundled `/run`, `/verify` skills. |
| **Complexity** | Moderate (once) |
| **Invocation** | `/run-skill-generator`, thereafter `/run` or `/verify` |

### E3. `replay-harness`
| | |
|---|---|
| **Description** | Deterministic market-replay testing: feed recorded tick/bar data through the worker at accelerated speed to verify end-to-end behavior (research → watchlist → rules → paper orders) without live markets. Also the tool for reproducing "what did the bot see at 10:42 ET?" |
| **Input** | Recorded session data (captured from paper trading); scenario script. |
| **Output** | Reproducible end-to-end test runs; regression fixtures. |
| **Dependencies** | `C2` (data capture side), `D1`, `E1`. |
| **Complexity** | Complex |
| **Invocation** | `/replay-harness replay 2026-07-17 and verify the daily-loss halt fires at the right tick` |

---

## Category F — Frontend

### F1. `ui-component`
| | |
|---|---|
| **Description** | Generate dashboard components following project conventions: shadcn/ui + Tailwind, TanStack Query against the *generated* API client (never hand-written fetches), Zustand only for UI state, Zod-validated forms. |
| **Input** | Component spec; relevant API endpoints. |
| **Output** | Typed component + hook, strict-TS clean. |
| **Dependencies** | [shadcn/ui](https://ui.shadcn.com/), [TanStack Query](https://tanstack.com/query/latest), [React Hook Form](https://react-hook-form.com/) + [Zod](https://zod.dev/), [Tailwind](https://tailwindcss.com/). Depends on `F2`. |
| **Complexity** | Moderate |
| **Invocation** | `/ui-component build the freeze/unfreeze control with confirmation dialog` |

### F2. `api-client-regen`
| | |
|---|---|
| **Description** | Regenerate the typed TS client from FastAPI's OpenAPI spec after any backend route/model change; fail loudly on breaking drift. Also wired into CI so drift = build failure. |
| **Input** | Running local API (or exported spec). |
| **Output** | Updated `frontend/src/api/` generated client; `tsc` passes. |
| **Dependencies** | [openapi-typescript](https://openapi-ts.dev/); [FastAPI OpenAPI](https://fastapi.tiangolo.com/tutorial/first-steps/#openapi). |
| **Complexity** | Simple |
| **Invocation** | `/api-client-regen` |

### F3. `trading-charts`
| | |
|---|---|
| **Description** | lightweight-charts integrations: candlestick chart with entry/exit trade markers, equity curve with SPY benchmark overlay, drawdown shading. Encapsulates the library's imperative API behind React components. |
| **Input** | Data shape (bars, trades, snapshots); chart spec. |
| **Output** | Reusable chart components. |
| **Dependencies** | [lightweight-charts](https://tradingview.github.io/lightweight-charts/); [Recharts](https://recharts.org/) for non-price viz. |
| **Complexity** | Moderate |
| **Invocation** | `/trading-charts overlay trade markers on the daily candles for a symbol` |

### F4. `pwa-notifications`
| | |
|---|---|
| **Description** | PWA installability + push notifications for fills, halts, and errors (the "never be surprised" requirement). Service worker, push subscription storage, notification triggers from the worker via Supabase. |
| **Input** | Event types to notify on. |
| **Output** | Working push on phone; per-event-type toggle in settings. |
| **Dependencies** | [Web Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API); [Vite PWA plugin](https://vite-pwa-org.netlify.app/); Supabase Realtime/Edge Functions ([docs](https://supabase.com/docs/guides/functions)). |
| **Complexity** | Moderate |
| **Invocation** | `/pwa-notifications add a distinct alert style for halt events` |

---

## Category G — Deployment & Infrastructure

### G1. `deploy` **[USER-ONLY]**
| | |
|---|---|
| **Description** | Deploy api/frontend/worker with the market-hours gate: worker deploys refuse to run 9:30–16:00 ET on trading days (checked via `exchange_calendars`, not hardcoded). Verifies worker reconciles and reports healthy post-deploy. |
| **Input** | Target (api / frontend / worker / all); environment (dev / paper / live). |
| **Output** | Deployed + health-checked release; deploy log entry. |
| **Dependencies** | [Railway](https://docs.railway.com/) (+ [Railway MCP](https://github.com/railwayapp/railway-mcp-server)); [Vercel](https://vercel.com/docs); [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars); GitHub Actions. |
| **Complexity** | Moderate |
| **Invocation** | `/deploy worker to paper` |

### G2. `ci-pipeline`
| | |
|---|---|
| **Description** | Maintain GitHub Actions: ruff + mypy --strict + pytest (safety suite mandatory) + tsc + client-drift check; nightly pg_dump cron; migration application via Supabase CLI; worker deploy gated to after market close. |
| **Input** | Pipeline change request. |
| **Output** | Updated workflow files, green run. |
| **Dependencies** | [GitHub Actions](https://docs.github.com/en/actions); [ruff](https://docs.astral.sh/ruff/); [mypy](https://mypy.readthedocs.io/). |
| **Complexity** | Moderate |
| **Invocation** | `/ci-pipeline make the safety-test job required for merge` |

### G3. `webull-mcp-server`
| | |
|---|---|
| **Description** | Build the planned thin **read-only** dev MCP server wrapping the Webull SDK (positions, orders, account, bars) so Claude Code can debug against live paper-account state. Must never expose order placement/modification — enforced in code, not just docs. |
| **Input** | Which read endpoints to expose. |
| **Output** | Local MCP server + `.mcp.json` registration. |
| **Dependencies** | [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk); `C1`. |
| **Complexity** | Moderate |
| **Invocation** | `/webull-mcp-server add a get-open-orders tool` |

---

## Category H — Documentation, Logging & Operations

### H1. `update-state`
| | |
|---|---|
| **Description** | After a work session: update the "Current State" section of `.claude/CLAUDE.md` (built / in-progress / debt) and append significant decisions to a `docs/decisions.md` log. Keeps the amnesia-proof memory actually current. |
| **Input** | This session's work (from conversation context / git diff). |
| **Output** | Updated CLAUDE.md + decision log entry. |
| **Dependencies** | None. |
| **Complexity** | Simple |
| **Invocation** | `/update-state` at end of session |

### H2. `logging-conventions` **[KNOWLEDGE]** — `paths: ["backend/**"]`
| | |
|---|---|
| **Description** | Standing rules: structured JSON logs via structlog; every order-path log line carries client order ID + decision ID; halts log their *reason* as an enum; no secrets or full account numbers in logs; log levels that Railway's viewer filters well. |
| **Input** | (Loads with backend files.) |
| **Output** | Consistent, greppable logs that `H3` can triage. |
| **Dependencies** | [structlog](https://www.structlog.org/). |
| **Complexity** | Simple |
| **Invocation** | Model-invoked when writing backend code. |

### H3. `incident-triage`
| | |
|---|---|
| **Description** | When the bot halted or misbehaved: pull Railway logs + `decisions`/`orders` rows + reconciliation status, build a timeline, identify the trigger, recommend (never auto-apply) a fix. The "what happened while I was at work?" command. |
| **Input** | Time window or halt event. |
| **Output** | Timeline + root cause + recommended action, in plain English. |
| **Dependencies** | Railway MCP (logs); Supabase MCP (read-only); `H2` conventions. |
| **Complexity** | Moderate |
| **Invocation** | `/incident-triage why did the worker halt at 11:03 ET today?` |

### H4. `weekly-review`
| | |
|---|---|
| **Description** | Generate the weekly report card: trades, P&L vs SPY, rule hit-rates, thesis accuracy (predictions vs outcomes), cost accounting (fees + slippage + LLM spend), and "what the reasoning got wrong." Reads the DB; never touches execution. |
| **Input** | Week (defaults to last completed trading week). |
| **Output** | Markdown report in `docs/reviews/`; optionally emailed/notified. |
| **Dependencies** | Supabase MCP (read-only); `A2`. |
| **Complexity** | Moderate |
| **Invocation** | `/weekly-review` (or scheduled Friday post-close) |

---

## Coverage check against requested categories

| Requested category | Covered by |
|---|---|
| Database operations | A1–A4 |
| Auth & authorization | B1, B2 |
| External API integration | C1–C5 (Webull ×3, Anthropic, news) |
| Frontend components | F1–F4 |
| Testing & validation | E1–E3, D2 |
| Deployment & infrastructure | G1–G3 |
| Documentation generation | H1, H4 |
| Error handling & logging | H2, H3 |
| *(Extra: trading domain — the heart of this app)* | D1–D3 |

**Deliberately identified but likely unnecessary** (per "better to over-identify"):
- `pdf-report-export` — H4 as PDF. YAGNI for a single user reading markdown.
- `multi-strategy-manager` — running strategy variants concurrently. Post-validation concern at best.
- `tax-lot-reporter` — realized gains export for taxes. Real someday; not a build-phase skill (broker 1099 covers year one).
- `react-native-port` — explicitly deferred in tech-stack.md; PWA covers mobile.
- `paypal-integration` — permanently ruled out (viability analysis); listed here only so nobody re-adds it.

## Suggested build order

1. **Foundations:** A4, B2, H2 (knowledge skills — cheap, shape everything after) → A1, E2
2. **Broker spine:** C1 → C2 → D3 → G3
3. **Brain:** D1 → E1 (in lockstep) → C3, C4 → D2, E3
4. **Money path last:** C5 (+ full E1 coverage) — paper only
5. **Face & ops:** F2 → F1, F3, F4 → B1 → G1, G2 → A2, A3, H1, H3, H4

Safety-critical trio to treat as one unit: **C5 + E1 + D3** — no change to one without the others' tests passing.
