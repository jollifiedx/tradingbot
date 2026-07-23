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
  Schema applied to Supabase dev 2026-07-20 (15/15 runtime smoke tests). Pydantic model
  layer (models.py, 1:1 mirror + drift-guard tests). Webull wrapper (read-only, account_v2)
  verified LIVE on sandbox (get_account_list→200, 5 accounts; balance parsed). DB-access
  layer (db.py: asyncpg pool + lifespan + get_settings/get_decisions/get_latest_equity_
  snapshot/update_settings, fail-closed). app_owner populated with Esther's Auth UID.
  Dashboard API COMPLETE (7169ded): single-owner auth gate (deps.py — Supabase JWT
  introspection + app_owner check, 401/403/503 fail-closed); GET /settings,/decisions,
  /account + PATCH /settings (freeze/cap write, architect-approved). Money string-typed
  through OpenAPI. 93 tests green, mypy strict clean.
  Frontend PWA first screen COMPLETE (68c1ee0): Vite+React+TS+Tailwind, TanStack Query +
  Zustand + RHF/Zod, Supabase auth (email+password), typed client from OpenAPI (openapi.json
  committed). Screen: freeze banner, freeze toggle + caps editor (both confirm-gated),
  decision log, account panel. Money strings end-to-end. Verified: npm run build clean, and
  boots live (backend uvicorn + vite) — login screen renders, no console errors, /settings
  401 unauth. NOT yet done: authenticated login→freeze flow (needs owner's password — owner
  tests). Dev run: backend `uvicorn app.api.main:app --port 8000`; frontend `npm run dev`.
  Worker equity-snapshot job (snapshot.py) verified live (dashboard Account panel shows real
  paper numbers). buying_power nested-parse RESOLVED. Pre-order safety gate COMPLETE (f7eb3cd,
  architect-approved after a fail-open fix): pure evaluate_order_safety() — fail-closed on
  None AND non-finite inputs; priority ladder SETTINGS_UNREADABLE→FROZEN→UNRECONCILED→
  STALE_DATA→DAILY_LOSS→PER_TRADE_CAP→BUY_POWER_CAP. Imported nowhere yet (built ahead of the
  order path). 157 tests green.
  Reconciliation COMPLETE (Invariant 6, architect-approved after 2 review rounds): pure
  compare_positions() + non-mutating async reconcile() on the PINNED account
  (WEBULL_ACCOUNT_ID, set to a CASH sandbox account). Owner rulings (docs/decisions.md
  2026-07-21): cash-unchecked ⇒ status CASH_NOT_VERIFIED + reconciled=False (structural —
  `reconciled` is derived, __post_init__ blocks inconsistent states); HaltCategory
  DRIFT=sticky/owner-clears, TRANSIENT=may auto-clear, NOT_VERIFIED=resolves structurally.
  db.get_open_position_intents() (long-only, guarded by a no-`side`-column tripwire test).
  260 tests green.
- **STRATEGY DECISION — RULED 2026-07-21 (docs/decisions.md):** HYBRID, built SWING-FIRST, as
  separate STRATEGY MODULES inside ONE worker. Build the swing layer first (daily/hourly bars,
  hold days–weeks), prove it vs SPY in forward paper, THEN add intraday entry-timing as a
  self-contained increment. NOT separate bot processes — one account + one set of account-level
  caps + one reconciliation + one freeze can't be shared by two racing processes. The rules
  engine is designed for pluggable strategy modules but only the swing module is built first;
  the safety gate / order path / worker stay strategy-agnostic and shared.
  Halt latch + worker SCHEDULER COMPLETE (4dda1c5, architect-approved after 3 review rounds):
  pure decide_posture(); lifecycle born HALTED→config→settings→reconcile→tick; APScheduler +
  exchange_calendars (sole market-hours authority); posture/reconcile/snapshot jobs. All 11
  defeat-list items are mechanism with a regression test each, incl. the system-level
  DRIFT-then-CLEAN test (long-standing CLAUDE.md requirement, now DISCHARGED). Worker
  self-freezes via engage_system_freeze; freeze_write_pending debt retried and never cleared by
  a clean run; verdicts EXPIRE (2× posture interval) so a starved tick can't keep publishing
  yes; latch defects stick process-lifetime as LATCH_ERROR. 386 tests green.
  Rules engine COMPLETE + architect-reviewed (6bdf3ab): pluggable Strategy ABC (base.py) +
  swing_trend_v1 (swing.py — daily-bar trend follower, long-only, draft 8% stop + trend-break
  exit, signals only, never sizes) + vectorbt backtest harness (backtest.py — no look-ahead
  walk, costs always applied, SPY benchmark, "backtest≠validation" disclaimer). Pure, wired to
  NOTHING. Review found D1/D2 (a non-finite/non-positive price bar slipped Bar construction and
  made the Decimal stop RAISE on a held position, skipping the stop — same NaN-boundary shape as
  before) — FIXED: Bar rejects malformed OHLC/volume at construction; stop compares native
  Decimal close. 453 tests green. Re-review of the small D1/D2 fix is PENDING (non-blocking —
  nothing consumes evaluate() yet; the order path that will is owner-gated). DRAFT risk params
  in SwingConfig still need OWNER REVIEW before real sizing.
- **In progress:** Nothing active. Next: (1) market-data stream + staleness heartbeat (feeds the
  gate's seconds_since_tick); (2) order path that WIRES the safety gate + strategy — its five hard
  requirements are enumerated in scheduler.py's start() docstring (fresh get_settings per order,
  may_trade at submission, gate wired incl. the loss_so_far sign test, + 2 end-to-end refusal
  tests); (3) TOTP 2FA enrollment; (4) deploy (owner-gated). GET /positions still DEFERRED.
  OWNER TODO: review the DRAFT SwingConfig risk params (stop_loss_pct=8%, MA lengths, RSI
  ceiling) + the backtest CostModel assumptions before any real sizing.
  NOTE: may_trade is permanently False today and that is CORRECT — it needs cash_checked, which
  needs a DB cash ledger, which the order path brings. Do not "fix" it to look healthy.
- **Known issues / debt:** OPEN owner items from architect review of c5b56c1: (1) CLAUDE.md
  Invariant 2 still reads "worker reads settings" but the worker now WRITES frozen=true
  (owner-approved 2026-07-21) — the invariant text needs Esther's wording; (2) TRUNCATE
  bypasses ALL append-only triggers (`truncate decisions cascade` would erase the Invariant 5
  audit chain silently) — needs BEFORE TRUNCATE statement triggers on decisions/orders/trades/
  settings_history, an audit-table change ⇒ owner approval. Freeze-guard limitation (D-2,
  documented in db.engage_system_freeze): the trigger rejects an unfreeze attributed to NULL,
  which covers the drift-latch case; a bare hand-written `update settings set frozen=false`
  after an OWNER freeze inherits her UID and would pass. Not reachable from app code (only two
  SQL writers). Full closure needs a per-transaction intent token.
  tests/test_db.py hits the LIVE dev DB — not safe to run concurrently
  (two pytest processes at once cause spurious failures); serialize before CI parallelizes.
  Reconciliation gaps (documented, pre-live): cash leg unverifiable until a DB cash ledger
  exists; open-orders-by-client_order_id leg not built (needs the order path); zero-quantity
  broker positions halt — validate against real paper data before scheduling. WIRING-TIME
  safety req for the order path (per architect, safety_gate N1): loss_so_far is a
  POSITIVE-magnitude convention (positive = down money) — the order-path caller MUST pin the
  sign with an end-to-end test where a real loss fires DAILY_LOSS, else a drawdown could read
  as profit under the cap. Gate also needs its data sources wired:
  settings re-read before EVERY order (None→deny), deployed_capital + buying_power (positions
  source), seconds_since_tick (market-data stream), reconciled flag (reconciliation).
  Minor: architect memory landed in backend/.claude/agent-memory (should be top-level
  .claude/) — tidy later. Pre-live API follow-ups (non-blocking): rolled-back live-DB test for
  update_settings; optional float-rejecting validator on money PATCH; PATCH gate-503 test.
  Backend verified locally (Python 3.12.10, 157 tests green). Webull
  paper creds + Supabase keys in backend/.env. supabase CLI + Docker not installed (migrations
  via asyncpg script). Webull follow-ups: buying_power/settled_funds nested under
  account_currency_assets parse to None (BLOCKS cap logic until confirmed); get_order_status
  still on v1 path (execution-guardian switches to order_v2 at order-path time); position
  field names unverified (paper account empty). Safety "never" rules exist only as prompts —
  must become PreToolUse hooks / permissions.deny before live trading. Optional hardening
  awaiting owner OK: unique partial index on orders.previous_order_id (audit-table → approval).

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
