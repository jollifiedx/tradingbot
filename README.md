# TradingBot

Single-user automated stock research + trading bot on Webull. An LLM researches overnight;
a deterministic rules engine trades during market hours. Paper trading until it beats SPY
buy-and-hold over a meaningful forward sample.

**Owner:** Esther — sole user. Not multi-tenant, not a product.

## Layout

```
backend/     Python 3.12 + FastAPI — `api` (dashboard REST) + `worker` (the bot)
frontend/    React + Vite + TypeScript PWA (starts after first API routes exist)
supabase/    SQL migrations (Supabase CLI)
research/    Analysis docs: viability, tech stack, skills, agents (the "why")
docs/        Decision log, deploy log, weekly reviews
.claude/     Project memory (CLAUDE.md), 10 subagents, 26 skills
```

## Key documents

- [research/viability-analysis.md](research/viability-analysis.md) — go/no-go analysis; serves as PRD
- [research/tech-stack.md](research/tech-stack.md) — stack decisions, schema, integration map
- [.claude/CLAUDE.md](.claude/CLAUDE.md) — architecture invariants and safety rules (read first)

## Setup (dev)

1. Install Python 3.12+ and Node 20+.
2. `cd backend && python -m venv .venv && .venv\Scripts\activate && pip install -e .[dev]`
3. Copy `.env.example` → `backend/.env` and fill values (never commit).
4. Run API: `uvicorn app.api.main:app --reload` · Run tests: `pytest`

## Safety model (short version)

The LLM is an analyst, not a trader — only the deterministic rules engine places orders.
The worker fails closed: stale data, unreadable settings, loss-limit breach, or
reconciliation mismatch all halt trading. `decisions` and `orders` are append-only audit
tables. Paper and live are separate environments; promotion is a manual owner act.
