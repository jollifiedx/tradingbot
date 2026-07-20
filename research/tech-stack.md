# Tech Stack Analysis: Personal AI Trading Bot

**Date:** 2026-07-19
**Builds on:** [viability-analysis.md](./viability-analysis.md) — architecture assumes its conclusions: LLM as *analyst* (slow path), deterministic rules engine as *executor* (fast path), paper trading first, Webull OpenAPI as the broker, ACH funding (no PayPal).

---

## ⚠️ One flag before the stack

Your prompt asks for costs at "1k users" and "10k users," but the viability analysis is for a tool **just for you**. Those are different products. The moment other people's money flows through your bot, you're operating an investment service — RIA/broker-dealer registration territory, and the tech bill becomes a rounding error next to compliance. The stack below is optimized for the single-user product and *happens to scale* to a multi-user SaaS if you ever pivot deliberately. Cost estimates for 1k/10k are included as requested, with that caveat attached.

---

## The Decision That Drives Everything: Python Backend

**Webull publishes official SDKs for exactly two languages: Python and Java.** There is no official Node.js SDK — you'd be hand-rolling REST signatures, MQTT streaming, and gRPC order events yourself.

- Official SDK: [`webull-openapi-python-sdk`](https://github.com/webull-inc/webull-openapi-python-sdk) (Python 3.8–3.13, `pip install webull-openapi-python-sdk`) — handles auth/signatures, order placement, historical bars over HTTP, real-time quotes over MQTT, order events over gRPC. Docs: [Webull SDKs & Tools](https://developer.webull.com/apis/docs/sdk/)
- Python also owns the quant ecosystem you need: `pandas`, `pandas-ta`, `vectorbt`, `exchange_calendars`. Node has nothing comparable.

**Backend language: Python. Not a close call.** Node.js loses on the single most important integration in the project.

---

## 1. Frontend Recommendation

### Framework: **React (Vite + TypeScript) as a PWA — not React Native at MVP**

- **[React](https://react.dev/)** + **[Vite](https://vite.dev/)** + TypeScript. Your UI is a dashboard: equity curve, positions, trade/decision log, freeze button, buy-power slider. That's a web app.
- Ship it as a **PWA** (installable on your phone, push-capable). You get "cross-platform" without maintaining a second codebase. **React Native/[Expo](https://docs.expo.dev/) is a deliberate later step** if you ever want native push + biometric-gated freeze/unfreeze — and because you chose React now, the mental model and much of the state layer transfers.
- Rationale for deferring RN: this app has exactly one user (you), and the highest-value mobile feature (alerts) is covered by PWA push or a Telegram bot in the interim.

### Key libraries (mapped to your features)

| Feature | Library | Why |
|---|---|---|
| Candlestick charts w/ trade markers | [lightweight-charts](https://tradingview.github.io/lightweight-charts/) (TradingView) | The canonical OSS candlestick renderer; 45KB; built for financial data |
| Equity curve / P&L viz | [Recharts](https://recharts.org/) | Simple declarative charts for the non-candlestick views |
| Server state | [TanStack Query](https://tanstack.com/query/latest) | Polling/refetching of positions, orders, bot status — this *is* the app |
| Live updates | Supabase [Realtime](https://supabase.com/docs/guides/realtime) subscriptions | Trade fills and bot-status changes push to the UI with zero custom WebSocket code |
| UI kit | [Tailwind CSS](https://tailwindcss.com/) + [shadcn/ui](https://ui.shadcn.com/) | Fast, good-looking, Claude Code generates it fluently |
| Forms (settings, caps) | [React Hook Form](https://react-hook-form.com/) + [Zod](https://zod.dev/) | Zod schemas shared for API payload validation |

### State management: **TanStack Query + Zustand — no Redux**

- ~90% of your state is *server* state (positions, orders, logs) → TanStack Query.
- The sliver of client state (UI toggles, chart settings) → [Zustand](https://zustand.docs.pmnd.rs/) (tiny, no boilerplate).
- Critical control state (frozen/unfrozen, buy-power cap) lives **in the database, never in the client** — the UI just reads and mutates it. The bot must obey the DB flag even if the UI is closed.

---

## 2. Backend Recommendation

### Runtime & framework: **Python 3.12 + [FastAPI](https://fastapi.tiangolo.com/)**

Two deployables from one codebase:

1. **API service (FastAPI)** — serves the dashboard: read positions/history, toggle freeze, set caps, trigger deposits/withdrawals.
2. **Bot worker** (long-running process) — the actual trader:
   - **Scheduler:** [APScheduler](https://apscheduler.readthedocs.io/) + [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) (knows NYSE holidays/half-days — do not hand-roll this)
   - **Slow path (overnight/pre-market):** LLM research → watchlist + theses → written to DB
   - **Fast path (market hours):** deterministic rules engine on streaming data — entries, exits, stops, buy-power cap, daily-loss halt. **No LLM calls in this path.**

### Key backend libraries

| Concern | Library |
|---|---|
| Broker | [webull-openapi-python-sdk](https://github.com/webull-inc/webull-openapi-python-sdk) |
| LLM | [Anthropic Python SDK](https://docs.claude.com/en/api/client-sdks) — `claude-opus-4-8` for overnight deep research, `claude-haiku-4-5` for cheap intraday summarization/classification. Use [prompt caching](https://docs.claude.com/en/docs/build-with-claude/prompt-caching) and the [Batch API](https://docs.claude.com/en/docs/build-with-claude/batch-processing) (50% off) for the nightly research run |
| Technical analysis | [pandas-ta](https://github.com/twopirllc/pandas-ta) (pure Python — avoid TA-Lib's C-compilation deployment pain) |
| Backtesting/paper validation | [vectorbt](https://vectorbt.dev/) |
| Data validation | [Pydantic](https://docs.pydantic.dev/) (native to FastAPI; every order intent is a validated model) |
| DB access | [SQLAlchemy 2.0](https://docs.sqlalchemy.org/) + [asyncpg](https://magicstack.github.io/asyncpg/) |

### API architecture: **REST (FastAPI) — not GraphQL, not tRPC**

- **tRPC is TypeScript-only** — dead on arrival with a Python backend.
- **GraphQL** solves problems you don't have (many clients, flexible queries). One client, ~15 endpoints.
- FastAPI auto-generates an OpenAPI spec → generate a typed TS client with [openapi-typescript](https://openapi-ts.dev/) → you get tRPC-style end-to-end type safety across the language boundary anyway.

### Authentication: **[Supabase Auth](https://supabase.com/docs/guides/auth)**

- Email + **mandatory TOTP 2FA** (this UI can move your money — treat it like a bank login). FastAPI verifies the Supabase JWT on every request.
- Single-user hardening: an allowlist of exactly your user ID, checked server-side.
- **Webull App Key/Secret and Anthropic key never touch the frontend or DB** — they live in the worker's host secret store (Railway env vars) only.

---

## 3. Database Recommendation

### Primary: **[Supabase](https://supabase.com/docs) (Postgres 15+)**

Chosen over Firebase and Mongo Atlas because:
1. **Trading data is relational and financial** — you want real transactions, foreign keys, and SQL aggregations over trades. Firestore's document model fights you here.
2. **[pgvector](https://supabase.com/docs/guides/ai) built in** — your "research memory" becomes embedded documents in the same DB. Nightly theses get embedded; before researching a symbol, the bot retrieves its own past theses + outcomes. No separate vector DB.
3. **Realtime + Auth + Storage** bundled — three fewer services.
4. **Official MCP server** (see §5) — the strongest MCP story of the three by a wide margin.

### Schema approach (core tables)

```
settings          -- singleton: frozen flag, buy_power_cap, max_daily_loss, per-trade caps
decisions         -- every decision incl. NO-trades: timestamp, symbol, rules_fired,
                  --   llm_rationale, action, APPEND-ONLY (audit log)
orders            -- client_order_id (idempotency key), broker_order_id, status, fills
trades            -- round-trips: entry/exit, P&L, fees, slippage vs. intended price
theses            -- LLM research: symbol, thesis text, conviction, embedding vector,
                  --   outcome + P&L back-linked when the trade closes  ← the feedback loop
equity_snapshots  -- daily account value; drives equity curve + SPY benchmark comparison
llm_calls         -- model, tokens, cost per call (know your true net P&L)
```

- `decisions` and `orders` are **append-only** (enforce via RLS/trigger): your audit trail must be immutable.
- **Reconciliation rule:** Webull is the source of truth for positions/cash; your DB is the source of truth for *intent and reasoning*. Worker reconciles on every startup.

### Secondary stores: **none at MVP — deliberately**

- Cache: in-process (worker holds hot symbol state in memory). Add Redis only if you go multi-user.
- Search: Postgres full-text + pgvector covers "search my research."
- Queue: APScheduler in-process. Add one queue-shaped thing later if ever needed.

### Backup & migration strategy

- **Migrations:** [Supabase CLI](https://supabase.com/docs/guides/deployment/database-migrations) SQL migrations in Git, applied via CI. Local dev against `supabase start` (Dockerized local stack).
- **Backups:** Supabase Pro ($25/mo) includes daily backups. On the free tier, run a nightly `pg_dump` from a [GitHub Actions](https://docs.github.com/en/actions) cron to private storage — **your decision log is the most valuable artifact this project produces; do not lose it.**
- Test restores once before go-live, not during an incident.

---

## 4. Infrastructure & Hosting

### Deployment

| Piece | Platform | Cost |
|---|---|---|
| Frontend | [Vercel](https://vercel.com/docs) (Hobby) | $0 |
| API + bot worker | [Railway](https://docs.railway.com/) (Hobby, 2 services) | ~$5–15/mo |
| DB/Auth/Realtime | Supabase | $0 → $25 (Pro) |
| CI/CD | GitHub Actions | $0 |

**Why Railway over Fly.io/Render:** Fly.io is cheapest (~$3–6/mo for a 512MB always-on machine) and Render starts at $7/service, but Railway's DX (git-push deploys, first-class background workers, sane logs) plus an available MCP server makes it the right default here. Swap to Fly later if you're pinching pennies — the app is a Dockerfile either way. **Set a Railway usage alert — it has no hard spend cap by default.**

- **Worker uptime note:** the worker must be always-on during market hours with **restart-on-crash** — and per the viability analysis, on unexpected restart it reconciles state and stays **halted until state is verified**. Never deploy the worker mid-market-hours; CI should gate deploys to after close.

### CI/CD (GitHub Actions)

1. PR → lint (`ruff`), typecheck (`mypy`, `tsc`), unit tests — **rules engine and cap/halt logic get exhaustive tests; this is the code that loses money when wrong**
2. Merge to `main` → deploy API + frontend; worker deploys only outside market hours
3. Nightly cron → `pg_dump` backup (free tier)
4. Migrations applied via Supabase CLI in the deploy job

### Estimated monthly costs

| Phase | Infra | LLM (Anthropic) | Market data | Total |
|---|---|---|---|---|
| **MVP (you, paper trading)** | ~$5–15 (Railway) + $0 (Vercel, Supabase free) | ~$10–25 (nightly Opus batch + Haiku intraday, with caching) | $0–3 (Webull OpenAPI real-time subscription, personal tier) | **~$15–43 ✅ under $50** |
| **1k users** † | ~$150–350 (bigger Railway/Fly instances, Supabase Pro+compute, Redis) | ~$500–2,000 (research shared across users helps; per-user personalization hurts) | ~$100–500 (redistribution licensing kicks in) | **~$750–2,850** |
| **10k users** † | ~$800–2,000 | ~$3,000–15,000 | Exchange-license territory, $1k+ | **~$5k–18k+** |

† At 1k+ users the dominant cost is **not on this table**: legal/compliance for operating a trading service (RIA registration or broker partnerships, E&O insurance). Infra is the easy part.

Biggest controllable MVP cost is LLM spend: nightly research via **Batch API (−50%)**, system-prompt **caching**, Haiku for everything that isn't deep reasoning, and the `llm_calls` table so spend is visible next to P&L.

---

## 5. MCP Server Availability

This stack was partly *chosen* for its MCP coverage — here's the map:

| Component | MCP server | Status | What it enables in Claude Code |
|---|---|---|---|
| Supabase | [Official MCP server](https://supabase.com/docs/guides/ai-tools/mcp) (official Claude connector since Jan 2026; 32 tools) | ✅ Official | Claude queries your trade history, inspects schema, writes migrations, debugs "why did the bot skip this trade" by reading the `decisions` table directly |
| Firecrawl | Already installed in your environment | ✅ Installed | News/filings scraping during development; prototyping the research pipeline before writing production code |
| GitHub | [Official MCP server](https://github.com/github/github-mcp-server) | ✅ Official | PRs, issues, CI runs from chat |
| Railway | [Community MCP](https://github.com/railwayapp/railway-mcp-server) | ✅ Available | Deploys, logs, env vars from chat |
| Vercel | [Official MCP](https://vercel.com/docs/mcp/vercel-mcp) | ✅ Official | Deployment status/logs |
| Anthropic API | `claude-api` skill already in this environment | ✅ | Model/pricing/API reference while building the research pipeline |
| **Webull** | — | ❌ None exists | The gap (see below) |

**What this enables:** your dev loop becomes conversational — "show me every trade where the rules fired but the LLM vetoed, with P&L if we'd taken it" is a Supabase MCP query, not a script you write. Schema changes, deploy checks, and log spelunking all happen in-session.

**The Webull gap — and an opportunity:** no Webull MCP server exists. Build a **thin read-only MCP server** wrapping the Python SDK (get positions, get orders, get account) for development use. ~A day of work, and Claude Code can then debug against your *live paper account* state. Keep it strictly read-only — order placement stays in the audited worker path, never exposed to a dev tool.

**Caution that matters for this project:** Supabase's own docs say the MCP server is **for development/testing, not production** — and more importantly, once real money flows, **do not point write-capable MCP tools at the production DB**. A well-meaning "clean up test rows" against `settings` or `orders` is how a dev tool unfreezes a live bot. Dev project: full MCP. Prod project: read-only role, if connected at all.

---

## 6. Integration Map

```
                        ┌────────────────────────────────────────────┐
                        │            VERCEL (React PWA)              │
                        │  dashboard · freeze btn · caps · history   │
                        └───────┬───────────────────────▲────────────┘
                        REST + JWT                 Realtime (fills, status)
                                │                       │
     ┌──────────────────────────▼───────┐   ┌───────────┴───────────────┐
     │      RAILWAY: FastAPI API        │   │     SUPABASE (Postgres)   │
     │  auth check · settings CRUD ─────┼──▶│ settings · decisions      │
     │  history queries · funding ops   │   │ orders · trades · theses  │
     └──────────────────────────────────┘   │ (pgvector) · equity ·     │
                                            │ llm_calls  + Auth + RT    │
     ┌──────────────────────────────────┐   └───────────▲───────────────┘
     │      RAILWAY: Bot Worker         │               │
     │                                  ├───────────────┘
     │  SLOW PATH (nightly):            │    writes decisions/theses; reads settings
     │   Anthropic Batch API (Opus) ────┼──▶ Anthropic API
     │   news/filings ingestion         │
     │  FAST PATH (market hrs):         │
     │   rules engine · caps · stops    │
     │   daily-loss halt · dead-man     │
     └──────┬───────────────▲───────────┘
        orders (HTTP)   quotes (MQTT) + order events (gRPC)
            │               │
     ┌──────▼───────────────┴───────────┐
     │        WEBULL OPENAPI            │
     │  paper env → live · ACH funding  │◀── your bank (ACH) — NOT PayPal
     └──────────────────────────────────┘
```

**Control-flow invariant:** UI never talks to Webull. UI mutates `settings` in Supabase; the worker reads `settings` before *every* order. Freeze works even if Vercel, the API, and your phone are all down — and if the worker can't *read* `settings`, it must halt (fail-closed).

### Integration pain points (ranked, with mitigations)

1. **Webull MQTT/gRPC stream reliability** — streams drop; a worker trading on stale quotes is the scariest failure in this system. *Mitigation:* heartbeat timestamps on every tick; no-data-for-N-seconds → halt new entries; auto-reconnect with backoff; REST snapshot re-sync on reconnect. This is the dead-man's switch from the viability analysis — build it first.
2. **Order idempotency across retries** — timeout ≠ failure; naive retry = double position. *Mitigation:* client order IDs stored in `orders` before submission; on any ambiguity, query order status before re-sending. Never blind-retry a POST.
3. **Two sources of truth drift** (Supabase intent vs. Webull reality — partial fills, rejections, out-of-band manual trades). *Mitigation:* reconciliation job at startup + every N minutes; on mismatch, halt and alert — never "fix" silently.
4. **Supabase free-tier project pausing** (~1 week inactivity). Unlikely to bite an active bot, but a paused DB during pre-market = fail-closed halt (correct behavior, annoying). *Mitigation:* upgrade to Pro ($25) before live trading; it also buys you real backups.
5. **Python↔TS type drift** — *Mitigation:* generated OpenAPI TS client in CI; a drifted type is a build failure, not a runtime surprise.
6. **Timezones** — you're not in US Eastern; DST + half-days will burn you. *Mitigation:* everything internal in UTC; `exchange_calendars` is the only authority on market hours; render local time only in the UI.
7. **Secrets sprawl** — Webull keys, Anthropic key, Supabase service role. *Mitigation:* Railway env vars only; separate paper vs. live Webull credentials as separate Railway environments; rotation checklist in the README.
8. **Webull OpenAPI approval** — 1–2 day review before any live integration. *Mitigation:* apply on day one; build against the test env while waiting.

---

## Stack Summary (one screen)

| Layer | Choice | Runner-up (and why it lost) |
|---|---|---|
| Frontend | React + Vite + TS, PWA | React Native (defer — web PWA covers mobile for 1 user) |
| UI | Tailwind + shadcn/ui, lightweight-charts, TanStack Query + Zustand | Redux (boilerplate for state you don't have) |
| Backend | Python 3.12 + FastAPI (API + worker) | Node.js (no official Webull SDK — disqualifying) |
| API style | REST + generated TS client | tRPC (TS-only), GraphQL (overkill) |
| DB | Supabase Postgres + pgvector | Firebase (weak relational/SQL fit), Atlas (weaker MCP + no bundled auth/realtime) |
| Auth | Supabase Auth + TOTP 2FA | — |
| Hosting | Vercel (FE) + Railway (BE) | Fly.io (cheaper, weaker DX — fine later) |
| CI/CD | GitHub Actions + Supabase CLI migrations | — |
| LLM | Anthropic: Opus 4.8 (nightly batch research) + Haiku 4.5 (cheap tasks) | — |
| MVP cost | **~$15–43/mo** ✅ | — |

**Documentation index:** [Webull OpenAPI](https://developer.webull.com/apis/docs/) · [Webull Python SDK](https://github.com/webull-inc/webull-openapi-python-sdk) · [FastAPI](https://fastapi.tiangolo.com/) · [React](https://react.dev/) · [Vite](https://vite.dev/) · [TanStack Query](https://tanstack.com/query/latest) · [Zustand](https://zustand.docs.pmnd.rs/) · [lightweight-charts](https://tradingview.github.io/lightweight-charts/) · [Tailwind](https://tailwindcss.com/) · [shadcn/ui](https://ui.shadcn.com/) · [Supabase](https://supabase.com/docs) · [Supabase MCP](https://supabase.com/docs/guides/ai-tools/mcp) · [pgvector guide](https://supabase.com/docs/guides/ai) · [Supabase migrations](https://supabase.com/docs/guides/deployment/database-migrations) · [Railway](https://docs.railway.com/) · [Vercel](https://vercel.com/docs) · [GitHub Actions](https://docs.github.com/en/actions) · [Anthropic API](https://docs.claude.com/en/api/overview) · [Prompt caching](https://docs.claude.com/en/docs/build-with-claude/prompt-caching) · [Batch API](https://docs.claude.com/en/docs/build-with-claude/batch-processing) · [APScheduler](https://apscheduler.readthedocs.io/) · [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) · [pandas-ta](https://github.com/twopirllc/pandas-ta) · [vectorbt](https://vectorbt.dev/) · [SQLAlchemy](https://docs.sqlalchemy.org/) · [Pydantic](https://docs.pydantic.dev/)
