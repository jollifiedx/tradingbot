# TradingBot — Personal AI Trading Assistant

Single-user automated stock research + trading bot on Webull. LLM researches overnight;
a deterministic rules engine trades during market hours. Owner: Esther (agenoresther@gmail.com),
sole user. **No other users, ever — do not build multi-tenant anything.**

**Mission:** Fully autonomous weekday trading with owner controls limited to: deposit,
withdraw, set buy-power cap, freeze/unfreeze. **Success criteria:** beats SPY buy-and-hold
in forward paper trading over a meaningful sample *before* any real money; zero
safety-system failures (cap breaches, trades while frozen, trades on stale data).

**Context docs** (read on demand, do not skip when making design decisions):
- `research/viability-analysis.md` — go/no-go analysis; the "why" behind every constraint here (serves as PRD)
- `research/tech-stack.md` — full stack rationale, schema, integration map, cost model

## Tech Stack (decided — do not relitigate)

- **Backend:** Python 3.12 + FastAPI. Two deployables: `api` (dashboard REST) + `worker` (bot).
  Python is non-negotiable: Webull's official SDK is Python/Java only (`webull-openapi-python-sdk`).
- **Frontend:** React + Vite + TypeScript **PWA** (not React Native). Tailwind + shadcn/ui,
  TanStack Query (server state), Zustand (UI state only), lightweight-charts, React Hook Form + Zod.
- **API style:** REST. FastAPI OpenAPI spec → generated TS client (`openapi-typescript`). No GraphQL/tRPC.
- **DB:** Supabase Postgres + pgvector (research memory embeddings). Supabase Auth (TOTP 2FA) + Realtime.
- **Hosting:** Vercel (frontend), Railway (api + worker). CI: GitHub Actions. Migrations: Supabase CLI.
- **LLM:** Anthropic API — `claude-opus-4-8` nightly research via Batch API; `claude-haiku-4-5` for cheap tasks.
- **TA:** pandas-ta (pure Python; do NOT add TA-Lib — C build pain). Backtesting: vectorbt.
- **Scheduling:** APScheduler + `exchange_calendars` (sole authority on market hours; never hand-roll).

## Architecture Invariants (safety-critical — never weaken)

1. **LLM is analyst, not trader.** LLM output (theses, watchlist, conviction) goes to the DB.
   Only the deterministic rules engine places orders. **No LLM call in the order path.**
2. **UI never talks to Webull.** UI mutates `settings` in Supabase; worker reads `settings`
   before EVERY order. If `settings` is unreadable → halt (fail closed).
3. **Fail closed everywhere.** Stale market data (no tick for N sec), lost connection,
   unreconciled state, daily-loss breach → stop trading. Never trade through uncertainty.
4. **Idempotent orders.** Client order ID written to `orders` before submission. On
   timeout/ambiguity: query status, never blind-retry a POST.
5. **`decisions` and `orders` are append-only** audit tables. Never UPDATE/DELETE rows.
6. **Reconciliation:** Webull = truth for positions/cash; DB = truth for intent/reasoning.
   Reconcile at worker startup + periodically; on mismatch → halt and alert, never silent-fix.
7. **Paper trading until promoted.** Live credentials/environment only after owner explicitly
   promotes; paper vs live are separate Railway environments with separate Webull keys.

## Agent Coordination (meta role — the main session)

Roster and full architecture: `research/agents.md`. Definitions: `.claude/agents/` (10 subagents).
For multi-domain tasks, consult `orchestrator` first and follow its plan. Write each subagent a
self-contained brief: goal, files in scope, constraints from this file relevant to the task, and
what a done-report must contain. Never forward a subagent's raw transcript to another subagent —
extract only what it needs. After any subagent reports `ESCALATION`, stop that line of work and
ask Esther before proceeding. After merged work, run `architect` for drift review and update
Current State. Trust reports' VERIFIED sections only if they contain actual command output.
Routing: planning→orchestrator · review→architect · supabase/→db-engineer · SDK/streams→
broker-integrator · rules/backtests→strategy-quant · order path/safety→execution-guardian ·
app/research/→research-engineer · frontend/→frontend-engineer · .github//infra→devops-engineer ·
"why did it…"→ops-analyst. Overlap: the agent owning the riskier half owns the task
(execution-guardian outranks all). Execution-guardian diffs always get architect review.

## Current State (update this section as work progresses)

- **Built:** Research docs (`research/`). Claude Code scaffolding: CLAUDE.md, 10 subagents,
  26 skills. Git repo (main branch, repo-local identity). Backend skeleton: FastAPI api with
  /health, worker stub (born HALTED), fail-closed pydantic-settings config, pyproject with
  ruff/mypy-strict/pytest, smoke test. Initial schema: 11 migrations in `supabase/migrations/`
  (audit chain decisions→orders→trades, append-only triggers + SELECT-only RLS, pgvector
  theses, singleton settings born frozen with caps=0).
- **In progress:** AWAITING OWNER SIGN-OFF on initial audit-table shapes (settings defaults,
  decisions.action enum, orders enums + previous_order_id chaining) before the schema is
  applied anywhere — db-engineer escalation 2026-07-19. Architect review of schema also
  pending. Next after sign-off: link Supabase dev project + apply; Pydantic models mirroring
  schema; Webull client wrapper.
- **Known issues / debt:** Python not installed on this machine (backend can't run/test
  locally yet). Webull OpenAPI application not yet submitted (1–2 day approval — submit
  early). No PRD beyond research files. Safety "never" rules exist only as prompts — must
  become PreToolUse hooks / permissions.deny before live trading. app_owner table must be
  populated with Esther's Supabase Auth UID per environment or all RLS denies (fail-closed).

## Agent Instructions

**Approach:** Read the two research docs before architectural work. Safety code (caps,
freeze flag, halts, reconciliation) gets exhaustive unit tests — it's the code that loses
money when wrong. Prefer boring/deterministic over clever in the worker. Match existing style.

**Ask before changing:** anything in Architecture Invariants above; risk parameters
(caps, loss limits, sizing); DB schema of audit tables; auth; adding paid services.

**Never without explicit owner approval:**
- Place, modify, or cancel a LIVE (non-paper) order, or promote paper → live
- Change/disable any safety mechanism: freeze flag, buy-power cap, daily-loss halt, dead-man switch
- Write to production `settings`, `orders`, or `decisions` (incl. via Supabase MCP — dev DB only)
- Deploy the worker during US market hours (9:30–16:00 ET, per exchange_calendars)
- Commit secrets, or move keys anywhere except Railway env vars
- Add PayPal integration (already ruled out — Webull funds via ACH only)

## Planned File Structure

```
backend/
  app/api/          # FastAPI routes (dashboard REST)
  app/worker/       # bot: scheduler, rules engine, execution, reconciliation
  app/research/     # LLM pipeline: ingestion, theses, embeddings
  app/core/         # config, models (Pydantic), db, webull client wrapper
  tests/            # pytest; safety logic = exhaustive coverage
frontend/
  src/              # React PWA (components/, hooks/, api/ generated client)
supabase/migrations/  # SQL migrations via Supabase CLI (in git)
research/             # analysis docs (this phase's output)
.claude/              # this file
```

**Conventions:** Python `ruff` + `mypy --strict`, snake_case; TS strict, `tsc` in CI, PascalCase
components; SQL tables snake_case plural; money as `Decimal`/Postgres `numeric` — never float;
all internal timestamps UTC (`timestamptz`); local time rendered only in the UI.

## External Dependencies & Env Vars (names only — values live in Railway/local .env, never in git)

| Service | Purpose | Env vars | Docs |
|---|---|---|---|
| Webull OpenAPI | Broker: orders, data, funding | `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, `WEBULL_ENV` (paper\|live) | developer.webull.com/apis/docs/ |
| Anthropic API | Research LLM | `ANTHROPIC_API_KEY` | docs.claude.com/en/api/overview |
| Supabase | DB/Auth/Realtime | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL` | supabase.com/docs |
| Railway | Backend hosting | (dashboard-managed) | docs.railway.com |
| Vercel | Frontend hosting | `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_URL` | vercel.com/docs |

MCP servers available in dev: Supabase (official), Firecrawl (installed), GitHub, Railway, Vercel.
No Webull MCP exists — a read-only dev wrapper is planned; it must never expose order placement.
