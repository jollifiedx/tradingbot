---
name: db-migrate
description: Create, review, and apply a Supabase SQL migration (new table, column, index, RLS policy) following TradingBot schema conventions. Use for any schema change request.
argument-hint: [description of schema change]
---

Create a Supabase migration for: $ARGUMENTS

1. Inspect current schema (Supabase CLI diff or Supabase MCP, dev project only).
2. Write a timestamped SQL migration file in `supabase/migrations/` via
   `supabase migration new <slug>`. Never mutate schema directly via MCP.
3. Conventions (non-negotiable): tables snake_case plural; money columns `numeric`;
   timestamps `timestamptz` (UTC); RLS enabled on every new table; `decisions` and
   `orders` stay append-only — any migration touching them must preserve the
   UPDATE/DELETE-revoking policies and triggers.
4. Apply to local/dev (`supabase db push` or `supabase migration up`), then verify with a
   smoke query.
5. Report whether the migration is reversible and what a rollback would require.

Changing the SHAPE of `settings`, `orders`, or `decisions` requires owner approval first —
stop and escalate rather than proceeding.
